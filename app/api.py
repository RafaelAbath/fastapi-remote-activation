from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app import __version__
from app.runner import IdempotencyConflict, JobNotCancellable, JobRunner
from app.schemas import (
    HealthResponse,
    JobCreatedResponse,
    JobCreateRequest,
    JobResponse,
)
from app.security import require_token
from app.store import Job

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)])


def get_runner(request: Request) -> JobRunner:
    return request.app.state.runner


def _to_response(job: Job, runner: JobRunner) -> JobResponse:
    return JobResponse(
        job_id=job.job_id,
        execution_id=job.execution_id,
        script=job.script,
        status=job.status,
        started_at=job.started_at,
        finished_at=job.finished_at,
        metrics=job.metrics,
        log_tail=job.log_tail,
        error=job.error,
        ingestao=runner.store.contagem_ingestao(job.job_id),
    )


@router.get("/health", response_model=HealthResponse)
async def health(runner: JobRunner = Depends(get_runner)) -> HealthResponse:
    return HealthResponse(version=__version__, running_jobs=runner.running_jobs)


@router.post("/jobs", response_model=JobCreatedResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    body: JobCreateRequest,
    runner: JobRunner = Depends(get_runner),
    idempotency_key: str | None = Header(default=None, max_length=128),
) -> JobCreatedResponse:
    if body.script not in runner.settings.scripts:
        raise HTTPException(
            status_code=422,
            detail=f"Script não permitido nesta VM: '{body.script}'",
        )
    try:
        job = runner.submit(idempotency_key or body.execution_id, body)
    except IdempotencyConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key já usado com outro script ou parâmetros",
        ) from None
    return JobCreatedResponse(job_id=job.job_id, execution_id=job.execution_id, status=job.status)


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, runner: JobRunner = Depends(get_runner)) -> JobResponse:
    job = runner.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job não encontrado")
    return _to_response(job, runner)


@router.post(
    "/jobs/{job_id}/cancel", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED
)
async def cancel_job(job_id: str, runner: JobRunner = Depends(get_runner)) -> JobResponse:
    try:
        job = await runner.cancel(job_id)
    except JobNotCancellable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Job já finalizado, não pode ser cancelado"
        ) from None
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job não encontrado")
    return _to_response(job, runner)
