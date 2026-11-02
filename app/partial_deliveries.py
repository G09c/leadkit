"""Eventos transacionais de captura parcial para um receptor interno explícito."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import sqlalchemy as sa

from app.destinations import SecretCipher, UnreadableSecret
from app.errors import RetryableError, TerminalError
from app.models import (
    PartialCapture,
    PartialDelivery,
    Submission,
    WebhookDestination,
    WebhookIssuer,
    utcnow,
)

CONTACT = "contato.capturado"
COMPLETED = "formulario.concluido"
REVOKED = "contato.revogado"
EVENTS = {CONTACT, COMPLETED, REVOKED}


def enqueue(session, row, spec, *, completed=False, revoked=False):
    """Snapshot e revisão são conservados; nunca herda o destino global."""
    from app.integration_readiness import configuration_lock

    configuration_lock(session, shared=True)
    original = session.scalar(
        sa.select(PartialDelivery).where(
            PartialDelivery.partial_id == row.id, PartialDelivery.event == CONTACT
        )
    )
    if revoked and original is None:
        return
    if (completed or revoked) and original is not None:
        destination_id, revision = original.destination_id, original.destination_revision
    else:
        allowed = (
            row.answers.get(spec.partial_capture["contact_permission_field"])
            == spec.partial_capture["contact_permission_value"]
        )
        if not completed and not allowed:
            return
        destination = session.scalar(
            sa.select(WebhookDestination).where(WebhookDestination.form_slug == row.form_slug)
        )
        if (
            destination is None
            or destination.purpose != "internal"
            or not EVENTS.issubset(destination.events)
        ):
            return
        destination_id, revision = destination.id, destination.version
    event = REVOKED if revoked else (COMPLETED if completed else CONTACT)
    if session.scalar(
        sa.select(PartialDelivery.id).where(
            PartialDelivery.partial_id == row.id, PartialDelivery.event == event
        )
    ):
        return
    submission = session.get(Submission, row.submission_id) if completed else None
    answers = submission.answers if submission else row.answers
    identity = spec.identity(spec.answers_model.model_construct(**answers))
    event_id = uuid4()
    field = spec.partial_capture["contact_permission_field"]
    value = answers.get(field)
    payload = {
        "schema_version": 1,
        "event": event,
        "event_id": str(event_id),
        "issuer": f"leadkit:{session.get_one(WebhookIssuer, 1).namespace}",
        "occurred_at": (
            utcnow() if revoked else (submission.created_at if submission else row.created_at)
        ).isoformat(),
        "form": {
            "slug": row.form_slug,
            "version": spec.version,
            "version_number": row.form_version_number,
        },
        "partial": {"id": str(row.id), "expires_at": row.expires_at.isoformat()},
        "submission": {"id": str(submission.id), "submitted_at": submission.created_at.isoformat()}
        if submission
        else None,
        "contact": {"name": identity.name, "email": identity.email, "phone": identity.phone},
        "consent": {
            "processing": deepcopy(submission.consent if submission else row.consent),
            "contact": {
                "field": field,
                "value": value,
                "authorized": not revoked
                and value == spec.partial_capture["contact_permission_value"],
            },
        },
        "tracking": deepcopy(row.tracking),
    }
    if submission is not None:
        from app.models import Job

        job = session.scalar(sa.select(Job).where(Job.submission_id == submission.id))
        if job is not None:
            payload["job"] = {"public_id": job.public_id, "attempt": job.attempt}
    session.add(
        PartialDelivery(
            id=event_id,
            partial_id=row.id,
            destination_id=destination_id,
            destination_revision=revision,
            event=event,
            payload=payload,
        )
    )
    session.flush()


def dispatch_pending(deps, *, limit=10, now=None):
    """At-least-once, com backoff persistido; retries conservam event_id e conteúdo."""
    from app.integration_readiness import configuration_lock

    now = now or utcnow()
    with deps.sessions() as session:
        pending = session.execute(
            sa.select(PartialDelivery.id, PartialDelivery.partial_id)
            .where(PartialDelivery.status == "pending", PartialDelivery.next_attempt_at <= now)
            .order_by(PartialDelivery.next_attempt_at)
            .limit(limit)
        ).all()
    delivered = 0
    for delivery_id, partial_id in pending:
        with deps.sessions.begin() as session:
            configuration_lock(session, shared=True)
            row = session.scalar(
                sa.select(PartialCapture)
                .where(PartialCapture.id == partial_id)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                continue
            delivery = session.get(PartialDelivery, delivery_id, with_for_update=True)
            if delivery is None or delivery.status != "pending" or delivery.next_attempt_at > now:
                continue
            dest = session.get(WebhookDestination, delivery.destination_id, with_for_update=True)
            historical = deps.forms.get(row.form_slug, row.form_version_number)
            current_permission = row.answers.get(
                historical.partial_capture["contact_permission_field"]
            )
            if (
                delivery.event == CONTACT
                and current_permission != historical.partial_capture["contact_permission_value"]
            ):
                delivery.status = "cancelled"
                continue
            if delivery.event == CONTACT and (
                row.expires_at <= now or row.submission_id is not None
            ):
                delivery.status = "cancelled"
                continue
            if (
                dest is None
                or dest.form_slug != row.form_slug
                or dest.purpose != "internal"
                or dest.version != delivery.destination_revision
                or not EVENTS.issubset(dest.events)
            ):
                delivery.status = "blocked"
                continue
            cipher = SecretCipher.from_settings(deps.settings)
            if cipher is None:
                continue
            payload = deepcopy(delivery.payload)
            try:
                if delivery.event == CONTACT:
                    payload["resume_url"] = (
                        f"{deps.settings.public_base_url.rstrip('/')}/p/{row.form_slug}#resume={cipher.open(row.sealed_token)}"
                    )
                secret = cipher.open(dest.secret_sealed)
                delivery.attempts += 1
                deps.webhook.send(dest.url, secret, delivery.event, str(delivery.id), payload)
            except RetryableError:
                delivery.next_attempt_at = now + timedelta(
                    seconds=min(3600, 30 * 2 ** min(delivery.attempts, 7))
                )
            except (TerminalError, UnreadableSecret):
                delivery.status = "failed"
            else:
                delivery.status = "succeeded"
                delivered += 1
    return delivered
