import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.schemas import JobStatus


@dataclass
class Job:
    job_id: str
    idempotency_key: str
    execution_id: str
    script: str
    parameters: dict[str, Any]
    created_at: str
    status: JobStatus = JobStatus.QUEUED
    started_at: str | None = None
    finished_at: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    log_tail: list[str] = field(default_factory=list)
    error: str | None = None
    cancel_requested: bool = False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id           TEXT PRIMARY KEY,
    idempotency_key  TEXT NOT NULL UNIQUE,
    execution_id     TEXT NOT NULL,
    script           TEXT NOT NULL,
    parameters       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    status           TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    metrics          TEXT NOT NULL,
    log_tail         TEXT NOT NULL,
    error            TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0
)
"""

# Outbox dos resultados que os scripts imprimem, até o painel confirmar o recebimento.
_SCHEMA_INGESTAO = """
CREATE TABLE IF NOT EXISTS ingestao (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    tipo         TEXT NOT NULL,
    item         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'PENDENTE',
    erro         TEXT,
    criado_em    TEXT NOT NULL,
    enviado_em   TEXT
);
CREATE INDEX IF NOT EXISTS ingestao_status ON ingestao (status, tipo, execution_id, id);
CREATE INDEX IF NOT EXISTS ingestao_job ON ingestao (job_id);
"""

PENDENTE = "PENDENTE"
ENVIADO = "ENVIADO"
REJEITADO = "REJEITADO"

_COLUMNS = (
    "job_id", "idempotency_key", "execution_id", "script", "parameters", "created_at",
    "status", "started_at", "finished_at", "metrics", "log_tail", "error", "cancel_requested",
)


class JobStore:
    """Persistência dos jobs em SQLite, para que status e idempotência sobrevivam a reinícios."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.executescript(_SCHEMA_INGESTAO)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def save(self, job: Job) -> None:
        row = (
            job.job_id, job.idempotency_key, job.execution_id, job.script,
            json.dumps(job.parameters), job.created_at, job.status.value, job.started_at,
            job.finished_at, json.dumps(job.metrics, ensure_ascii=False),
            json.dumps(job.log_tail, ensure_ascii=False), job.error, int(job.cancel_requested),
        )
        placeholders = ", ".join("?" for _ in _COLUMNS)
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO jobs ({', '.join(_COLUMNS)}) VALUES ({placeholders})", row
            )
            self._conn.commit()

    def get(self, job_id: str) -> Job | None:
        return self._fetch_one("SELECT * FROM jobs WHERE job_id = ?", job_id)

    def get_by_idempotency_key(self, key: str) -> Job | None:
        return self._fetch_one("SELECT * FROM jobs WHERE idempotency_key = ?", key)

    def recover(self, now: str) -> list[Job]:
        """Após reinício: jobs que estavam rodando falham; os enfileirados voltam para a fila."""
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, error = ? WHERE status = ?",
                (JobStatus.FAILED.value, now, "Execução interrompida: a API da VM foi reiniciada",
                 JobStatus.RUNNING.value),
            )
            self._conn.commit()
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at", (JobStatus.QUEUED.value,)
            ).fetchall()
        return [self._to_job(r) for r in rows]

    def adicionar_itens(
        self, job_id: str, execution_id: str, tipo: str, itens: list[dict[str, Any]], agora: str
    ) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT INTO ingestao (job_id, execution_id, tipo, item, criado_em) VALUES (?, ?, ?, ?, ?)",
                [(job_id, execution_id, tipo, json.dumps(i, ensure_ascii=False), agora) for i in itens],
            )
            self._conn.commit()

    def grupos_pendentes(self) -> list[tuple[str, str]]:
        """(tipo, execution_id) com itens pendentes, do mais antigo para o mais novo."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT tipo, execution_id FROM ingestao WHERE status = ? "
                "GROUP BY tipo, execution_id ORDER BY MIN(id)",
                (PENDENTE,),
            ).fetchall()
        return [(r["tipo"], r["execution_id"]) for r in rows]

    def pendentes(self, tipo: str, execution_id: str, limite: int) -> list[tuple[int, dict[str, Any]]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, item FROM ingestao WHERE status = ? AND tipo = ? AND execution_id = ? "
                "ORDER BY id LIMIT ?",
                (PENDENTE, tipo, execution_id, limite),
            ).fetchall()
        return [(r["id"], json.loads(r["item"])) for r in rows]

    def marcar(self, ids: list[int], status: str, agora: str, erro: str | None = None) -> None:
        with self._lock:
            self._conn.executemany(
                "UPDATE ingestao SET status = ?, erro = ?, enviado_em = ? WHERE id = ?",
                [(status, erro, agora, i) for i in ids],
            )
            self._conn.commit()

    def contagem_ingestao(self, job_id: str) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM ingestao WHERE job_id = ? GROUP BY status", (job_id,)
            ).fetchall()
        por_status = {r["status"]: r["n"] for r in rows}
        return {
            "pendentes": por_status.get(PENDENTE, 0),
            "enviados": por_status.get(ENVIADO, 0),
            "rejeitados": por_status.get(REJEITADO, 0),
        }

    def purgar_enviados(self, antes_de: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM ingestao WHERE status = ? AND enviado_em < ?", (ENVIADO, antes_de)
            )
            self._conn.commit()

    def _fetch_one(self, sql: str, *args: Any) -> Job | None:
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return self._to_job(row) if row else None

    @staticmethod
    def _to_job(row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"],
            idempotency_key=row["idempotency_key"],
            execution_id=row["execution_id"],
            script=row["script"],
            parameters=json.loads(row["parameters"]),
            created_at=row["created_at"],
            status=JobStatus(row["status"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            metrics=json.loads(row["metrics"]),
            log_tail=json.loads(row["log_tail"]),
            error=row["error"],
            cancel_requested=bool(row["cancel_requested"]),
        )
