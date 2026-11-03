"""Retenção LGPD (`app.retention.purge_expired`) contra o Postgres de teste e o storage local.

O relógio é injetado: a limpeza roda "daqui a N dias". Só quando dois pedidos precisam de idades
diferentes um deles é envelhecido no banco (`ended_days_ago`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from conftest import DEMO_ANSWERS, record_webhook
from test_intake import submit
from test_pipeline import run_until_finalize

from app import crons, jobs, pipeline, retention
from app.deps import Deps
from app.errors import FailureCode, RetryableError, TerminalError
from app.models import Artifact, AuditLog, Base, Job, utcnow
from app.settings import Settings

LEAD_TRACES = ("contadora@example.test", "Contadora Exemplo", "Escritório Exemplo")
# As definições dos formulários trazem respostas de exemplo fictícias, com nomes parecidos com os
# do lead de teste: são configuração, não dado de lead, e a retenção não as toca.
DEFINITION_TABLES = ("forms", "form_versions")


def days_later(days: int) -> datetime:
    return utcnow() + timedelta(days=days)


def stored_rows(deps: Deps) -> dict[str, list[str]]:
    """Cada tabela do app -> suas linhas como JSON (onde procurar rastros do lead)."""
    with deps.sessions() as session:
        return {
            table.name: list(
                session.scalars(sa.text(f"select row_to_json(t)::text from {table.name} t"))
            )
            for table in Base.metadata.sorted_tables
        }


def lead_traces(deps: Deps) -> list[str]:
    """Dados do lead de exemplo encontrados em qualquer linha de qualquer tabela que não seja a das
    definições dos formulários."""
    rows = [
        row
        for name, found in stored_rows(deps).items()
        if name not in DEFINITION_TABLES
        for row in found
    ]
    return [trace for trace in LEAD_TRACES if any(trace in row for row in rows)]


def pdf_keys(deps: Deps, job_id: uuid.UUID) -> list[str]:
    statement = sa.select(Artifact.storage_key).where(Artifact.job_id == job_id)
    with deps.sessions() as session:
        return list(session.scalars(statement))


def delivered_request(deps: Deps) -> uuid.UUID:
    """Pedido concluído, com PDF no storage e webhook entregue."""
    record_webhook(deps)
    job_id, deliveries = run_until_finalize(deps)
    pipeline.deliver(deps, deliveries[0])
    return job_id


def test_a_request_past_retention_leaves_no_trace_of_the_lead(deps: Deps) -> None:
    job_id = delivered_request(deps)
    (key,) = pdf_keys(deps, job_id)

    report = retention.purge_expired(deps, now=days_later(61))

    assert (report.requests_deleted, report.leads_deleted, report.pdfs_deleted) == (1, 1, 1)
    rows = stored_rows(deps)
    # Fica só configuração: os formulários e as versões deles, o destino do webhook, a auditoria da
    # criação dele e a da retenção.
    left = {name: len(found) for name, found in rows.items() if found}
    assert left == {
        "audit_log": 2,
        "webhook_destinations": 1,
        "forms": 3,
        "form_versions": 3,
        "deployment_contract": 1,
        "webhook_issuer": 1,
    }
    assert lead_traces(deps) == []
    with pytest.raises(FileNotFoundError):
        deps.storage.get(key)


def request_counts(deps: Deps) -> dict[str, int]:
    rows = stored_rows(deps)
    return {name: len(rows[name]) for name in ("leads", "submissions", "jobs", "plans")}


def test_a_request_inside_the_period_is_kept(deps: Deps) -> None:
    delivered_request(deps)

    report = retention.purge_expired(deps, now=days_later(59))

    assert report.requests_deleted == 0
    assert request_counts(deps) == {"leads": 1, "submissions": 1, "jobs": 1, "plans": 1}


def test_nothing_goes_while_the_lead_link_is_still_valid(deps: Deps) -> None:
    """Um link emitido quando o LINK_TTL_DAYS era maior guarda essa validade: os prazos padrão
    (30 e 60 dias) esperam o link vencer."""
    deps.settings = Settings(  # type: ignore[call-arg]
        _env_file=None, LINK_TTL_DAYS="365", RETENTION_PDF_DAYS="365", RETENTION_DAYS="365"
    )
    delivered_request(deps)
    deps.settings = Settings(_env_file=None)  # type: ignore[call-arg]

    report = retention.purge_expired(deps, now=days_later(200))

    assert (report.requests_deleted, report.pdfs_deleted) == (0, 0)
    assert request_counts(deps) == {"leads": 1, "submissions": 1, "jobs": 1, "plans": 1}
    assert retention.purge_expired(deps, now=days_later(366)).requests_deleted == 1


def ended_days_ago(deps: Deps, job_id: uuid.UUID, days: int) -> None:
    """O pedido foi feito e terminou `days` dias atrás (link de 30 dias)."""
    moment = utcnow() - timedelta(days=days)
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Job)
            .where(Job.id == job_id)
            .values(created_at=moment, updated_at=moment, expires_at=moment + timedelta(days=30))
        )


def test_a_lead_with_a_newer_request_keeps_its_contact(deps: Deps) -> None:
    old = submit(deps, key="pedido-antigo").job
    submit(deps, key="pedido-novo")  # mesmo e-mail: mesmo lead
    ended_days_ago(deps, old.id, 200)

    report = retention.purge_expired(deps)

    assert (report.requests_deleted, report.leads_deleted) == (1, 0)
    rows = stored_rows(deps)
    assert request_counts(deps) == {"leads": 1, "submissions": 1, "jobs": 1, "plans": 0}
    assert old.public_id not in rows["jobs"][0]
    assert "contadora@example.test" in rows["leads"][0]


def test_only_the_lead_of_the_old_request_goes(deps: Deps) -> None:
    other = {**DEMO_ANSWERS, "email": "outra@example.test", "nome": "Outra Pessoa"}
    old = submit(deps, key="pedido-antigo").job
    submit(deps, key="pedido-de-outra-pessoa", answers=other)
    ended_days_ago(deps, old.id, 200)

    report = retention.purge_expired(deps)

    assert (report.requests_deleted, report.leads_deleted) == (1, 1)
    (lead,) = stored_rows(deps)["leads"]
    assert "outra@example.test" in lead


def test_the_pdf_file_leaves_the_storage_before_the_rest_of_the_request(deps: Deps) -> None:
    job_id = delivered_request(deps)
    (key,) = pdf_keys(deps, job_id)
    assert retention.purge_expired(deps, now=days_later(29)).pdfs_deleted == 0

    report = retention.purge_expired(deps, now=days_later(31))

    assert (report.pdfs_deleted, report.requests_deleted) == (1, 0)
    with pytest.raises(FileNotFoundError):
        deps.storage.get(key)
    assert request_counts(deps) == {"leads": 1, "submissions": 1, "jobs": 1, "plans": 1}
    assert retention.purge_expired(deps, now=days_later(32)).pdfs_deleted == 0  # já foi


def audit_trail(deps: Deps) -> list[AuditLog]:
    with deps.sessions() as session:
        return list(session.scalars(sa.select(AuditLog).order_by(AuditLog.at)))


def test_each_run_leaves_an_audit_record_with_counts_and_periods_only(deps: Deps) -> None:
    delivered_request(deps)

    retention.purge_expired(deps, now=days_later(61))
    retention.purge_expired(deps, now=days_later(62))

    first, second = [audit for audit in audit_trail(deps) if audit.actor == "retention"]
    assert (first.actor, first.action, first.target_id) == ("retention", "retention.purge", None)
    assert first.details == {
        "requests_deleted": 1,
        "captures_deleted": 0,
        "partials_deleted": 0,
        "leads_deleted": 1,
        "pdfs_deleted": 1,
        "storage_errors": 0,
        "draft_tests_deleted": 0,
        "retention_days": 60,
        "retention_pdf_days": 30,
    }
    assert second.details["requests_deleted"] == 0


def test_the_reason_typed_to_reprocess_a_deleted_request_leaves_the_audit(deps: Deps) -> None:
    job_id = delivered_request(deps)
    with deps.sessions.begin() as session:
        jobs.reprocess(session, job_id, actor="operadora", reason="Contadora Exemplo pediu de novo")

    retention.purge_expired(deps, now=days_later(61))

    reprocessed = next(a for a in audit_trail(deps) if a.action == "job.reprocess")
    assert reprocessed.actor == "operadora" and reprocessed.details["attempt"] == 2
    assert lead_traces(deps) == []


class DeletesFail:
    """Storage que guarda e lê, mas não consegue apagar (permissão faltando, S3 fora do ar)."""

    def __init__(self, inner: object) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)

    def delete(self, key: str) -> None:  # como o S3Storage diante de uma chave sem permissão
        raise TerminalError(FailureCode.STORAGE_FAILED, "delete_object: AccessDenied (HTTP 403)")


def test_a_request_whose_pdf_cannot_be_deleted_stays_for_the_next_run(deps: Deps) -> None:
    delivered_request(deps)
    healthy = deps.storage
    deps.storage = DeletesFail(healthy)  # type: ignore[assignment]

    report = retention.purge_expired(deps, now=days_later(61))

    assert (report.requests_deleted, report.storage_errors) == (0, 1)
    assert request_counts(deps) == {"leads": 1, "submissions": 1, "jobs": 1, "plans": 1}
    deps.storage = healthy
    assert retention.purge_expired(deps, now=days_later(61)).requests_deleted == 1


class SecondDeleteFails:
    """Storage que apaga o primeiro arquivo e cai no segundo (S3 instável)."""

    def __init__(self, inner: object) -> None:
        self.inner = inner
        self.deletes = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)

    def delete(self, key: str) -> None:
        self.deletes += 1
        if self.deletes == 2:
            raise RetryableError(FailureCode.STORAGE_FAILED, "delete_object: SlowDown (HTTP 503)")
        self.inner.delete(key)  # type: ignore[attr-defined]


def test_a_pdf_deleted_before_a_storage_failure_is_marked_removed(deps: Deps) -> None:
    job_id = delivered_request(deps)
    with deps.sessions.begin() as session:
        jobs.reprocess(session, job_id, actor="operadora", reason="segunda versão do plano")
        jobs.claim(session, job_id, 2, "run-2")
    plan_id, _ = pipeline.generate_plan(deps, job_id, 2)
    pipeline.render_pdf(deps, job_id, 2, plan_id)  # agora o pedido tem dois PDFs
    deps.storage = SecondDeleteFails(deps.storage)  # type: ignore[assignment]

    report = retention.purge_expired(deps, now=days_later(61))

    assert (report.requests_deleted, report.pdfs_deleted, report.storage_errors) == (0, 1, 1)
    with deps.sessions() as session:
        removed = session.scalars(sa.select(Artifact.purged_at.is_not(None)).order_by(Artifact.id))
        assert sorted(removed) == [False, True]  # o admin não oferece o arquivo que já saiu


def test_the_daily_task_reports_counts_and_fails_while_files_stay(deps: Deps) -> None:
    job_id = delivered_request(deps)
    ended_days_ago(deps, job_id, 200)
    healthy = deps.storage
    deps.storage = DeletesFail(healthy)  # type: ignore[assignment]

    with pytest.raises(RetryableError) as failed:
        crons.retention_purge.mock_run()

    assert failed.value.code is FailureCode.STORAGE_FAILED
    deps.storage = healthy
    assert crons.retention_purge.mock_run() == retention.RetentionReport(
        requests_deleted=1, leads_deleted=1, pdfs_deleted=1
    )


def test_the_retention_cron_runs_daily_and_by_default_only_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RETENTION_CRON", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    assert crons.retention_schedule() == []
    monkeypatch.setenv("APP_ENV", "production")
    assert crons.retention_schedule() == ["0 6 * * *"]
    monkeypatch.setenv("RETENTION_CRON", "")
    assert crons.retention_schedule() == []
