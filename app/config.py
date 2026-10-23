from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuração lida de variáveis de ambiente (prefixo VM_) ou do arquivo .env."""

    model_config = SettingsConfigDict(env_prefix="VM_", env_file=".env", extra="ignore")

    # Token desta VM, o mesmo cadastrado no painel (RPAs › Máquinas).
    api_token: SecretStr

    # Allowlist: nome do script -> comando (argv) executado localmente.
    # Ex.: VM_SCRIPTS='{"extracao": ["/opt/rpa/venv/bin/python", "/opt/rpa/extracao.py"]}'
    scripts: dict[str, list[str]] = Field(default_factory=dict)

    # Diretório de trabalho dos scripts (None = diretório atual da API).
    scripts_cwd: Path | None = None

    data_dir: Path = Path("data")
    max_concurrent_jobs: int = Field(default=1, ge=1)
    job_timeout_seconds: float | None = Field(default=None, gt=0)
    cancel_grace_seconds: float = Field(default=10, ge=0)
    log_tail_lines: int = Field(default=200, ge=1)
    timezone: str = "America/Sao_Paulo"

    # Envio dos resultados (::extracao / ::designacao) ao painel. Sem URL, os itens ficam guardados
    # no SQLite e são enviados quando ela for configurada. O token é o próprio api_token da máquina.
    painel_url: str | None = None
    ingestao_intervalo_seconds: float = Field(default=5, gt=0)
    ingestao_min_intervalo_seconds: float = Field(default=1, ge=0)  # o painel aceita 60 req/min
    ingestao_lote_max: int = Field(default=1000, ge=1, le=1000)
    ingestao_timeout_seconds: float = Field(default=15, gt=0)
    ingestao_retencao_dias: int = Field(default=7, ge=1)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"


@lru_cache
def get_settings() -> Settings:
    return Settings()
