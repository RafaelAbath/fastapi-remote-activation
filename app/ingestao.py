import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.store import ENVIADO, REJEITADO, JobStore

logger = logging.getLogger(__name__)

# tipo do item -> rota do painel (POST /api/rpa/ingestao/...)
ENDPOINTS = {"extracao": "extracoes", "designacao": "designacoes"}

_BACKOFF_MAX_SECONDS = 300
_ERRO_DE_ITEM = re.compile(r"^itens\.(\d+)(\.|$)")


class IngestaoSender:
    """Envia ao painel, em lotes, os resultados guardados no outbox; reenvia até o painel confirmar."""

    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self.store = store
        self._tz = ZoneInfo(settings.timezone)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task | None = None
        self._acordar = asyncio.Event()
        self._ultimo_envio = 0.0

    def now(self) -> str:
        return datetime.now(self._tz).isoformat(timespec="seconds")

    async def start(self) -> None:
        if not self.settings.painel_url:
            logger.warning("VM_PAINEL_URL não configurada: resultados ficam guardados sem envio ao painel")
            return
        self._client = httpx.AsyncClient(
            base_url=self.settings.painel_url.rstrip("/"),
            headers={"Authorization": f"Bearer {self.settings.api_token.get_secret_value()}"},
            timeout=self.settings.ingestao_timeout_seconds,
            transport=self._transport,
        )
        self._task = asyncio.create_task(self._loop(), name="ingestao-sender")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()

    def acordar(self) -> None:
        """Envia já, sem esperar o próximo intervalo (ex.: quando um job termina)."""
        self._acordar.set()

    async def _loop(self) -> None:
        falhas = 0
        while True:
            try:
                ok = await self.enviar_pendentes()
                limite = datetime.now(self._tz) - timedelta(days=self.settings.ingestao_retencao_dias)
                self.store.purgar_enviados(limite.isoformat(timespec="seconds"))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Falha inesperada no envio ao painel")
                ok = False

            if ok:
                falhas = 0
                try:
                    await asyncio.wait_for(
                        self._acordar.wait(), timeout=self.settings.ingestao_intervalo_seconds
                    )
                except TimeoutError:
                    pass
            else:
                falhas += 1
                espera = self.settings.ingestao_intervalo_seconds * 2 ** min(falhas, 10)
                await asyncio.sleep(min(espera, _BACKOFF_MAX_SECONDS))
            self._acordar.clear()

    async def enviar_pendentes(self) -> bool:
        """Envia tudo o que está pendente. False = falha temporária, tentar de novo mais tarde."""
        for tipo, execution_id in self.store.grupos_pendentes():
            while lote := self.store.pendentes(tipo, execution_id, self.settings.ingestao_lote_max):
                if not await self._enviar_lote(tipo, execution_id, lote):
                    return False
        return True

    async def _enviar_lote(self, tipo: str, execution_id: str, lote: list[tuple[int, dict]]) -> bool:
        assert self._client is not None
        espera = self._ultimo_envio + self.settings.ingestao_min_intervalo_seconds - time.monotonic()
        if espera > 0:
            await asyncio.sleep(espera)
        self._ultimo_envio = time.monotonic()

        ids = [item_id for item_id, _ in lote]
        corpo = {"execution_id": execution_id, "itens": [item for _, item in lote]}
        try:
            resp = await self._client.post(f"/api/rpa/ingestao/{ENDPOINTS[tipo]}", json=corpo)
        except httpx.HTTPError as exc:
            logger.warning("Painel inacessível ao enviar %s (%d itens): %s", tipo, len(lote), exc)
            return False

        if resp.status_code == 200:
            self.store.marcar(ids, ENVIADO, self.now())
            return True
        if resp.status_code == 422:
            self._rejeitar(tipo, ids, resp)
            return True
        logger.warning(
            "Painel respondeu %d ao enviar %s (%d itens): %s",
            resp.status_code, tipo, len(lote), resp.text[:300],
        )
        return False

    def _rejeitar(self, tipo: str, ids: list[int], resp: httpx.Response) -> None:
        """422 recusa o lote inteiro: descarta só os itens apontados e deixa o resto para reenviar."""
        try:
            erros = resp.json().get("errors") or {}
        except (ValueError, AttributeError):
            erros = {}
        por_item: dict[int, str] = {}
        geral = []
        for campo, mensagens in erros.items():
            msg = f"{campo}: {'; '.join(mensagens) if isinstance(mensagens, list) else mensagens}"
            match = _ERRO_DE_ITEM.match(campo)
            if match and int(match.group(1)) < len(ids):
                indice = int(match.group(1))
                por_item[indice] = f"{por_item[indice]}; {msg}" if indice in por_item else msg
            else:
                geral.append(msg)

        agora = self.now()
        if por_item and not geral:
            for indice, msg in por_item.items():
                self.store.marcar([ids[indice]], REJEITADO, agora, msg)
            logger.warning("Painel recusou %d item(ns) de %s: %s", len(por_item), tipo, list(por_item.values())[:3])
        else:
            msg = "; ".join(geral) or resp.text[:500]
            self.store.marcar(ids, REJEITADO, agora, msg)
            logger.warning("Painel recusou o lote de %s (%d itens): %s", tipo, len(ids), msg)
