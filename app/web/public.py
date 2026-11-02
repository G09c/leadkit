"""Jornada pública do lead: formulário (`/p/{slug}`) -> espera e resultado (`/d/{public_id}`).

O token de acesso ao pedido nunca fica na URL: o envio responde 303 com um cookie HttpOnly
restrito ao caminho do pedido, e o link com `?t=` (webhook -> e-mail/WhatsApp) é trocado pelo
mesmo cookie. Toda resposta deste roteador sai com `PUBLIC_HEADERS` (sem cache, sem referrer,
sem indexação e CSP sem script inline).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute
from markupsafe import Markup
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.admin.actions.jobs import NoPdf, PdfRemoved, pdf_file
from app.admin.auth import instant
from app.consent import CheckboxConsentIn, SubmitConsentIn
from app.deployment_contract import DeploymentContractError
from app.deps import Deps, get_deps
from app.destinations import SecretCipher
from app.errors import RetryableError, TerminalError
from app.forms import FormSpec, InactiveForm, Notices, Screen, UnknownForm
from app.forms.dates import long_date
from app.forms.sandbox import TemplateProblem
from app.handoffs import completion_view
from app.intake import (
    Accepted,
    CaptureAccepted,
    ConsentTextMismatch,
    IdempotencyConflict,
    QueueFull,
    SubmissionIn,
    SubmissionLimitExceeded,
    accept_submission,
    clean_tracking,
    submission_spec,
)
from app.internal_links import ParticipantPdfAccess
from app.models import Artifact, Job, Plan, Submission, utcnow
from app.pdf_availability import PdfAvailability, current_pdf
from app.plan_release import PlanHeld, ReleaseRefused, require_participant_access
from app.submission_identity import resolve_identity
from app.submission_launch import launch_after_commit, launch_capture_after_commit
from app.tokens import TokenError
from app.web.completion import render_completion
from app.web.formview import (
    PRIVACY,
    FormPost,
    conditions_data,
    error_summary,
    form_errors,
    queue_full,
    read_post,
    steps_view,
    too_many_submissions,
)
from app.web.jobview import date_br, file_size, progress
from app.web.templating import templates

ACCESS_COOKIE = "acesso"
PUBLIC_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
STOP_POLLING = 286  # código do HTMX para parar o `hx-trigger="every ..."`
DOWNLOAD_TTL_SECONDS = 60  # a URL do storage é usada na hora, pelo próprio navegador
# Depois de vencido o link, o cookie ainda vive um tempo para a página dizer "link expirado"
# em vez de "não encontramos".
COOKIE_GRACE = timedelta(days=30)

SEND_FAILED = "Não conseguimos enviar o formulário. Confira as respostas e tente de novo."
CONSENT_UPDATED = (
    "Atualizamos o texto sobre o uso dos seus dados. Leia o aviso novamente, confira sua "
    "concordância e envie de novo."
)
ALREADY_SENT = (
    "Você já enviou este formulário com outras respostas. Para salvar estas respostas como "
    "um novo envio, envie de novo."
)

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; object-src 'none'"
)
PUBLIC_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class PublicRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()

        async def with_headers(request: Request) -> Response:
            response = await handler(request)
            response.headers.update(PUBLIC_HEADERS)
            return response

        return with_headers


from app.partial_capture import PartialUnavailable

router = APIRouter(route_class=PublicRoute)
logger = logging.getLogger(__name__)


class _HideLinkTokens(logging.Filter):
    """Omite a query no log do Uvicorn para ocultar credenciais em qualquer codificação.

    Preserva método, caminho e status, sem interpretar ou alterar o request.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) > 2 and isinstance(args[2], str):
            path, separator, _ = args[2].partition("?")
            target = f"{path}?[removido]" if separator else path
            record.args = (*args[:2], target, *args[3:])
        return True


_HIDE_LINK_TOKENS = _HideLinkTokens()


def hide_link_tokens_in_access_log() -> None:
    """Idempotente: chamar na montagem do app."""
    logging.getLogger("uvicorn.access").addFilter(_HIDE_LINK_TOKENS)


def _page(request: Request, name: str, status_code: int = 200, **context: object) -> Response:
    return templates.TemplateResponse(
        request, f"public/{name}.html", dict(context), status_code=status_code
    )


# --- formulário -------------------------------------------------------------------------------


def _screen_form(
    request: Request, slug: str, key: str | None = None, session: Session | None = None
) -> tuple[FormSpec, Screen] | Response:
    """A versão atual do formulário, se ele tem página pública e recebe envios: um envio novo usa
    sempre ela. Senão, a página que diz por quê. Inativo, ele está indisponível (410): a lead sabe
    que o problema não é com ela, e quem enviou sabe que as respostas não foram."""
    try:
        deps = get_deps()
        if key is None:
            spec = deps.forms.current(slug)
        elif session is not None:
            spec = submission_spec(session, deps.forms, slug, key)
        else:
            with deps.sessions() as lookup:
                spec = submission_spec(lookup, deps.forms, slug, key)
    except InactiveForm:
        return _page(request, "unavailable", 410, submitted=request.method == "POST")
    except UnknownForm:
        return _page(request, "not_found", 404)
    if spec.screen is None:
        return _page(request, "not_found", 404)
    return spec, spec.screen


def _form_page(
    request: Request,
    spec: FormSpec,
    screen: Screen,
    post: FormPost | None = None,
    *,
    errors: dict[str, str] | None = None,
    banner: str | None = None,
    tracking: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    """Formulário em branco ou devolvido com erros: as respostas válidas voltam preenchidas (a
    pessoa não perde o que já respondeu); as inválidas, não (nada é repetido de volta)."""
    errors = errors or {}
    steps = steps_view(spec, screen, post.values if post else None, errors)
    return _page(
        request,
        "form",
        status_code,
        spec=spec,
        screen=screen,
        steps=steps,
        conditions_data=conditions_data(spec),
        preview=False,
        summary=error_summary(steps, errors),
        banner=banner,
        consent={
            "privacy": bool(post and post.privacy),
            "marketing_email": bool(post and post.marketing_email),
            "marketing_whatsapp": bool(post and post.marketing_whatsapp),
        },
        consent_error=errors.get(PRIVACY),
        tracking=post.tracking if post else tracking or {},
        idempotency_key=post.idempotency_key if post else str(uuid.uuid4()),
        partial_token=post.partial_token if post else None,
        partial_revision=post.partial_revision if post else 0,
        partial_step=post.partial_step if post else 0,
    )


@router.get("/p/{slug}")
def form_page(slug: str, request: Request) -> Response:
    found = _screen_form(request, slug)
    if isinstance(found, Response):
        return found
    # UTMs do anúncio seguem no envio (a lista permitida está em app.intake).
    return _form_page(request, *found, tracking=clean_tracking(dict(request.query_params)))


def _https(request: Request) -> bool:
    return request.url.scheme == "https" or get_deps().settings.public_base_url.startswith(
        "https://"
    )


def _grant_access(response: Response, request: Request, job: Job, token: str) -> None:
    remaining = max(job.expires_at - utcnow(), timedelta(0))
    response.set_cookie(
        ACCESS_COOKIE,
        token,
        max_age=int((remaining + COOKIE_GRACE).total_seconds()),
        path=f"/d/{job.public_id}",
        secure=_https(request),
        httponly=True,
        samesite="lax",
    )


@router.post("/p/{slug}")
async def submit_form(slug: str, request: Request) -> Response:
    raw = await request.form()
    accepted = await run_in_threadpool(_submit_form, slug, request, raw)
    if isinstance(accepted, Response):
        return accepted
    if isinstance(accepted, CaptureAccepted):
        if not accepted.replay:
            launch_capture_after_commit(request.app, accepted.submission.id)
        response = RedirectResponse(f"/s/{accepted.receipt.public_id}/conclusao", status_code=303)
        _grant_capture_access(response, request, accepted.receipt, accepted.token)
        return response
    job = accepted.job
    if not accepted.replay:
        # Falha no disparo não perde o pedido: o reconciliador dispara depois.
        launch_after_commit(request.app, job.id, job.attempt)
        launch_capture_after_commit(request.app, job.submission_id)
    suffix = "/conclusao" if accepted.form.completion.mode != "result" else ""
    response = RedirectResponse(f"/d/{job.public_id}{suffix}", status_code=303)
    _grant_access(response, request, job, accepted.token)
    return response


def _prepare_form_submission(
    request: Request, spec: FormSpec, screen: Screen, post: FormPost, session: Session
) -> Accepted | CaptureAccepted | Response:
    """Valida e recebe a página sob a mesma trava usada para resolver sua versão."""
    if post.honeypot:
        return _form_page(request, spec, screen, post, banner=SEND_FAILED, status_code=400)
    errors = form_errors(spec, screen, post)
    if errors:
        return _form_page(request, spec, screen, post, errors=errors, status_code=422)
    outdated = replace(post, privacy=False)  # concordou com outro texto: pede de novo
    try:
        declaration = (
            {"method": "submit", "action": "submit"}
            if screen.consent.acquisition_method == "submit"
            else {"method": "checkbox", "action": "confirm", "privacy": post.privacy}
        )
        consent_model = (
            SubmitConsentIn if screen.consent.acquisition_method == "submit" else CheckboxConsentIn
        )
        consent = consent_model(
            **declaration,
            text_version=post.consent_version,
            marketing_email=post.marketing_email,
            marketing_whatsapp=post.marketing_whatsapp,
        )
    except ValidationError:  # versão ausente ou adulterada
        return _form_page(request, spec, screen, outdated, banner=CONSENT_UPDATED, status_code=409)
    body = SubmissionIn(
        answers=post.answers,
        consent=consent,
        tracking=post.tracking,
        partial_token=post.partial_token,
    )
    deps = get_deps()

    try:
        accepted = accept_submission(
            session,
            spec,
            body,
            idempotency_key=f"web:{post.idempotency_key}",
            signer=deps.signer,
            link_ttl=deps.link_ttl,
            limits=deps.submission_limits,
            forms=deps.forms,
            cipher=SecretCipher.from_settings(deps.settings),
            source="public_html",
        )
    except ValidationError as exc:
        # As respostas já passaram por `form_errors`: sobra o canal de marketing que a página não
        # pede (página velha ou envio forjado). O erro vai no campo do consentimento.
        message = exc.errors(include_input=False, include_url=False)[0]["msg"]
        return _form_page(request, spec, screen, post, errors={PRIVACY: message}, status_code=422)
    except ConsentTextMismatch:
        return _form_page(request, spec, screen, outdated, banner=CONSENT_UPDATED, status_code=409)
    except IdempotencyConflict:  # o mesmo formulário voltou com outras respostas
        fresh = replace(post, idempotency_key=str(uuid.uuid4()))
        return _form_page(request, spec, screen, fresh, banner=ALREADY_SENT, status_code=409)
    except SubmissionLimitExceeded as exc:
        # A mesma tela, com as respostas e a chave do envio: ela reenvia quando a janela abrir.
        limits = deps.submission_limits
        banner = too_many_submissions(limits.per_email, limits.window, exc.retry_after)
        response = _form_page(request, spec, screen, post, banner=banner, status_code=429)
        response.headers["Retry-After"] = str(exc.retry_after)
        return response
    except QueueFull as exc:
        response = _form_page(
            request, spec, screen, post, banner=queue_full(exc.retry_after), status_code=503
        )
        response.headers["Retry-After"] = str(exc.retry_after)
        return response
    return accepted


def _submit_form(
    slug: str, request: Request, raw: FormData
) -> Accepted | CaptureAccepted | Response:
    deps = get_deps()
    spec: FormSpec | None = None
    screen: Screen | None = None
    post: FormPost | None = None
    try:
        with deps.sessions.begin() as session:
            if raw.get("_partial_token"):
                wait = request.app.state.partial_limiter.take(
                    request.client.host if request.client else "unknown"
                )
                if wait:
                    return Response(status_code=429, headers={"Retry-After": str(wait)})
                from app.partial_capture import lookup

                partial = lookup(session, slug, str(raw["_partial_token"]), allow_completed=True)
                historical = deps.forms.get(slug, partial.form_version_number)
                found = historical, historical.screen
            else:
                found = _screen_form(request, slug, f"web:{raw.get('_idem', '')}", session)
            if isinstance(found, Response):
                return found
            spec, screen = found
            post = read_post(screen, raw, spec.conditions)
            if post.partial_token:
                post = replace(post, partial_revision=partial.revision, partial_step=partial.step)
            accepted = _prepare_form_submission(request, spec, screen, post, session)
    except PartialUnavailable:
        return _page(request, "unavailable", 404, submitted=False)
    except sa.exc.SQLAlchemyError:
        # Também cobre falha no commit: nenhum recibo ou disparo antecede sua confirmação.
        # Não consulta o banco indisponível de novo nem repete respostas ainda não validadas.
        if spec is None or screen is None or post is None:
            response = _page(request, "submission_unavailable", 503, slug=slug)
        else:
            response = _form_page(request, spec, screen, post, banner=SEND_FAILED, status_code=503)
        response.headers["Retry-After"] = "5"
        return response
    return accepted


# --- página do pedido -------------------------------------------------------------------------


@dataclass(frozen=True)
class Access:
    """Conferência do token. Sem assinatura válida, `job` é None: a resposta é a mesma para
    código inexistente e token errado (não revela quais pedidos existem)."""

    job: Job | None
    expired: bool = False


def _access(deps: Deps, public_id: str, token: str | None) -> Access:
    if not token or not PUBLIC_ID_RE.fullmatch(public_id):
        return Access(None)
    with deps.sessions() as session:
        job = session.scalar(sa.select(Job).where(Job.public_id == public_id))
    if job is None:
        return Access(None)
    try:
        deps.signer.verify(token, job)
    except TokenError as exc:
        return Access(job, expired=True) if exc.reason == "expired" else Access(None)
    return Access(job)


def _lead_job(request: Request, deps: Deps, public_id: str) -> Job | Response:
    """O job do cookie, ou a página para quem não pode vê-lo (inválido: 404; vencido: 410)."""
    access = _access(deps, public_id, request.cookies.get(ACCESS_COOKIE))
    if access.job is None:
        return _page(request, "unknown", 404)
    if access.expired:
        return _page(request, "expired", 410, job=access.job, days=deps.settings.link_ttl_days)
    return access.job


def _exchange(
    request: Request, deps: Deps, public_id: str, token: str, *, suffix: str = ""
) -> Response:
    """Link com `?t=`: troca o token pelo cookie e volta para a URL limpa, para o token não
    ficar na barra, no histórico nem no Referer. Token errado não ganha nada."""
    response = RedirectResponse(f"/d/{public_id}{suffix}", status_code=303)
    access = _access(deps, public_id, token)
    if access.job is not None:  # válido ou vencido: a página certa vem depois do redirect
        _grant_access(response, request, access.job, token)
    return response


def _current_pdf(session: Session, job: Job) -> Artifact | None:
    return session.scalar(
        sa.select(Artifact).where(
            Artifact.job_id == job.id, Artifact.kind == "pdf", Artifact.version == job.attempt
        )
    )


@dataclass(frozen=True)
class JobDetails:
    slug: str
    version_number: int  # a versão do formulário que a lead respondeu
    first_name: str | None
    plan: dict[str, Any] | None = None  # só com o pedido concluído
    pdf: Artifact | None = None
    # O que a prévia de uma definição compilada recebe, como o PDF.
    lead_name: str | None = None
    scores: dict[str, Any] = field(default_factory=dict)
    availability: PdfAvailability | None = None
    held: bool = False


def _details(deps: Deps, job: Job) -> JobDetails:
    with deps.sessions.begin() as session:
        from app.integration_readiness import configuration_lock

        configuration_lock(session, shared=True)
        observed = session.scalar(sa.select(Job).where(Job.id == job.id).with_for_update())
        if (
            observed is None
            or observed.attempt != job.attempt
            or observed.token_version != job.token_version
        ):
            raise NoPdf
        submission = session.get_one(Submission, job.submission_id)
        try:
            lead_name = resolve_identity(deps.forms, submission).name
        except TerminalError:  # a página segue acessível, sem nome presumido de outro envio
            lead_name = None
        plan = pdf = None
        availability = current_pdf(session, deps.storage, job)
        try:
            access = require_participant_access(session, job, _current_pdf(session, job))
            held = not access.allowed
        except (ReleaseRefused, DeploymentContractError):
            held = True
        if availability.available and not held:
            plan = session.scalar(
                sa.select(Plan.document).where(Plan.job_id == job.id, Plan.version == job.attempt)
            )
            pdf = _current_pdf(session, job)
    names = (lead_name or "").split()
    first_name = names[0] if names else None
    return JobDetails(
        submission.form_slug,
        submission.form_version_number,
        first_name,
        plan,
        pdf,
        lead_name=lead_name,
        scores=submission.scores or {},
        availability=availability,
        held=held,
    )


def _notices(deps: Deps, details: JobDetails) -> Notices:
    """Os avisos extras das páginas de espera e de falha, na versão do formulário que a lead
    respondeu. Se o formulário saiu do registro, as páginas saem sem eles."""
    try:
        spec = deps.forms.get(details.slug, details.version_number)
    except UnknownForm:
        return Notices()
    return spec.screen.notices if spec.screen is not None else Notices()


def _preview(deps: Deps, details: JobDetails) -> Markup | None:
    """A prévia do plano na versão do formulário que a lead respondeu, com o contexto do PDF. Se
    ela falha, a página sai sem a prévia, e a lead ainda baixa o PDF; o log leva só o template e a
    linha."""
    if details.plan is None:
        return None
    try:
        spec = deps.forms.get(details.slug, details.version_number)
    except UnknownForm:  # o formulário saiu do registro: o resultado aparece sem a prévia
        return None
    plan = details.plan
    generated = details.pdf.created_at if details.pdf is not None else utcnow()
    try:
        html = spec.templates.render_preview(
            plan,
            lead={"name": details.lead_name},
            form={"slug": spec.slug, "title": spec.title},
            scores=details.scores,
            generated_date=long_date(generated),
        )
    except TemplateProblem as exc:
        logger.warning("prévia de %s v%s falhou: %s", spec.slug, spec.version_number, exc)
        return None
    return Markup(html)


@router.get("/d/{public_id}/conclusao")
def completion_page(public_id: str, request: Request) -> Response:
    deps = get_deps()
    token = request.query_params.get("t")
    if token is not None:
        return _exchange(request, deps, public_id, token, suffix="/conclusao")
    job = _lead_job(request, deps, public_id)
    if isinstance(job, Response):
        return job
    with deps.sessions() as session:
        submission = session.get_one(Submission, job.submission_id)
        try:
            spec = deps.forms.get(submission.form_slug, submission.form_version_number)
        except UnknownForm:
            return _page(request, "unknown", 404)
        completion = completion_view(
            session,
            submission,
            spec,
            cipher=SecretCipher.from_settings(deps.settings),
            now=instant(request.app.state.admin_clock),
        )
    if spec.completion.mode == "result":
        return RedirectResponse(f"/d/{job.public_id}", status_code=303)
    from fastapi.responses import HTMLResponse

    return HTMLResponse(render_completion(spec, completion=completion))


@router.get("/d/{public_id}")
def job_page(public_id: str, request: Request) -> Response:
    deps = get_deps()
    token = request.query_params.get("t")
    if token is not None:
        return _exchange(request, deps, public_id, token)
    job = _lead_job(request, deps, public_id)
    if isinstance(job, Response):
        return job
    try:
        details = _details(deps, job)
    except NoPdf:
        return _page(request, "plan_held", 409, job=job)
    if job.status == "failed":
        return _page(
            request, "failed", job=job, details=details, notice=_notices(deps, details).failed
        )
    if job.status != "completed":
        return _page(
            request,
            "waiting",
            job=job,
            details=details,
            progress=progress(job, utcnow()),
            notice=_notices(deps, details).waiting,
        )
    if details.held:
        return _page(request, "plan_held", job=job)
    if details.pdf is None:
        return _page(
            request,
            "pdf_unavailable",
            job=job,
            can_verify=details.availability is not None and details.availability.state == "unknown",
        )
    return _page(
        request,
        "result",
        job=job,
        details=details,
        preview=_preview(deps, details),
        valid_until=date_br(job.expires_at),
        generated_on=date_br(details.pdf.created_at) if details.pdf else None,
        size=file_size(details.pdf.byte_size) if details.pdf else None,
    )


@router.get("/d/{public_id}/pdf")
def download_pdf(public_id: str, request: Request) -> Response:
    """Baixar o PDF com o cookie: URL temporária do storage (302) ou o próprio arquivo."""
    deps = get_deps()
    job = _lead_job(request, deps, public_id)
    if isinstance(job, Response):
        return job
    if job.status != "completed":
        return RedirectResponse(f"/d/{job.public_id}", status_code=303)
    try:
        pdf = pdf_file(
            deps,
            public_id,
            None,
            signed_url=True,
            filename=f"plano-{job.public_id}.pdf",
            access=ParticipantPdfAccess(job.id, job.attempt, job.token_version),
        )
    except TokenError:
        return _page(request, "expired", 410, job=job, days=deps.settings.link_ttl_days)
    except PlanHeld:
        return _page(request, "plan_held", 403, job=job)
    except (ReleaseRefused, DeploymentContractError):
        return _page(request, "plan_held", 503, job=job)
    except NoPdf:
        return _page(request, "pdf_unavailable", 404, job=job)
    except PdfRemoved:
        return _page(request, "pdf_unavailable", 410, job=job)
    except (RetryableError, TerminalError, OSError):
        return _page(request, "pdf_unavailable", 502, job=job)
    if pdf.url:
        return RedirectResponse(pdf.url, status_code=302)
    return Response(
        pdf.content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="plano-{job.public_id}.pdf"'},
    )


@router.get("/d/{public_id}/progresso")
def job_progress(public_id: str, request: Request) -> Response:
    """Trecho trocado pelo HTMX na espera. Terminou (ou perdeu o acesso): 286 para o polling e
    `HX-Refresh` recarrega a página, que mostra o estado final."""
    access = _access(get_deps(), public_id, request.cookies.get(ACCESS_COOKIE))
    job = access.job
    if job is None or access.expired or job.status not in ("queued", "running"):
        response = _page(request, "progress_done", STOP_POLLING, public_id=public_id)
        response.headers["HX-Refresh"] = "true"
        return response
    return _page(
        request,
        "progress",
        job=job,
        progress=progress(job, utcnow()),
        announce=request.query_params.get("etapa") != job.stage,
    )


CAPTURE_COOKIE = "leadkit_capture"


def _grant_capture_access(response, request, receipt, token):
    response.set_cookie(
        CAPTURE_COOKIE,
        token,
        max_age=max(0, int((receipt.expires_at - utcnow()).total_seconds())),
        path=f"/s/{receipt.public_id}",
        secure=_https(request),
        httponly=True,
        samesite="lax",
    )


@router.get("/s/{public_id}/conclusao")
def capture_completion_page(public_id: str, request: Request) -> Response:
    """Recibo histórico sem respostas, contato ou acesso a PDF; troca token por cookie local."""
    from fastapi.responses import HTMLResponse

    from app.models import CaptureReceipt
    from app.tokens import CaptureSigner, TokenError

    deps = get_deps()
    token = request.query_params.get("t")
    supplied = token if token is not None else request.cookies.get(CAPTURE_COOKIE, "")
    with deps.sessions() as session:
        receipt = session.scalar(
            sa.select(CaptureReceipt).where(CaptureReceipt.public_id == public_id)
        )
        if receipt is None:
            return _page(request, "capture_unknown", 404)
        try:
            CaptureSigner(deps.signer.secrets_).verify(supplied, receipt)
        except TokenError:
            return _page(request, "capture_unknown", 404)
        submission = session.get_one(Submission, receipt.submission_id)
        try:
            spec = deps.forms.get(submission.form_slug, submission.form_version_number)
        except UnknownForm:
            return _page(request, "capture_unknown", 404)
        completion = completion_view(
            session,
            submission,
            spec,
            cipher=SecretCipher.from_settings(deps.settings),
            now=instant(request.app.state.admin_clock),
        )
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    if token is not None:
        response = RedirectResponse(f"/s/{public_id}/conclusao", status_code=303, headers=headers)
        _grant_capture_access(response, request, receipt, token)
        return response
    return HTMLResponse(render_completion(spec, completion=completion), headers=headers)


@router.post("/p/{slug}/retomar")
async def resume_partial_page(slug: str, request: Request) -> Response:
    """Troca o fragmento privado pelo formulário histórico, sem redirecionar a capacidade."""
    from starlette.concurrency import run_in_threadpool

    from app.partial_capture import lookup

    wait = request.app.state.partial_limiter.take(
        request.client.host if request.client else "unknown"
    )
    if wait:
        return Response(status_code=429, headers={"Retry-After": str(wait)})
    raw = await request.form()
    token = raw.get("token")
    if not isinstance(token, str) or not 1 <= len(token) <= 100:
        return _page(request, "unavailable", 404, submitted=False)

    def resume():
        deps = get_deps()
        with deps.sessions.begin() as session:
            row = lookup(session, slug, token)
            spec = deps.forms.get(slug, row.form_version_number)
            post = FormPost(
                answers=row.answers,
                values={
                    k: [str(v) for v in value] if isinstance(value, list) else [str(value)]
                    for k, value in row.answers.items()
                    if value is not None
                },
                privacy=True,
                marketing_email=row.consent.get("marketing_email", False),
                marketing_whatsapp=row.consent.get("marketing_whatsapp", False),
                consent_version=spec.consent_text_version,
                idempotency_key=str(uuid.uuid4()),
                tracking=row.tracking,
                honeypot=False,
                partial_token=token,
                partial_revision=row.revision,
                partial_step=row.step,
            )
            return _form_page(request, spec, spec.screen, post)

    try:
        return await run_in_threadpool(resume)
    except (PartialUnavailable, UnknownForm):
        return _page(request, "unavailable", 404, submitted=False)
    except sa.exc.SQLAlchemyError:
        return _page(request, "submission_unavailable", 503, slug=slug)
