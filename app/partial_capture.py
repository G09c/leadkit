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
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, create_model

from app.consent import ConsentIn, record, same_declaration
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
        or not row.sealed_token
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
        "last_activity_at": row.last_activity_at.isoformat(),
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
    key = digest(f"{slug}:{body.request_id}" + (f":{body.token}" if body.token else ""))
    session.execute(
        sa.text("select pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"partial:{key}"},
    )
    created = session.scalar(
        sa.select(PartialCapture).where(PartialCapture.create_key_hash == key).with_for_update()
    )
    row = created or (lookup(session, slug, body.token, now=now) if body.token else None)
    if row and (not row.sealed_token or row.expires_at <= now or row.submission_id is not None):
        raise PartialUnavailable()
    spec = deps.forms.get(slug, row.form_version_number) if row else deps.forms.current(slug)
    if spec.partial_capture is None or spec.screen is None:
        raise PartialUnavailable()
    if body.step >= len(spec.screen.steps) or (row is None and body.step != 0):
        raise PartialConflict()
    _check_marketing_channels(spec, body.consent)
    if body.consent.text_version != spec.consent_text_version:
        raise PartialConflict()
    answers = _validate_answers(spec, body, row)
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
        identity_changed = spec.identity(
            spec.answers_model.model_construct(**row.answers)
        ) != spec.identity(spec.answers_model.model_construct(**answers))
        field = spec.partial_capture["contact_permission_field"]
        if (
            identity_changed
            or row.answers.get(field) != answers.get(field)
            or not same_declaration(row.consent, body.consent)
        ):
            row = _create_successor(session, row, spec, cipher, key, now)
        row.last_activity_at = now
        row.answers = answers
        row.consent = record(body.consent, now, "public_api_declaration")
        row.step = max(row.step, body.step)
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
            created_at=now,
            last_activity_at=now,
            expires_at=now + timedelta(hours=hours),
        )
        session.add(row)
        session.flush()
    from app.partial_deliveries import enqueue

    enqueue(session, row, spec)
    return snapshot(row, token=cipher.open(row.sealed_token)), False


def _validate_answers(spec, body, row):
    """Valida a etapa atual; dados futuros inválidos ficam sem resposta, sem bloquear o retorno."""
    from app.forms.definition.questions import conditioned_answers

    saved_step = max(body.step, row.step if row else 0)
    saved_names = {q.name for step in spec.screen.steps[: saved_step + 1] for q in step.questions}
    current_names = {q.name for step in spec.screen.steps[: body.step + 1] for q in step.questions}
    candidate = {**(row.answers if row else {}), **body.answers}
    hidden = set(spec.conditions.evaluate(candidate).hidden) if spec.conditions else set()
    if set(body.answers) - saved_names - hidden:
        raise ValueError("invalid_partial")
    prepared = (
        conditioned_answers(spec.conditions, candidate, names=current_names)
        if spec.conditions
        else candidate
    )
    fields = {
        name: (field.annotation, deepcopy(field))
        for name, field in spec.answers_model.model_fields.items()
        if name in current_names and name not in hidden
    }
    config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    model = create_model("PartialAnswers", __config__=config, **fields)
    answers = model.model_validate(
        {name: value for name, value in prepared.items() if name in current_names}
    ).model_dump(mode="json")
    for name in (saved_names - current_names) - hidden:
        value = prepared.get(name)
        if value is not None:
            field = spec.answers_model.model_fields[name]
            adapter = TypeAdapter(field.rebuild_annotation(), config=config)
            try:
                value = adapter.dump_python(adapter.validate_python(value), mode="json")
            except ValidationError:
                value = None
        answers[name] = value
    return answers


def _create_successor(session, row, spec, cipher, key, now):
    """Revoga a capacidade e o fato antigos; uma identidade nova nunca herda o link."""
    from app.models import PartialDelivery
    from app.partial_deliveries import CONTACT, enqueue

    enqueue(session, row, spec, revoked=True)
    session.execute(
        sa.update(PartialDelivery)
        .where(
            PartialDelivery.partial_id == row.id,
            PartialDelivery.event == CONTACT,
            PartialDelivery.status == "pending",
        )
        .values(status="cancelled")
    )
    token = secrets.token_urlsafe(32)
    successor = PartialCapture(
        form_slug=row.form_slug,
        form_version_number=row.form_version_number,
        create_key_hash=key,
        token_hash=digest(token),
        sealed_token=cipher.seal(token),
        answers=row.answers,
        consent=row.consent,
        tracking=row.tracking,
        revision=row.revision,
        step=row.step,
        last_request_id=row.last_request_id,
        last_digest=row.last_digest,
        created_at=now,
        last_activity_at=now,
        expires_at=row.expires_at,
    )
    row.sealed_token = ""
    session.add(successor)
    session.flush()
    return successor


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
        partial_capture_row=row,
        forms=forms,
        **options,
    )
    row.last_activity_at = options.get("now") or utcnow()
    row.submission_id = (
        accepted.submission.id
        if isinstance(accepted, CaptureAccepted)
        else accepted.job.submission_id
    )
    session.flush()
    return accepted


def complete(session, spec, submission, row=None, *, now):
    """A conclusão opt-in sempre emite seu fato, inclusive sem salvamento intermediário."""
    if spec.partial_capture is None:
        return
    if row is None:
        row = PartialCapture(
            form_slug=spec.slug,
            form_version_number=spec.version_number,
            create_key_hash=digest(f"submission:{submission.id}"),
            token_hash=digest(secrets.token_urlsafe(32)),
            sealed_token="",
            answers={},
            consent={},
            tracking=submission.tracking,
            revision=1,
            step=len(spec.screen.steps) - 1,
            last_request_id=uuid.uuid4(),
            last_digest="",
            created_at=now,
            last_activity_at=now,
            expires_at=now,
        )
        session.add(row)
    row.submission_id = submission.id
    session.flush()
    from app.partial_deliveries import enqueue

    enqueue(session, row, spec, completed=True)


def detach_submission(session, submission_id):
    """A retenção apaga conteúdo e filhos, conservando a barreira de replay da criação."""
    from app.models import PartialDelivery

    rows = session.scalars(
        sa.select(PartialCapture)
        .where(PartialCapture.submission_id == submission_id)
        .with_for_update()
    ).all()
    for row in rows:
        session.execute(sa.delete(PartialDelivery).where(PartialDelivery.partial_id == row.id))
        row.submission_id = None
        row.answers = {}
        row.consent = {}
        row.tracking = {}
        row.sealed_token = ""
    session.flush()


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
                        sa.or_(
                            PartialCapture.sealed_token != "",
                            PartialCapture.answers != {},
                            PartialCapture.consent != {},
                            sa.exists().where(PartialDelivery.partial_id == PartialCapture.id),
                        ),
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
