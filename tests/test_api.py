import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

TOKEN = "segredo-de-teste"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

SCRIPT = """
import sys, time
mode = sys.argv[1]
if mode == "ok":
    print("login ok", flush=True)
    print("senha=123 token: abc", flush=True)
    print('::metrics {"extraidos": 7}', flush=True)
    sys.exit(0)
if mode == "fail":
    print("erro de login", flush=True)
    sys.exit(3)
if mode == "slow":
    print("iniciando", flush=True)
    time.sleep(30)
"""


@pytest.fixture
def client(tmp_path: Path):
    script = tmp_path / "rpa.py"
    script.write_text(SCRIPT)
    settings = Settings(
        api_token=TOKEN,
        scripts={
            "extracao": [sys.executable, str(script), "ok"],
            "designacao": [sys.executable, str(script), "fail"],
            "lento": [sys.executable, str(script), "slow"],
        },
        data_dir=tmp_path / "data",
        cancel_grace_seconds=1,
        _env_file=None,
    )
    with TestClient(create_app(settings)) as c:
        yield c


def create(client, script="extracao", execution_id="exec-1", **kwargs):
    body = {"script": script, "execution_id": execution_id, **kwargs}
    return client.post(
        "/api/v1/jobs", json=body, headers={**AUTH, "Idempotency-Key": execution_id}
    )


def wait_final(client, job_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).json()
        if data["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} não terminou: {data}")


def test_health(client):
    resp = client.get("/api/v1/health", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": "1.0.0", "running_jobs": 0}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer errado"}, {"Authorization": "Basic x"}])
def test_invalid_token_is_401(client, headers):
    assert client.get("/api/v1/health", headers=headers).status_code == 401


def test_job_succeeds_with_metrics_and_redacted_log(client):
    resp = create(client, parameters={"limit": 1000})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "QUEUED"
    assert body["execution_id"] == "exec-1"

    job = wait_final(client, body["job_id"])
    assert job["status"] == "SUCCEEDED"
    assert job["metrics"] == {"extraidos": 7}
    assert job["started_at"] and job["finished_at"]
    assert job["error"] is None
    lines = [line.split(" ", 1)[1] for line in job["log_tail"]]
    assert lines == ["login ok", "senha=*** token: ***"]


def test_idempotency_returns_same_job(client):
    first = create(client).json()
    second = create(client)
    assert second.status_code == 202
    assert second.json()["job_id"] == first["job_id"]


def test_idempotency_conflict_with_other_payload(client):
    create(client)
    assert create(client, script="designacao").status_code == 409


def test_failed_job(client):
    job_id = create(client, script="designacao").json()["job_id"]
    job = wait_final(client, job_id)
    assert job["status"] == "FAILED"
    assert "código 3" in job["error"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"script": "rm -rf /"},
        {"parameters": {"limit": 0}},
        {"parameters": {"limit": 10001}},
        {"parameters": {"limit": "10"}},
        {"parameters": {"comando": "ls"}},
    ],
)
def test_rejects_invalid_payload(client, kwargs):
    resp = create(client, **kwargs)
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)


def test_cancel_running_job(client):
    job_id = create(client, script="lento").json()["job_id"]
    deadline = time.monotonic() + 5
    while client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).json()["status"] != "RUNNING":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert client.get("/api/v1/health", headers=AUTH).json()["running_jobs"] == 1

    assert client.post(f"/api/v1/jobs/{job_id}/cancel", headers=AUTH).status_code == 202
    assert wait_final(client, job_id)["status"] == "CANCELLED"


def test_cancel_queued_job(client):
    running = create(client, script="lento", execution_id="exec-a").json()["job_id"]
    queued = create(client, script="extracao", execution_id="exec-b").json()["job_id"]
    resp = client.post(f"/api/v1/jobs/{queued}/cancel", headers=AUTH)
    assert resp.status_code == 202
    assert resp.json()["status"] == "CANCELLED"
    client.post(f"/api/v1/jobs/{running}/cancel", headers=AUTH)


def test_cancel_finished_job_is_409(client):
    job_id = create(client).json()["job_id"]
    wait_final(client, job_id)
    assert client.post(f"/api/v1/jobs/{job_id}/cancel", headers=AUTH).status_code == 409


def test_unknown_job_is_404(client):
    assert client.get("/api/v1/jobs/j-nao-existe", headers=AUTH).status_code == 404
    assert client.post("/api/v1/jobs/j-nao-existe/cancel", headers=AUTH).status_code == 404
