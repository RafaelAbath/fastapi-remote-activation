import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

TOKEN = "segredo-de-teste"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
EXECUTION_ID = "01a160e2-7c1b-7f6e-9d3a-2b4c5d6e7f80"

# O script de teste só reproduz no stdout o conteúdo do arquivo recebido.
ECHO_SCRIPT = "import sys; sys.stdout.write(open(sys.argv[1], encoding='utf-8').read())"


class FakePainel:
    """Laravel falso: registra as requisições e responde conforme a fila `respostas` (padrão 200)."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.respostas: list = []

    @property
    def lotes(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        resposta = self.respostas.pop(0) if self.respostas else 200
        if callable(resposta):
            return resposta(request)
        if isinstance(resposta, Exception):
            raise resposta
        if isinstance(resposta, tuple):
            return httpx.Response(resposta[0], json=resposta[1])
        itens = json.loads(request.content)["itens"]
        return httpx.Response(resposta, json={"recebidos": len(itens), "novos": len(itens), "ja_existentes": 0})


@pytest.fixture
def painel():
    return FakePainel()


@pytest.fixture
def make_client(tmp_path: Path, painel: FakePainel):
    script = tmp_path / "echo.py"
    script.write_text(ECHO_SCRIPT)
    saida = tmp_path / "saida.txt"

    def make(saida_script: str = "", **overrides):
        saida.write_text(saida_script, encoding="utf-8")
        config = {
            "painel_url": "http://painel.test/",
            "ingestao_intervalo_seconds": 0.05,
            "ingestao_min_intervalo_seconds": 0,
            **overrides,
        }
        settings = Settings(
            api_token=TOKEN,
            scripts={"extracao": [sys.executable, str(script), str(saida)]},
            data_dir=tmp_path / "data",
            _env_file=None,
            **config,
        )
        return TestClient(create_app(settings, painel_transport=httpx.MockTransport(painel.handler)))

    return make


def disparar(client, execution_id=EXECUTION_ID) -> str:
    resp = client.post(
        "/api/v1/jobs",
        json={"script": "extracao", "execution_id": execution_id},
        headers={**AUTH, "Idempotency-Key": execution_id},
    )
    assert resp.status_code == 202
    return resp.json()["job_id"]


def esperar(client, job_id, condicao, timeout=10) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).json()
        if job["status"] in ("SUCCEEDED", "FAILED") and condicao(job):
            return job
        time.sleep(0.05)
    raise AssertionError(f"condição não atingida: {job}")


def linhas(*linhas: str) -> str:
    return "".join(f"{linha}\n" for linha in linhas)


def test_resultados_sao_enviados_ao_painel(make_client, painel):
    saida = linhas(
        "login ok",
        '::extracao {"senha": "91787170", "extraida_em": "2026-10-22T21:24:57-03:00"}',
        '::extracao [{"senha": 91788750}, {"senha": "91789999"}]',
        '::designacao {"senha": "91612540", "setor": "AUTORIZAÇÃO", "status": "Em análise", "plano": null}',
        "fim",
    )
    with make_client(saida) as client:
        job_id = disparar(client)
        job = esperar(client, job_id, lambda j: j["ingestao"]["enviados"] == 4)

    assert job["ingestao"] == {"pendentes": 0, "enviados": 4, "rejeitados": 0}
    assert job["metrics"] == {"extraidos": 3, "designados": 1}
    assert [line.split(" ", 1)[1] for line in job["log_tail"]] == ["login ok", "fim"]

    por_rota = {r.url.path: json.loads(r.content) for r in painel.requests}
    assert set(por_rota) == {"/api/rpa/ingestao/extracoes", "/api/rpa/ingestao/designacoes"}
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in painel.requests)

    extracoes = por_rota["/api/rpa/ingestao/extracoes"]
    assert extracoes["execution_id"] == EXECUTION_ID
    assert [i["senha"] for i in extracoes["itens"]] == ["91787170", "91788750", "91789999"]
    assert extracoes["itens"][0]["extraida_em"] == "2026-10-22T21:24:57-03:00"
    assert extracoes["itens"][1]["extraida_em"]  # preenchida pela API

    designacao = por_rota["/api/rpa/ingestao/designacoes"]["itens"][0]
    assert designacao["setor"] == "AUTORIZAÇÃO"
    assert "plano" not in designacao  # null não é enviado
    assert designacao["designada_em"]


def test_divide_em_lotes_de_1000(make_client, painel):
    itens = [{"senha": str(90000000 + i)} for i in range(1500)]
    with make_client(linhas("::extracao " + json.dumps(itens))) as client:
        job_id = disparar(client)
        esperar(client, job_id, lambda j: j["ingestao"]["enviados"] == 1500)

    assert [len(lote["itens"]) for lote in painel.lotes] == [1000, 500]


def test_itens_invalidos_sao_ignorados_com_aviso(make_client, painel):
    saida = linhas(
        "::extracao nao-e-json",
        '::designacao {"senha": "1"}',  # falta setor
        '::extracao {"senha": "2"}',
    )
    with make_client(saida) as client:
        job_id = disparar(client)
        job = esperar(client, job_id, lambda j: j["ingestao"]["enviados"] == 1)

    assert job["metrics"] == {"extraidos": 1}
    assert sum("ignorado" in line for line in job["log_tail"]) == 2


def test_falha_temporaria_reenvia(make_client, painel):
    painel.respostas = [503, httpx.ConnectError("sem rede"), 200]
    with make_client(linhas('::extracao {"senha": "1"}')) as client:
        job_id = disparar(client)
        job = esperar(client, job_id, lambda j: j["ingestao"]["enviados"] == 1)

    assert len(painel.requests) == 3
    assert job["ingestao"]["rejeitados"] == 0


def test_422_por_item_rejeita_so_o_item(make_client, painel):
    painel.respostas = [(422, {"message": "x", "errors": {"itens.1.senha": ["Número de senha inválido."]}})]
    saida = linhas('::extracao [{"senha": "1"}, {"senha": "inv@lida"}, {"senha": "3"}]')
    with make_client(saida) as client:
        job_id = disparar(client)
        job = esperar(client, job_id, lambda j: j["ingestao"]["pendentes"] == 0)

    assert job["ingestao"] == {"pendentes": 0, "enviados": 2, "rejeitados": 1}
    assert [i["senha"] for i in painel.lotes[-1]["itens"]] == ["1", "3"]


def test_422_geral_rejeita_o_lote(make_client, painel):
    painel.respostas = [(422, {"message": "x", "errors": {"execution_id": ["Execução não encontrada para esta máquina."]}})]
    with make_client(linhas('::extracao [{"senha": "1"}, {"senha": "2"}]')) as client:
        job_id = disparar(client)
        job = esperar(client, job_id, lambda j: j["ingestao"]["pendentes"] == 0)

    assert job["ingestao"] == {"pendentes": 0, "enviados": 0, "rejeitados": 2}
    assert len(painel.requests) == 1


def test_pendentes_sao_enviados_apos_reinicio(make_client, painel):
    painel.respostas = [500] * 1000
    with make_client(linhas('::extracao {"senha": "1"}')) as client:
        job_id = disparar(client)
        esperar(client, job_id, lambda j: j["ingestao"]["pendentes"] == 1)

    painel.respostas = []
    with make_client() as client:
        job = esperar(client, job_id, lambda j: j["ingestao"]["enviados"] == 1)
    assert job["ingestao"]["pendentes"] == 0


def test_sem_painel_url_guarda_sem_enviar(make_client, painel):
    with make_client(linhas('::extracao {"senha": "1"}'), painel_url=None) as client:
        job_id = disparar(client)
        job = esperar(client, job_id, lambda j: True)
        time.sleep(0.2)
        job = client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).json()

    assert job["ingestao"] == {"pendentes": 1, "enviados": 0, "rejeitados": 0}
    assert painel.requests == []
