import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.api import router
from app.config import Settings, get_settings
from app.ingestao import IngestaoSender
from app.runner import JobRunner
from app.store import JobStore


def create_app(
    settings: Settings | None = None, painel_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = JobStore(settings.db_path)
        sender = IngestaoSender(settings, store, transport=painel_transport)
        runner = JobRunner(settings, store, sender)
        app.state.runner = runner
        await sender.start()
        await runner.start()
        try:
            yield
        finally:
            await runner.stop()
            await sender.stop()
            store.close()

    app = FastAPI(
        title="API das VMs de RPA",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.include_router(router)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # O painel exibe `detail` ao usuário: devolve um texto legível em vez da lista do Pydantic.
        messages = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"] if p != "body")
            messages.append(f"{loc}: {err['msg']}" if loc else err["msg"])
        return JSONResponse(
            status_code=422, content={"detail": "; ".join(messages)}
        )

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Cada POST ao painel apareceria no log do serviço; falhas já são registradas pelo IngestaoSender.
logging.getLogger("httpx").setLevel(logging.WARNING)
