"""Salvamento e retomada por capacidade temporária, sem acionar geração."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from copy import deepcopy
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, create_model

from app.consent import ConsentIn, record
from app.destinations import SecretCipher
from app.intake import _check_marketing_channels, clean_tracking
from app.models import PartialCapture, utcnow


class PartialUnavailable(Exception):
    """Ausente, expirada ou encerrada: mesma resposta pública."""


class PartialConflict(Exception):
    """Outra revisão foi salva; nunca sobrescrever silenciosamente."""


class SavePartial(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    token: str | None = Field(default=None, max_length=100)
    revision: int = Field(default=0, ge=0)
    step: int = Field(ge=0, le=100)
    answers: dict[str, Any]
    consent: ConsentIn
    tracking: dict[str, str] = Field(default_factory=dict)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def lookup(session, slug, token, *, now=None, allow_completed=False):
    row = session.scalar(
        sa.select(PartialCapture)
        .where(PartialCapture.form_slug == slug, PartialCapture.token_hash == digest(token))
        .with_for_update()
    )
    if (
        row is None
        or row.expires_at <= (now or utcnow())
        or (row.submission_id is not None and not allow_completed)
    ):
        raise PartialUnavailable()
    return row


def snapshot(row, *, token=None):
    result = {
        "id": str(row.id),
        "revision": row.revision,
        "step": row.step,
        "version_number": row.form_version_number,
        "expires_at": row.expires_at.isoformat(),
        "answers": row.answers,
        "consent": row.consent,
        "tracking": row.tracking,
    }
    if token:
        result["token"] = token
    return result


def save(session, deps, slug, body, *, now=None):
    now = now or utcnow()
    cipher = SecretCipher.from_settings(deps.settings)
    if cipher is None:
        raise PartialUnavailable()
    key = digest(f"{slug}:{body.request_id}")
    session.execute(
        sa.text("select pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"partial:{key}"},
    )
    row = (
        lookup(session, slug, body.token, now=now)
        if body.token
        else session.scalar(
            sa.select(PartialCapture).where(PartialCapture.create_key_hash == key).with_for_update()
        )
    )
    if row and (row.expires_at <= now or row.submission_id is not None):
        raise PartialUnavailable()
    spec = deps.forms.get(slug, row.form_version_number) if row else deps.forms.current(slug)
    if spec.partial_capture is None or spec.screen is None:
        raise PartialUnavailable()
    if body.step >= len(spec.screen.steps) or (row is None and body.step != 0):
        raise PartialConflict()
    _check_marketing_channels(spec, body.consent)
    if body.consent.text_version != spec.consent_text_version:
        raise PartialConflict()
    names = {q.name for step in spec.screen.steps[: body.step + 1] for q in step.questions}
    hidden = set(spec.conditions.evaluate(body.answers).hidden) if spec.conditions else set()
    fields = {
        name: (field.annotation, deepcopy(field))
        for name, field in spec.answers_model.model_fields.items()
        if name in names and name not in hidden
    }
    model = create_model("PartialAnswers", __config__=ConfigDict(extra="forbid"), **fields)
    answers = model.model_validate(
        {k: v for k, v in body.answers.items() if k not in hidden}
    ).model_dump(mode="json")
    fingerprint = digest(
        json.dumps(
            body.model_dump(mode="json", exclude={"token"}), sort_keys=True, separators=(",", ":")
        )
    )
    if row:
        if row.last_request_id == body.request_id:
            if row.last_digest != fingerprint:
                raise PartialConflict()
            return snapshot(row, token=cipher.open(row.sealed_token)), True
        if body.revision != row.revision:
            raise PartialConflict()
        row.answers = answers
        row.step = body.step
        row.revision += 1
        row.last_request_id = body.request_id
        row.last_digest = fingerprint
    else:
        from app.forms.retention import in_force, periods_of

        periods = periods_of(session, slug)
        hours = min(
            spec.partial_capture["ttl_hours"], in_force(periods, deps.settings).request_days * 24
        )
        token = secrets.token_urlsafe(32)
        row = PartialCapture(
            form_slug=slug,
            form_version_number=spec.version_number,
            create_key_hash=key,
            token_hash=digest(token),
            sealed_token=cipher.seal(token),
            answers=answers,
            consent=record(body.consent, now, "public_api_declaration"),
            tracking=clean_tracking(body.tracking),
            step=body.step,
            revision=1,
            last_request_id=body.request_id,
            last_digest=fingerprint,
            expires_at=now + timedelta(hours=hours),
        )
        session.add(row)
        session.flush()
    from app.partial_deliveries import enqueue

    if row.revision == 1:
        enqueue(session, row, spec)
    elif (
        row.answers.get(spec.partial_capture["contact_permission_field"])
        != spec.partial_capture["contact_permission_value"]
    ):
        enqueue(session, row, spec, revoked=True)
    return snapshot(row, token=cipher.open(row.sealed_token)), False


def accept_final(session, spec, body, *, forms, **options):
    """Fecha a captura na mesma transação que a submissão validada completa."""
    from app.intake import CaptureAccepted, accept_submission

    row = lookup(
        session, spec.slug, body.partial_token, now=options.get("now"), allow_completed=True
    )
    if forms is None:
        raise PartialUnavailable()
    historical = forms.get(spec.slug, row.form_version_number)
    accepted = accept_submission(
        session,
        historical,
        body.model_copy(update={"partial_token": None, "tracking": row.tracking}),
        idempotency_key=f"partial:{row.id}",
        forms=forms,
        **options,
    )
    row.submission_id = (
        accepted.submission.id
        if isinstance(accepted, CaptureAccepted)
        else accepted.job.submission_id
    )
    session.flush()
    if not accepted.replay:
        from app.partial_deliveries import enqueue

        enqueue(session, row, historical, completed=True)
    return accepted


def purge(deps, *, now):
    """Apaga conteúdo; hash da criação impede que replay tardio recrie dados vencidos."""
    from app.models import PartialDelivery

    count = 0
    while True:
        with deps.sessions.begin() as session:
            rows = list(
                session.scalars(
                    sa.select(PartialCapture)
                    .where(
                        PartialCapture.expires_at <= now,
                        PartialCapture.sealed_token != "",
                        PartialCapture.submission_id.is_(None),
                    )
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                session.execute(
                    sa.delete(PartialDelivery).where(PartialDelivery.partial_id == row.id)
                )
                row.answers = {}
                row.consent = {}
                row.tracking = {}
                row.sealed_token = ""
                count += 1
        if len(rows) < 100:
            return count
