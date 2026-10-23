import asyncio
import json
import logging
import os
import re
import signal
import time
import uuid
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.ingestao import IngestaoSender
from app.schemas import JobCreateRequest, JobStatus
from app.store import Job, JobStore

logger = logging.getLogger(__name__)

# Linha que o script imprime para reportar contagens: ::metrics {"extraidos": 120}
METRICS_PREFIX = "::metrics "

# Linhas de resultado, repassadas ao painel: prefixo -> (tipo, campo de data, métrica contada).
# Ex.: ::extracao {"senha": "91787170"}  |  ::designacao [{"senha": "...", "setor": "OPME"}, ...]
RESULT_PREFIXES = {
    "::extracao ": ("extracao", "extraida_em", "extraidos"),
    "::designacao ": ("designacao", "designada_em", "designados"),
}
_CAMPOS_OBRIGATORIOS = {"extracao": ("senha",), "designacao": ("senha", "setor")}

_SAVE_INTERVAL_SECONDS = 1.0
_STREAM_LINE_LIMIT = 1024 * 1024

_REDACTIONS = (
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer ***"),
    (
        re.compile(r"(?i)\b(senha|password|passwd|pwd|token|secret|api[_-]?key)(\s*[:=]\s*)\S+"),
        r"\1\2***",
    ),
)


def redact(line: str) -> str:
    for pattern, replacement in _REDACTIONS:
        line = pattern.sub(replacement, line)
    return line


class IdempotencyConflict(Exception):
    pass


class JobNotCancellable(Exception):
    pass


class JobRunner:
    """Fila local de jobs: cada job roda um script da allowlist como subprocesso desacoplado da requisição."""

    def __init__(self, settings: Settings, store: JobStore, sender: IngestaoSender | None = None):
        self.settings = settings
        self.store = store
        self.sender = sender
        self._tz = ZoneInfo(settings.timezone)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._live: dict[str, Job] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    def now(self) -> str:
        return datetime.now(self._tz).isoformat(timespec="seconds")

    @property
    def running_jobs(self) -> int:
        return sum(1 for job in self._live.values() if job.status == JobStatus.RUNNING)

    async def start(self) -> None:
        for job in self.store.recover(self.now()):
            self._live[job.job_id] = job
            self._queue.put_nowait(job.job_id)
        self._workers = [
            asyncio.create_task(self._worker(), name=f"job-worker-{i}")
            for i in range(self.settings.max_concurrent_jobs)
        ]

    async def stop(self) -> None:
        for proc in list(self._procs.values()):
            _signal_group(proc, signal.SIGTERM)
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    def submit(self, idempotency_key: str, request: JobCreateRequest) -> Job:
        parameters = request.parameters.model_dump(exclude_none=True)
        existing = self.get(idempotency_key=idempotency_key)
        if existing is not None:
            if existing.script != request.script or existing.parameters != parameters:
                raise IdempotencyConflict
            return existing

        job = Job(
            job_id=f"j-{uuid.uuid4().hex[:16]}",
            idempotency_key=idempotency_key,
            execution_id=request.execution_id,
            script=request.script,
            parameters=parameters,
            created_at=self.now(),
        )
        self.store.save(job)
        self._live[job.job_id] = job
        self._queue.put_nowait(job.job_id)
        return job

    def get(self, job_id: str | None = None, *, idempotency_key: str | None = None) -> Job | None:
        if job_id is not None:
            return self._live.get(job_id) or self.store.get(job_id)
        for job in self._live.values():
            if job.idempotency_key == idempotency_key:
                return job
        return self.store.get_by_idempotency_key(idempotency_key)

    async def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is None:
            return None
        if job.status == JobStatus.CANCELLED or job.cancel_requested:
            return job
        if job.status.is_final:
            raise JobNotCancellable

        job.cancel_requested = True
        if job.status == JobStatus.QUEUED:
            self._finish(job, JobStatus.CANCELLED)
        else:
            proc = self._procs.get(job_id)
            if proc is not None:
                _signal_group(proc, signal.SIGTERM)
                asyncio.create_task(self._kill_after_grace(job_id, proc))
            self.store.save(job)
        return job

    async def _kill_after_grace(self, job_id: str, proc: asyncio.subprocess.Process) -> None:
        await asyncio.sleep(self.settings.cancel_grace_seconds)
        if proc.returncode is None and self._procs.get(job_id) is proc:
            _signal_group(proc, signal.SIGKILL)

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                job = self._live.get(job_id)
                if job is not None and job.status == JobStatus.QUEUED:
                    await self._run(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Falha inesperada no job %s", job_id)
                job = self._live.get(job_id)
                if job is not None and not job.status.is_final:
                    self._finish(job, JobStatus.FAILED, "Falha interna da API da VM")
            finally:
                self._queue.task_done()

    async def _run(self, job: Job) -> None:
        argv = list(self.settings.scripts[job.script])
        if "limit" in job.parameters:
            argv += ["--limit", str(job.parameters["limit"])]

        env = {k: v for k, v in os.environ.items() if not k.startswith("VM_")}
        env.update(RPA_JOB_ID=job.job_id, RPA_EXECUTION_ID=job.execution_id, PYTHONUNBUFFERED="1")
        if "limit" in job.parameters:
            env["RPA_LIMIT"] = str(job.parameters["limit"])

        tail: deque[str] = deque(job.log_tail, maxlen=self.settings.log_tail_lines)
        job.status = JobStatus.RUNNING
        job.started_at = self.now()
        self.store.save(job)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.settings.scripts_cwd,
                env=env,
                start_new_session=True,
                limit=_STREAM_LINE_LIMIT,
            )
        except OSError as exc:
            logger.error("Não foi possível iniciar %s: %s", argv, exc)
            self._finish(job, JobStatus.FAILED, f"Não foi possível iniciar o script '{job.script}'")
            return

        self._procs[job.job_id] = proc
        if job.cancel_requested:
            # Cancelamento chegou entre a marcação RUNNING e o início do processo.
            _signal_group(proc, signal.SIGTERM)
            asyncio.create_task(self._kill_after_grace(job.job_id, proc))
        timed_out = False
        try:
            await asyncio.wait_for(
                self._consume_output(job, proc, tail), timeout=self.settings.job_timeout_seconds
            )
        except TimeoutError:
            timed_out = True
            _signal_group(proc, signal.SIGTERM)
            asyncio.create_task(self._kill_after_grace(job.job_id, proc))
        returncode = await proc.wait()
        self._procs.pop(job.job_id, None)
        job.log_tail = list(tail)
        if self.sender is not None:
            self.sender.acordar()

        if job.cancel_requested:
            self._finish(job, JobStatus.CANCELLED)
        elif timed_out:
            self._finish(job, JobStatus.FAILED, "Tempo limite de execução excedido")
        elif returncode == 0:
            self._finish(job, JobStatus.SUCCEEDED)
        else:
            self._finish(job, JobStatus.FAILED, _error_message(returncode, tail))

    async def _consume_output(
        self, job: Job, proc: asyncio.subprocess.Process, tail: deque[str]
    ) -> None:
        assert proc.stdout is not None
        last_save = time.monotonic()
        while True:
            try:
                raw = await proc.stdout.readline()
            except ValueError:
                # Linha maior que o limite do buffer: descarta o pedaço e segue.
                continue
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            result_prefix = next((p for p in RESULT_PREFIXES if line.startswith(p)), None)
            if line.startswith(METRICS_PREFIX):
                _merge_metrics(job.metrics, line[len(METRICS_PREFIX):])
            elif result_prefix is not None:
                self._registrar_resultado(job, result_prefix, line[len(result_prefix):], tail)
            elif line.strip():
                stamp = datetime.now(self._tz).strftime("%H:%M:%S")
                tail.append(f"{stamp} {redact(line)}")
            if time.monotonic() - last_save >= _SAVE_INTERVAL_SECONDS:
                job.log_tail = list(tail)
                self.store.save(job)
                last_save = time.monotonic()

    def _registrar_resultado(self, job: Job, prefix: str, payload: str, tail: deque[str]) -> None:
        """Guarda no outbox os itens de uma linha ::extracao/::designacao (enviados depois ao painel)."""
        tipo, campo_data, metrica = RESULT_PREFIXES[prefix]
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            data = None
        itens = data if isinstance(data, list) else [data]

        agora = self.now()
        validos = []
        for item in itens:
            if not isinstance(item, dict) or "senha" not in item:
                continue
            item = {k: v for k, v in item.items() if v is not None}
            if isinstance(item["senha"], int):
                item["senha"] = str(item["senha"])
            if not all(isinstance(item.get(c), str) and item[c].strip() for c in _CAMPOS_OBRIGATORIOS[tipo]):
                continue
            item.setdefault(campo_data, agora)
            validos.append(item)

        if len(validos) < len(itens):
            stamp = datetime.now(self._tz).strftime("%H:%M:%S")
            tail.append(f"{stamp} aviso: {len(itens) - len(validos)} item(ns) de {prefix.strip()} ignorado(s) por formato inválido")
        if validos:
            self.store.adicionar_itens(job.job_id, job.execution_id, tipo, validos, agora)
            job.metrics[metrica] = job.metrics.get(metrica, 0) + len(validos)

    def _finish(self, job: Job, status: JobStatus, error: str | None = None) -> None:
        job.status = status
        job.finished_at = self.now()
        job.error = error
        self.store.save(job)
        self._live.pop(job.job_id, None)


def _merge_metrics(metrics: dict, payload: str) -> None:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return
    if isinstance(data, dict):
        metrics.update(
            {str(k): v for k, v in data.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        )


def _error_message(returncode: int, tail: deque[str]) -> str:
    if returncode < 0:
        return f"Script encerrado pelo sinal {-returncode}"
    last = tail[-1].split(" ", 1)[-1] if tail else ""
    message = f"Script terminou com código {returncode}"
    return f"{message}: {last[:200]}" if last else message


def _signal_group(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass
