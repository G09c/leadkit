"""Retenção LGPD: apaga os dados vencidos da lead (cron `retention_purge`, em `app.crons`).

Dois prazos em dias, os padrões do ambiente (app.settings):
- `RETENTION_PDF_DAYS` (30): o arquivo do PDF sai do storage esse tempo depois de gerado; a linha
  do artefato fica, marcada com `purged_at` (o admin mostra "removido").
- `RETENTION_DAYS` (60): o pedido inteiro sai quando o job está parado há esse tempo (fim do
  processamento ou do último reprocessamento): entregas, PDFs (linha e arquivo), planos, job e
  envio (respostas, consentimento e rastreamento). A lead (e-mail, nome, telefone e primeiro
  contato) sai junto quando não tem outro pedido.
Cada formulário pode ter os dois prazos próprios (`forms.retention_pdf_days` e
`forms.retention_days`, definidos no admin, `app.forms.retention`): a retenção resolve os prazos de
cada pedido pelo formulário dele, com o padrão do ambiente onde ele não tem prazo, e o prazo vale
também para os pedidos que já existem. `due_for_form` conta o que uma execução apagaria com prazos
dados, para o admin mostrar antes de a operadora confirmar um prazo menor; ele e a execução
compartilham as mesmas condições.
Nenhum dos dois apaga nada enquanto o link da lead vale (`jobs.expires_at`), seja qual for o prazo.
A subida recusa prazos menores que o `LINK_TTL_DAYS` (`Settings.check`), e o admin os do
formulário, mas o link guarda a validade com que foi emitido: se o `LINK_TTL_DAYS` baixar, os links
antigos seguram os pedidos deles até vencer; se subir, um prazo próprio que ficou abaixo dele
espera o link vencer.

Apagar, e não anonimizar: o banco do app não é o histórico do funil (o CRM recebe tudo pelo
webhook) e plano e respostas têm texto livre, que não dá para anonimizar com segurança. A entrega PDF
não guarda payload: ele é montado na hora do envio a partir das outras tabelas. A intenção de
respostas v2 conserva snapshot histórico na outbox e sai pela cascata da mesma submissão. O mesmo vale para a
sincronização com uma integração (`integration_syncs`), que sai junto com o pedido. As linhas que já foram para
a planilha do Baserow ficam lá: a retenção dela é da operadora, fora do app.

Os testes do rascunho de um formulário (`app.forms.draft_tests`) saem `TEST_DAYS` (3) dias depois de
começar, com as respostas que a operadora usou, o prompt, o plano e o PDF. São respostas fictícias,
mas nada garante que ela não colou um dado de verdade.

Auditoria sem dados pessoais: uma linha em `audit_log` por execução, só com contagens e os prazos
padrão (os de cada formulário ficam na auditoria da mudança deles, `form.retention`); o motivo
digitado num reprocessamento (texto livre) sai junto com o pedido. Arquivo que não sai do storage
deixa o pedido inteiro para a próxima execução.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.deps import Deps
from app.errors import RetryableError, TerminalError
from app.forms.draft_tests import TEST_DAYS
from app.models import (
    Artifact,
    AuditLog,
    CaptureReceipt,
    Delivery,
    DraftTest,
    DraftTestCommand,
    Form,
    IntegrationSync,
    Job,
    Lead,
    Plan,
    PlanRelease,
    Submission,
    utcnow,
)

logger = logging.getLogger(__name__)

BATCH = 100


class RetentionReport(BaseModel):
    requests_deleted: int = 0
    captures_deleted: int = 0
    partials_deleted: int = 0
    leads_deleted: int = 0
    pdfs_deleted: int = 0
    storage_errors: int = 0
    draft_tests_deleted: int = 0


def purge_expired(deps: Deps, *, now: datetime | None = None) -> RetentionReport:
    """Uma passada completa. Idempotente: o que falhar fica para a próxima."""
    now = now or utcnow()
    report = RetentionReport()
    from app.handoffs import expire_ciphertexts

    with deps.sessions.begin() as session:
        expire_ciphertexts(session, now=now)
    from app.partial_capture import purge

    report.partials_deleted = purge(deps, now=now)
    _purge_captures(deps, now, report)
    _purge_requests(deps, now, report)
    if not report.storage_errors:  # storage fora do ar: a próxima execução tenta de novo
        _purge_pdfs(deps, now, report)
    _purge_draft_tests(deps, now, report)
    with deps.sessions.begin() as session:
        session.add(
            AuditLog(
                at=now,
                actor="retention",
                action="retention.purge",
                details={
                    **report.model_dump(),
                    "retention_days": deps.settings.retention_days,
                    "retention_pdf_days": deps.settings.retention_pdf_days,
                },
            )
        )
    return report


def _interval(days: sa.ColumnElement[int] | int) -> sa.ColumnElement[timedelta]:
    return sa.func.make_interval(0, 0, 0, days)


def _request_due(now: datetime, days: sa.ColumnElement[int] | int) -> sa.ColumnElement[bool]:
    """O pedido parado há mais de `days` dias, com o link da lead vencido: sai inteiro."""
    return sa.and_(Job.updated_at < now - _interval(days), Job.expires_at < now)


def _pdf_due(now: datetime, days: sa.ColumnElement[int] | int) -> sa.ColumnElement[bool]:
    """O arquivo do PDF gerado há mais de `days` dias, ainda no storage, com o link vencido."""
    return sa.and_(
        Artifact.purged_at.is_(None),
        Artifact.created_at < now - _interval(days),
        Job.expires_at < now,
    )


def _purge_requests(deps: Deps, now: datetime, report: RetentionReport) -> None:
    # O prazo do pedido é o do formulário dele, ou o do ambiente onde não há prazo próprio.
    days = sa.func.coalesce(Form.retention_days, deps.settings.retention_days)
    while True:
        with deps.sessions.begin() as session:
            batch = session.execute(
                sa.select(Job.id, Job.submission_id)
                .join(Submission, Submission.id == Job.submission_id)
                .outerjoin(Form, Form.slug == Submission.form_slug)
                .where(_request_due(now, days))
                .order_by(Job.updated_at)
                .limit(BATCH)
                .with_for_update(of=Job, skip_locked=True)
            ).all()
            lead_ids = sa.select(Submission.lead_id).where(
                Submission.id.in_([row.submission_id for row in batch])
            )
            session.execute(
                sa.select(Lead.id).where(Lead.id.in_(lead_ids)).order_by(Lead.id).with_for_update()
            ).all()
            for job_id, submission_id in batch:
                stored = session.execute(
                    sa.select(Artifact.id, Artifact.storage_key).where(
                        Artifact.job_id == job_id, Artifact.purged_at.is_(None)
                    )
                ).all()
                deleted = _delete_files(deps, stored, report)
                if len(deleted) < len(stored):  # storage falhou: o pedido fica para a próxima
                    _mark_purged(session, deleted, now)
                    break
                report.leads_deleted += _delete_request(session, job_id, submission_id)
                report.requests_deleted += 1
        if report.storage_errors or len(batch) < BATCH:
            return


def _purge_pdfs(deps: Deps, now: datetime, report: RetentionReport) -> None:
    """Arquivos de PDF vencidos de pedidos que ainda ficam: sai o arquivo, a linha é marcada."""
    days = sa.func.coalesce(Form.retention_pdf_days, deps.settings.retention_pdf_days)
    while True:
        with deps.sessions.begin() as session:
            batch = session.execute(
                sa.select(Artifact.id, Artifact.storage_key)
                .join(Job, Job.id == Artifact.job_id)
                .join(Submission, Submission.id == Job.submission_id)
                .outerjoin(Form, Form.slug == Submission.form_slug)
                .where(_pdf_due(now, days))
                .order_by(Artifact.created_at)
                .limit(BATCH)
                .with_for_update(of=Artifact, skip_locked=True)
            ).all()
            _mark_purged(session, _delete_files(deps, batch, report), now)
        if report.storage_errors or len(batch) < BATCH:
            return


@dataclass(frozen=True)
class Due:
    """O que uma execução apagaria de um formulário: pedidos inteiros e, dos que ficam, arquivos de
    PDF."""

    requests: int
    pdfs: int
    captures: int = 0


def due_for_form(
    session: Session, slug: str, *, pdf_days: int, request_days: int, now: datetime
) -> Due:
    """Quantos pedidos do formulário `slug` uma execução em `now` apagaria, e quantos arquivos de PDF
    tiraria do storage dos pedidos que ficam, se os prazos dele fossem `pdf_days` e `request_days`.
    Usa as mesmas condições da execução."""
    requests = session.scalar(
        sa.select(sa.func.count())
        .select_from(Job)
        .join(Submission, Submission.id == Job.submission_id)
        .where(Submission.form_slug == slug, _request_due(now, request_days))
    )
    pdfs = session.scalar(
        sa.select(sa.func.count())
        .select_from(Artifact)
        .join(Job, Job.id == Artifact.job_id)
        .join(Submission, Submission.id == Job.submission_id)
        .where(
            Submission.form_slug == slug,
            _pdf_due(now, pdf_days),
            ~_request_due(now, request_days),  # o pedido que sai inteiro leva o PDF junto
        )
    )
    captures = session.scalar(
        sa.select(sa.func.count())
        .select_from(Submission)
        .join(CaptureReceipt, CaptureReceipt.submission_id == Submission.id)
        .where(Submission.form_slug == slug, Submission.created_at < now - _interval(request_days))
    )
    return Due(requests or 0, pdfs or 0, captures or 0)


def _purge_draft_tests(deps: Deps, now: datetime, report: RetentionReport) -> None:
    """Os testes do rascunho começados há mais de `TEST_DAYS` dias, com tudo o que guardam."""
    with deps.sessions.begin() as session:
        # O prazo técnico dt1 não encurta nem prolonga a política independente do resultado.
        session.execute(sa.delete(DraftTestCommand).where(DraftTestCommand.expires_at <= now))
        deleted = session.execute(
            sa.delete(DraftTest).where(DraftTest.started_at < now - timedelta(days=TEST_DAYS))
        )
        report.draft_tests_deleted = deleted.rowcount


def _delete_files(
    deps: Deps, stored: Sequence[tuple[uuid.UUID, str]], report: RetentionReport
) -> list[uuid.UUID]:
    """Apaga os arquivos (artefato, chave) em ordem e para no primeiro erro do storage, que fica
    no log (a mensagem do adaptador não tem dados do lead). Devolve os artefatos que saíram."""
    deleted = []
    for artifact_id, key in stored:
        try:
            deps.storage.delete(key)
        except (RetryableError, TerminalError) as exc:
            report.storage_errors += 1
            logger.warning("retenção: PDF não saiu do storage artifact=%s: %s", artifact_id, exc)
            break
        deleted.append(artifact_id)
    report.pdfs_deleted += len(deleted)
    return deleted


def _mark_purged(session: Session, artifact_ids: list[uuid.UUID], now: datetime) -> None:
    """O arquivo saiu do storage: o admin deixa de oferecê-lo e a retenção não conta de novo."""
    if artifact_ids:
        session.execute(
            sa.update(Artifact).where(Artifact.id.in_(artifact_ids)).values(purged_at=now)
        )


def _delete_request(session: Session, job_id: uuid.UUID, submission_id: uuid.UUID) -> int:
    """Apaga as linhas do pedido; devolve 1 se o lead também saiu (não tinha outro pedido)."""
    from app.partial_capture import detach_submission

    detach_submission(session, submission_id)
    session.execute(sa.delete(Delivery).where(Delivery.job_id == job_id))
    session.execute(sa.delete(IntegrationSync).where(IntegrationSync.job_id == job_id))
    session.execute(sa.delete(PlanRelease).where(PlanRelease.job_id == job_id))
    session.execute(sa.delete(Artifact).where(Artifact.job_id == job_id))
    session.execute(sa.delete(Plan).where(Plan.job_id == job_id))
    session.execute(sa.delete(Job).where(Job.id == job_id))
    lead_id = session.scalar(
        sa.delete(Submission).where(Submission.id == submission_id).returning(Submission.lead_id)
    )
    # A auditoria do pedido fica (quem fez o quê, quando); o motivo de um reprocessamento é texto
    # livre de quem operou e pode citar o lead, então sai com o pedido.
    session.execute(
        sa.update(AuditLog)
        .where(AuditLog.target_type == "job", AuditLog.target_id == str(job_id))
        .values(details=AuditLog.details.op("-")(sa.literal("reason", sa.Text)))
    )
    try:
        with session.begin_nested():
            alone = ~sa.exists().where(Submission.lead_id == Lead.id)
            return session.execute(sa.delete(Lead).where(Lead.id == lead_id, alone)).rowcount
    except IntegrityError:  # um pedido novo do mesmo lead chegou agora: o lead fica
        return 0


def _purge_captures(deps: Deps, now: datetime, report: RetentionReport) -> None:
    """Prazo contado do aceite; validade do recibo e replay não prolongam dados pessoais."""
    days = sa.func.coalesce(Form.retention_days, deps.settings.retention_days)
    while True:
        with deps.sessions.begin() as session:
            batch = session.execute(
                sa.select(Submission.id, Submission.lead_id)
                .join(CaptureReceipt, CaptureReceipt.submission_id == Submission.id)
                .outerjoin(Form, Form.slug == Submission.form_slug)
                .where(Submission.created_at < now - _interval(days))
                .order_by(Submission.created_at, Submission.id)
                .limit(BATCH)
                .with_for_update(of=Submission, skip_locked=True)
            ).all()
            session.execute(
                sa.select(Lead.id)
                .where(Lead.id.in_([row.lead_id for row in batch]))
                .order_by(Lead.id)
                .with_for_update()
            ).all()
            for submission_id, lead_id in batch:
                from app.partial_capture import detach_submission

                detach_submission(session, submission_id)
                session.execute(sa.delete(Submission).where(Submission.id == submission_id))
                report.captures_deleted += 1
                alone = ~sa.exists().where(Submission.lead_id == Lead.id)
                try:
                    with session.begin_nested():
                        report.leads_deleted += session.execute(
                            sa.delete(Lead).where(Lead.id == lead_id, alone)
                        ).rowcount
                except IntegrityError:
                    pass  # um novo envio válido conserva a lead
        if len(batch) < BATCH:
            return
