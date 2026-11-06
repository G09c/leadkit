"""Contexto da submissão por chave persistida; nunca procura pelo cadastro de contato."""

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.orm import Session, load_only

from app.errors import TerminalError
from app.forms import FormRegistry
from app.models import Job, Submission
from app.pdf_availability import availability_dict, current_pdf
from app.plan_release import release_snapshot
from app.submission_identity import submission_form


class ContextConflict(Exception):
    """Os vínculos persistidos não permitem escolher um contexto inequívoco."""


def commercial_context(
    session: Session,
    forms: FormRegistry,
    submission_id: UUID,
    *,
    form: str,
    with_lead: bool = False,
) -> dict[str, Any] | None:
    """Lê metadados primeiro e tracking apenas com autorização conferida pela chamadora."""
    row = session.scalars(
        sa.select(Submission)
        .options(
            load_only(
                Submission.id,
                Submission.form_slug,
                Submission.form_version,
                Submission.form_version_number,
                Submission.created_at,
                raiseload=True,
            )
        )
        .where(Submission.id == submission_id, Submission.form_slug == form)
    ).one_or_none()
    if row is None:
        return None
    try:
        spec = submission_form(forms, row)
    except TerminalError:
        spec = None
    # O lock compartilhado conserva a tentativa enquanto lê artefato e liberação.
    try:
        job = session.scalars(
            sa.select(Job)
            .options(
                load_only(
                    Job.id,
                    Job.submission_id,
                    Job.public_id,
                    Job.attempt,
                    Job.status,
                    raiseload=True,
                )
            )
            .where(Job.submission_id == row.id)
            .with_for_update(read=True)
        ).one_or_none()
    except MultipleResultsFound:
        raise ContextConflict from None
    if job and (job.attempt < 1 or (spec and spec.generation == "none")):
        raise ContextConflict
    tracking = None
    if with_lead:
        tracking = session.execute(
            sa.select(Submission.tracking).where(Submission.id == row.id)
        ).one_or_none()
        # A retenção pode excluir uma captura entre os SELECTs em READ COMMITTED.
        if tracking is None:
            return None
    return {
        "submission_id": row.id,
        "form_slug": row.form_slug,
        "form_version": row.form_version,
        "form_version_number": row.form_version_number,
        "created_at": row.created_at,
        "historical_version_available": spec is not None,
        "generation": spec.generation if spec else None,
        "job": {
            "id": job.id,
            "public_id": job.public_id,
            "attempt": job.attempt,
            "status": job.status,
            "current_pdf": availability_dict(current_pdf(session, None, job)),
            "release": release_snapshot(session, job),
        }
        if job
        else None,
        "lead_data": {"tracking": tracking[0]} if tracking is not None else None,
    }
