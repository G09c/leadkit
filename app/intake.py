"""Recebimento de uma submissão: valida no servidor, pontua e grava lead + submissão + job numa
transação. O disparo no Hatchet acontece depois do commit (ver `app.workflows`).

As rotas públicas são anônimas e cada envio gasta a OpenAI e escreve para o e-mail digitado, então
`accept_submission` aplica dois limites (`SubmissionLimits`), depois de reconhecer o replay: o
número de envios da mesma lead, por formulário, numa janela, e o teto de pedidos em andamento.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_core import PydanticCustomError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.consent import (
    ConsentDeclaration,
    ConsentIn,  # noqa: F401 - contrato de importação legado
    ConsentSource,
    check_method,
    record,
    same_declaration,
)
from app.forms import FormRegistry, FormSpec, ScoreResult
from app.models import CaptureReceipt, Job, Lead, Submission, utcnow
from app.tokens import CaptureSigner, TokenSigner, new_public_id

logger = logging.getLogger(__name__)

# Sugestão de espera quando a fila está cheia (cabeçalho Retry-After do 503): o tempo de a fila
# andar um pouco, não uma previsão.
QUEUE_RETRY_AFTER_SECONDS = 300
IN_PROGRESS = ("queued", "running")

TRACKING_KEYS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "fbclid",
        "gclid",
        "source",
        "campaign",
        "landing_page",
        "referrer",
    }
)


class SubmissionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    partial_token: str | None = Field(default=None, max_length=100)
    answers: dict[str, Any]
    consent: ConsentDeclaration
    tracking: dict[str, str] = Field(default_factory=dict)


class ConsentTextMismatch(Exception):
    """O formulário exibiu outra versão do texto de consentimento (HTTP 409)."""


class IdempotencyConflict(Exception):
    """A mesma chave de idempotência chegou com respostas diferentes (HTTP 409)."""


class SubmissionRefused(Exception):
    """Envio recusado por limite. `retry_after`, em segundos, vai no cabeçalho Retry-After. As
    exceções não levam dado da lead (e-mail, nome, respostas): quem as trata pode repassá-las."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"retry_after={retry_after}")
        self.retry_after = retry_after


class SubmissionLimitExceeded(SubmissionRefused):
    """A lead já fez o máximo de envios neste formulário na janela (HTTP 429)."""


class QueueFull(SubmissionRefused):
    """Há pedidos demais em andamento: o envio novo espera (HTTP 503)."""


@dataclass(frozen=True)
class SubmissionLimits:
    per_email: int  # envios por e-mail normalizado e por formulário, dentro da janela
    window: timedelta
    queue_max: int  # pedidos em `queued` ou `running`


@dataclass(frozen=True)
class Accepted:
    job: Job
    token: str
    replay: bool
    form: FormSpec


@dataclass(frozen=True)
class CaptureAccepted:
    submission: Submission
    receipt: CaptureReceipt
    token: str
    replay: bool
    form: FormSpec


def submission_spec(session: Session, forms: FormRegistry, slug: str, key: str) -> FormSpec:
    """Resolve a versão sob a trava da chave, mantida até o commit de quem recebe o envio.

    A chamada e toda validação dependente da versão devem usar a mesma transação. Assim um
    reenvio aguarda o envio original confirmar antes de consultar perguntas e consentimento.
    """
    _lock_submission(session, slug, key)
    previous = _find_submission(session, slug, key)
    if previous is not None:
        return forms.get(slug, previous.form_version_number)
    return forms.current(slug)


def _lock_submission(session: Session, slug: str, key: str) -> None:
    # A chave tem namespace próprio; não trava o formulário nem depende da identidade da lead,
    # que só pode ser extraída depois de resolver a versão e validar as respostas.
    session.execute(
        sa.text("select pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"submission:{slug}:{key}"},
    )


def clean_tracking(raw: dict[str, str]) -> dict[str, str]:
    return {k: str(v)[:300] for k, v in raw.items() if k in TRACKING_KEYS and v}


def _check_marketing_channels(spec: FormSpec, consent: ConsentDeclaration) -> None:
    """Levanta `ValidationError` (campo `consent.marketing_<canal>`) se o envio autoriza marketing
    por um canal que o formulário não pede. Um formulário sem tela (o antigo, em código) não declara
    canais: nada a conferir."""
    if spec.screen is None:
        return
    channels = [
        key
        for key in ("marketing_email", "marketing_whatsapp")
        if getattr(consent, key) and not getattr(spec.screen.consent, key)
    ]
    if channels:
        raise ValidationError.from_exception_data(
            "ConsentIn",
            [
                {
                    "type": PydanticCustomError(
                        "marketing_channel",
                        "Este formulário não solicita autorização de marketing por este canal.",
                    ),
                    "loc": ("consent", key),
                    "input": None,
                }
                for key in channels
            ],
            hide_input=True,
        )


def stored_answers(spec: FormSpec, answers: BaseModel) -> dict[str, Any]:
    """As respostas como ficam gravadas e vão no webhook: a pergunta que as condições do formulário
    esconderam fica ausente. A que ficou sem resposta e aparecia segue como `None`, como sempre; um
    formulário sem condições não muda em nada."""
    stored = answers.model_dump(mode="json")
    if spec.conditions is not None and spec.conditions.present:
        for name in spec.conditions.evaluate(stored).hidden:
            stored.pop(name, None)
    return stored


def accept_submission(
    session: Session,
    spec: FormSpec,
    body: SubmissionIn,
    *,
    idempotency_key: str,
    signer: TokenSigner,
    link_ttl: timedelta,
    limits: SubmissionLimits,
    now: datetime | None = None,
    forms: FormRegistry | None = None,
    cipher=None,
    source: ConsentSource = "public_api_declaration",
    partial_capture_row=None,
) -> Accepted | CaptureAccepted:
    """Levanta `pydantic.ValidationError` (respostas inválidas, ou autorização de marketing por um
    canal que o formulário não pede), `ConsentTextMismatch`, `IdempotencyConflict`,
    `SubmissionLimitExceeded` ou `QueueFull`. Não faz commit: quem chama controla a transação, e uma
    recusa não deixa nada gravado.

    Reenviar uma chave já gravada devolve o resultado de antes sem passar pelos limites: o replay
    não cria pedido nem conta como envio."""
    if body.partial_token:
        from app.partial_capture import accept_final

        return accept_final(
            session,
            spec,
            body,
            signer=signer,
            link_ttl=link_ttl,
            limits=limits,
            now=now,
            forms=forms,
            cipher=cipher,
            source=source,
        )
    from app.integration_readiness import configuration_lock

    configuration_lock(session, shared=True)
    _lock_submission(session, spec.slug, idempotency_key)
    existing = _find_submission(session, spec.slug, idempotency_key)
    if existing is not None and forms is not None:
        spec = forms.get(spec.slug, existing.form_version_number)
    answers = spec.answers_model.model_validate(body.answers)
    answers_json = stored_answers(spec, answers)
    if existing is not None and existing.consent.get("evidence_schema") == 2:
        # Uma chave já confirmada compara a intenção original antes das recusas de um envio novo.
        return _replay(session, existing, answers_json, signer, spec, body, forms)
    if body.consent.text_version != spec.consent_text_version:
        raise ConsentTextMismatch(spec.consent_text_version)
    check_method(
        spec.screen.consent.acquisition_method if spec.screen else "checkbox", body.consent
    )
    _check_marketing_channels(spec, body.consent)
    now = now or utcnow()

    if existing is not None:
        return _replay(session, existing, answers_json, signer, spec, body, forms)

    identity = spec.identity(answers)
    tracking = clean_tracking(body.tracking)
    result = spec.score(answers) if spec.score else ScoreResult()
    try:
        with session.begin_nested():
            # A linha da lead fica travada até o fim da transação: dois envios do mesmo e-mail
            # passam um de cada vez pela contagem abaixo.
            lead_id = _upsert_lead(session, identity.email, identity.name, identity.phone, tracking)
            # A irmã gêmea (mesma chave, ao mesmo tempo) acabou de gravar enquanto esperávamos a
            # linha: ela é replay, não envio novo.
            existing = _find_submission(session, spec.slug, idempotency_key)
            if existing is not None:
                return _replay(session, existing, answers_json, signer, spec, body, forms)
            _enforce_limits(session, spec.slug, lead_id, limits, now, generation=spec.generation)
            submission = Submission(
                lead_id=lead_id,
                form_slug=spec.slug,
                form_version=spec.version,
                form_version_number=spec.version_number,
                idempotency_key=idempotency_key,
                answers=answers_json,
                consent=record(body.consent, now, source),
                tracking=tracking,
                scores=result.scores,
                priority=result.priority,
                persona=result.persona,
            )
            session.add(submission)
            session.flush()
            if spec.generation == "none":
                from app.capture_deliveries import enqueue

                receipt = CaptureReceipt(
                    submission_id=submission.id,
                    public_id=new_public_id(),
                    expires_at=now + link_ttl,
                )
                session.add(receipt)
                session.flush()
                from app.handoffs import prepare_handoff

                prepare_handoff(
                    session,
                    submission,
                    spec,
                    receipt_expires_at=receipt.expires_at,
                    cipher=cipher,
                    now=now,
                )
                enqueue(session, submission, spec=spec)
                from app.partial_capture import complete

                complete(session, spec, submission, partial_capture_row, now=now)
                return CaptureAccepted(
                    submission,
                    receipt,
                    CaptureSigner(signer.secrets_).issue(receipt.public_id, receipt.token_version),
                    False,
                    spec,
                )
            job = Job(
                submission_id=submission.id,
                public_id=new_public_id(),
                expires_at=now + link_ttl,
            )
            session.add(job)
            session.flush()
            from app.handoffs import prepare_handoff

            prepare_handoff(
                session, submission, spec, receipt_expires_at=job.expires_at, cipher=cipher, now=now
            )
            from app.capture_deliveries import enqueue

            enqueue(session, submission, spec=spec)
            from app.partial_capture import complete

            complete(session, spec, submission, partial_capture_row, now=now)
    except IntegrityError:
        # Duas requisições iguais ao mesmo tempo: a perdedora devolve o resultado da vencedora.
        existing = _find_submission(session, spec.slug, idempotency_key)
        if existing is None:
            raise
        return _replay(session, existing, answers_json, signer, spec, body, forms)
    return Accepted(
        job=job, token=signer.issue(job.public_id, job.token_version), replay=False, form=spec
    )


def _enforce_limits(
    session: Session,
    form_slug: str,
    lead_id: Any,
    limits: SubmissionLimits,
    now: datetime,
    *,
    generation: str = "pdf",
) -> None:
    """Primeiro o limite da lead (a recusa mais específica), depois o teto da fila. O teto é uma
    contagem sem trava: envios simultâneos podem passar dele por alguns pedidos."""
    since = now - limits.window
    recent = session.scalars(
        sa.select(Submission.created_at)
        .where(
            Submission.lead_id == lead_id,
            Submission.form_slug == form_slug,
            Submission.created_at > since,
        )
        .order_by(Submission.created_at.desc())
        .limit(limits.per_email)
    ).all()
    if len(recent) >= limits.per_email:
        # Abre vaga quando sai da janela o envio que completa o limite: o `per_email`-ésimo mais novo.
        wait = recent[-1] + limits.window - now
        logger.warning("envio recusado no formulário %s: limite por e-mail", form_slug)
        raise SubmissionLimitExceeded(max(1, math.ceil(wait.total_seconds())))
    if generation == "none":
        return
    in_progress = session.scalar(
        sa.select(sa.func.count()).select_from(Job).where(Job.status.in_(IN_PROGRESS))
    )
    if (in_progress or 0) >= limits.queue_max:
        logger.warning("envio recusado no formulário %s: fila cheia", form_slug)
        raise QueueFull(QUEUE_RETRY_AFTER_SECONDS)


def _find_submission(session: Session, form_slug: str, key: str) -> Submission | None:
    return session.scalar(
        sa.select(Submission).where(
            Submission.form_slug == form_slug, Submission.idempotency_key == key
        )
    )


def _replay(
    session: Session,
    submission: Submission,
    answers_json: dict[str, Any],
    signer: TokenSigner,
    spec: FormSpec,
    body: SubmissionIn,
    forms: FormRegistry | None,
) -> Accepted | CaptureAccepted:
    if not same_declaration(submission.consent, body.consent):
        raise IdempotencyConflict(submission.idempotency_key)
    if forms is not None and spec.version_number != submission.form_version_number:
        # Uma publicação pode cruzar dois envios concorrentes da mesma chave. A perdedora
        # compara respostas e conclusão pela versão que a vencedora confirmou.
        spec = forms.get(submission.form_slug, submission.form_version_number)
        answers_json = stored_answers(spec, spec.answers_model.model_validate(body.answers))
        if body.consent.text_version != spec.consent_text_version:
            raise ConsentTextMismatch(spec.consent_text_version)
        _check_marketing_channels(spec, body.consent)
        check_method(
            spec.screen.consent.acquisition_method if spec.screen else "checkbox", body.consent
        )
    if submission.answers != answers_json:
        raise IdempotencyConflict(submission.idempotency_key)
    if spec.generation == "none":
        receipt = session.scalar(
            sa.select(CaptureReceipt).where(CaptureReceipt.submission_id == submission.id)
        )
        if receipt is None:
            raise RuntimeError("captura sem recibo")
        return CaptureAccepted(
            submission,
            receipt,
            CaptureSigner(signer.secrets_).issue(receipt.public_id, receipt.token_version),
            True,
            spec,
        )
    job = session.scalar(sa.select(Job).where(Job.submission_id == submission.id))
    if job is None:  # não acontece: submissão e job nascem na mesma transação
        raise RuntimeError("submissão sem job")
    return Accepted(
        job=job, token=signer.issue(job.public_id, job.token_version), replay=True, form=spec
    )


def _upsert_lead(
    session: Session, email: str, name: str | None, phone: str | None, tracking: dict[str, str]
) -> Any:
    statement = insert(Lead).values(email=email, name=name, phone=phone, first_touch=tracking)
    statement = statement.on_conflict_do_update(
        index_elements=[Lead.email],
        set_={
            # O conflito mantém a trava por e-mail, sem um envio anônimo redefinir o cadastro.
            # O contato de cada pedido vem das respostas dele e da sua versão do formulário.
            "updated_at": utcnow(),
        },
    ).returning(Lead.id)
    return session.execute(statement).scalar_one()
