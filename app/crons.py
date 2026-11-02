"""Reconciliador: substitui a outbox e o watchdog.

Não existe transação entre o nosso Postgres e o do Hatchet. O envio é: (1) commit do job,
(2) disparo no Hatchet, (3) gravação do run id. Qualquer queda entre esses passos é corrigida aqui:
- job `queued` sem run id                    -> dispara;
- run sumiu do engine                        -> redispara (ou falha se o job está parado há horas);
- run terminou sem atualizar o job           -> marca o job como falho;
- entrega pendente sem run id                -> dispara a entrega;
- sincronização pendente sem run id          -> dispara a sincronização (`app.integrations.syncs`).

No fim do arquivo, o outro cron: a retenção LGPD diária (`retention_purge`, em app.retention).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum

from hatchet_sdk import (
    ConcurrencyExpression,
    ConcurrencyLimitStrategy,
    Context,
    EmptyModel,
    V1TaskStatus,
)
from hatchet_sdk.clients.rest.exceptions import ApiException
from pydantic import BaseModel

from app import jobs
from app.deps import Deps, get_deps
from app.errors import FailureCode, RetryableError
from app.hatchet_client import hatchet
from app.integrations import syncs
from app.models import utcnow
from app.retention import RetentionReport, purge_expired
from app.workflows import (
    SCHEDULE_TIMEOUT,
    launch_capture,
    launch_delivery,
    launch_generate_plan,
    launch_sync,
)

logger = logging.getLogger(__name__)

UNLAUNCHED_AFTER = timedelta(seconds=30)
IN_FLIGHT_AFTER = timedelta(minutes=10)
STUCK_AFTER = timedelta(hours=2)
BATCH = 50


def reconcile_schedule() -> list[str]:
    """Agendamento do reconciliador. Ligado por padrão só em produção: o engine cria as execuções
    do cron mesmo com o worker desligado, e no plano gratuito do Hatchet Cloud cada uma conta na
    cota diária. `RECONCILE_CRON=""` desliga; em desenvolvimento rode `reconcile_once` à mão."""
    default = "*/2 * * * *" if os.environ.get("APP_ENV") == "production" else ""
    cron = os.environ.get("RECONCILE_CRON", default).strip()
    return [cron] if cron else []


class RunState(StrEnum):
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MISSING = "missing"


class ReconcileReport(BaseModel):
    jobs_launched: int = 0
    jobs_relaunched: int = 0
    jobs_failed: int = 0
    deliveries_launched: int = 0
    captures_launched: int = 0
    captures_relaunched: int = 0
    captures_failed: int = 0
    partial_events_delivered: int = 0
    syncs_launched: int = 0
    errors: int = 0


def hatchet_run_state(run_id: str) -> RunState:
    try:
        status = hatchet.runs.get_status(run_id)
    except ApiException as exc:
        if exc.status == 404:
            return RunState.MISSING
        raise
    if status in (V1TaskStatus.QUEUED, V1TaskStatus.RUNNING):
        return RunState.ACTIVE
    if status == V1TaskStatus.COMPLETED:
        return RunState.SUCCEEDED
    return RunState.FAILED


def reconcile_once(
    deps: Deps,
    *,
    launch_job: Callable[[object, int], str | None] = launch_generate_plan,
    run_state: Callable[[str], RunState] = hatchet_run_state,
    launch_delivery_fn: Callable[[object], str | None] = launch_delivery,
    launch_sync_fn: Callable[[object], str | None] = launch_sync,
    launch_capture_fn: Callable[[object, int], str | None] = launch_capture,
) -> ReconcileReport:
    report = ReconcileReport()

    with deps.sessions.begin() as session:
        pending = [
            (j.id, j.attempt)
            for j in jobs.unlaunched_jobs(session, older_than=UNLAUNCHED_AFTER, limit=BATCH)
        ]
    for job_id, attempt in pending:
        if launch_job(job_id, attempt) is None:
            report.errors += 1
            break  # engine fora: não insiste, o próximo ciclo tenta de novo
        report.jobs_launched += 1

    with deps.sessions.begin() as session:
        in_flight = [
            (j.id, j.attempt, j.hatchet_run_id, j.updated_at)
            for j in jobs.in_flight_jobs(session, older_than=IN_FLIGHT_AFTER, limit=BATCH)
        ]
    for job_id, attempt, run_id, updated_at in in_flight:
        try:
            state = run_state(str(run_id))
        except Exception:
            logger.exception("consulta do run falhou job=%s", job_id)
            report.errors += 1
            break
        if state is RunState.ACTIVE:
            continue
        stuck = utcnow() - updated_at > STUCK_AFTER
        if state is RunState.MISSING and not stuck:
            if launch_job(job_id, attempt) is None:
                report.errors += 1
            else:
                report.jobs_relaunched += 1
            continue
        detail = {
            RunState.MISSING: "execução não encontrada no Hatchet",
            RunState.SUCCEEDED: "execução terminou sem concluir o job",
            RunState.FAILED: "execução falhou ou foi cancelada no Hatchet",
        }[state]
        with deps.sessions.begin() as session:
            if jobs.fail(session, job_id, attempt, FailureCode.STUCK.value, detail):
                report.jobs_failed += 1

    with deps.sessions.begin() as session:
        deliveries = [
            d.id
            for d in jobs.unlaunched_deliveries(session, older_than=UNLAUNCHED_AFTER, limit=BATCH)
        ]
    for delivery_id in deliveries:
        if launch_delivery_fn(delivery_id) is None:
            report.errors += 1
            break
        report.deliveries_launched += 1

    with deps.sessions.begin() as session:
        pending_syncs = [
            sync.id for sync in syncs.unlaunched(session, older_than=UNLAUNCHED_AFTER, limit=BATCH)
        ]
    for sync_id in pending_syncs:
        if launch_sync_fn(sync_id) is None:
            report.errors += 1
            break
        report.syncs_launched += 1

    from app.capture_deliveries import reconcile

    counts = reconcile(
        deps,
        run_state=run_state,
        launch=launch_capture_fn,
        unlaunched_after=UNLAUNCHED_AFTER,
        in_flight_after=IN_FLIGHT_AFTER,
        limit=BATCH,
    )
    report.captures_launched, report.captures_relaunched, report.captures_failed, errors = counts
    report.errors += errors
    from app.partial_deliveries import dispatch_pending

    report.partial_events_delivered = dispatch_pending(deps)
    return report


@hatchet.task(
    name="reconcile_jobs",
    on_crons=reconcile_schedule(),
    # Uma execução por vez: se um ciclo ainda está rodando, o seguinte é descartado.
    concurrency=ConcurrencyExpression(
        expression="'reconcile'",
        max_runs=1,
        limit_strategy=ConcurrencyLimitStrategy.CANCEL_NEWEST,
    ),
    execution_timeout=timedelta(minutes=2),
    schedule_timeout=SCHEDULE_TIMEOUT,
    retries=0,
)
def reconcile_jobs(input: EmptyModel, ctx: Context) -> ReconcileReport:
    return reconcile_once(get_deps())


# --- retenção LGPD ----------------------------------------------------------------------------


def retention_schedule() -> list[str]:
    """Agendamento da retenção (app.retention): uma vez por dia, às 06:00 UTC (03:00 em
    Brasília). Ligado por padrão só em produção, pelo mesmo motivo do reconciliador (cota diária
    do Hatchet Cloud). `RETENTION_CRON=""` desliga; em desenvolvimento rode `purge_expired` à mão."""
    default = "0 6 * * *" if os.environ.get("APP_ENV") == "production" else ""
    cron = os.environ.get("RETENTION_CRON", default).strip()
    return [cron] if cron else []


@hatchet.task(
    name="retention_purge",
    on_crons=retention_schedule(),
    concurrency=ConcurrencyExpression(
        expression="'retention'",
        max_runs=1,
        limit_strategy=ConcurrencyLimitStrategy.CANCEL_NEWEST,
    ),
    execution_timeout=timedelta(minutes=30),
    schedule_timeout=SCHEDULE_TIMEOUT,
    retries=0,  # a execução do dia seguinte é a nova tentativa
)
def retention_purge(input: EmptyModel, ctx: Context) -> RetentionReport:
    """Saída só com contagens. Arquivo que não saiu do storage falha a execução (aparece no
    painel do Hatchet): dado pessoal guardado além do prazo não pode passar em silêncio."""
    report = purge_expired(get_deps())
    if report.storage_errors:
        raise RetryableError(
            FailureCode.STORAGE_FAILED, "a retenção não conseguiu apagar PDFs do storage"
        )
    return report
