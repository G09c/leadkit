"""API pública: receber submissões, acompanhar o status e baixar o PDF.

`uvicorn app.api.main:app`. Handlers síncronos rodam no threadpool do Starlette (acesso a banco
é síncrono); só o disparo no Hatchet é assíncrono e tem limite de tempo.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl

import sqlalchemy as sa
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel, ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import admin, destinations, jobs, pipeline
from app.admin.actions.jobs import NoPdf, PdfRemoved, pdf_file
from app.admin.api import errors as admin_api_errors
from app.admin.api.application import create_admin_api
from app.admin.api.schemas.jobs import CurrentPdf
from app.admin.auth import instant
from app.deployment_contract import DeploymentContractError, assert_contract
from app.deps import Deps, get_deps
from app.errors import RetryableError, TerminalError
from app.forms import FormSpec, InactiveForm, UnknownForm
from app.forms.data_notices import log_data_notices
from app.forms.image_optimizer import MAX_UPLOAD_BYTES
from app.intake import (
    CaptureAccepted,
    ConsentTextMismatch,
    IdempotencyConflict,
    QueueFull,
    SubmissionIn,
    SubmissionLimitExceeded,
    accept_submission,
    submission_spec,
)
from app.integrations import config as integration_config
from app.internal_links import ParticipantPdfAccess
from app.models import Artifact, Job
from app.partial_capture import PartialUnavailable
from app.pdf_availability import availability_dict, current_pdf
from app.plan_release import PlanHeld, ReleaseRefused, participant_access
from app.submission_launch import launch_after_commit, launch_capture_after_commit, stop_launches
from app.tokens import TokenError
from app.web import router as web_router
from app.web.images import router as images_router
from app.web.public import hide_link_tokens_in_access_log
from app.web.templating import STATIC_DIR
from app.workflows import alaunch_draft_test, alaunch_generate_plan

MAX_BODY_BYTES = 256 * 1024
# O envio e a troca de uma imagem de formulário são um corpo com o arquivo (até MAX_UPLOAD_BYTES) e uns
# campos: só estes (método e caminho) aceitam um corpo desse tamanho. Na página, os dois são POST
# (`/admin/assets` e `/admin/assets/<id>/replace`); na API de admin, `upload_asset` é POST em
# `/assets` e `replace_asset`, PUT em `/assets/<id>/content`. Outro método no mesmo caminho, ou outro
# caminho sob `/assets` (a remoção, a leitura), segue com o limite comum.
_ASSET_ID = r"[^/]+"
_API_ASSETS = re.escape(f"{admin_api_errors.PREFIX}/assets")
UPLOAD_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(r"/admin/assets")),
    ("POST", re.compile(rf"/admin/assets/{_ASSET_ID}/replace")),
    ("POST", re.compile(_API_ASSETS)),
    ("PUT", re.compile(rf"{_API_ASSETS}/{_ASSET_ID}/content")),
)
MAX_UPLOAD_BODY_BYTES = MAX_UPLOAD_BYTES + 256 * 1024
PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

logger = logging.getLogger(__name__)

Trigger = Callable[[uuid.UUID, int], Awaitable[str | None]]


class SubmissionCompletion(BaseModel):
    """Próximo passo pós-commit. Com opt-in, a URL inclui somente uma referência opaca autorizada."""

    mode: Literal["result", "confirmation", "button", "redirect"]
    message: str
    button_label: str
    url: str | None
    handoff_status: Literal["not_configured", "ready", "unavailable"] = "not_configured"
    receipt_url: str


class PublicReleaseStatus(BaseModel):
    policy: Literal["automatic", "manual", "unavailable"]
    state: Literal["automatic", "held", "released", "unavailable"]


class SubmissionAccepted(BaseModel):
    release: PublicReleaseStatus
    """Pedido persistido; geração e conclusão são independentes."""

    public_id: str
    token: str
    status: str
    replay: bool
    status_url: str
    pdf_url: str | None
    page_url: str
    completion: SubmissionCompletion


class CaptureSubmissionAccepted(BaseModel):
    """Captura confirmada: capacidade do recibo, sem acesso ou estados de PDF."""

    kind: Literal["capture"] = "capture"
    status: Literal["captured"] = "captured"
    submission_id: str
    receipt_token: str
    receipt_url: str
    replay: bool
    completion: SubmissionCompletion


class PublicJobStatus(BaseModel):
    release: PublicReleaseStatus
    """Estado autenticado; disponibilidade técnica não concede nova autorização."""

    public_id: str
    status: str
    stage: str
    attempt: int
    created_at: AwareDatetime
    completed_at: AwareDatetime | None
    expires_at: AwareDatetime
    current_pdf: CurrentPdf
    pdf_url: str | None = None
    failure: dict[str, str | None] | None = None


# Dispara o teste do rascunho (`app.forms.drafts`) pelo id; None: o engine não recebeu.
TestTrigger = Callable[[uuid.UUID], Awaitable[str | None]]


def _prepare_destinations() -> None:
    """O destino do webhook e os tokens das integrações na subida (`app.destinations.prepare` e
    `app.integrations.config.prepare`). Configuração que impede a subida (`StartupError`) derruba a
    API. O piso de compatibilidade precisa ser lido antes de qualquer preparação: banco
    indisponível nessa conferência também recusa a subida."""
    try:
        from app.deployment_contract import assert_runtime_contract

        with get_deps().sessions() as session:
            assert_runtime_contract(session)
        destinations.prepare(get_deps())
        integration_config.prepare(get_deps())
    except sa.exc.OperationalError:
        logger.warning("destino do webhook: banco fora do ar na subida da API", exc_info=True)


def _log_data_notices(now: datetime) -> None:
    """Os avisos dos dados vencidos dos formulários no log da subida (`app.forms.data_notices`).
    Nenhum erro do banco derruba a API: com o Postgres fora do ar, ou sem as tabelas (no primeiro
    deploy da stack do Swarm ela sobe antes das migrations), ela sobe sem os avisos."""
    try:
        log_data_notices(get_deps().sessions, now)
    except sa.exc.SQLAlchemyError:
        logger.warning(
            "avisos dos formulários: o banco não respondeu na subida da API (fora do ar ou antes "
            "das migrations); a API sobe sem os avisos",
            exc_info=True,
        )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    await run_in_threadpool(_prepare_destinations)
    # O relógio do app (o mesmo da sessão do admin) diz o dia contra o qual os dados dos formulários
    # são conferidos; os testes o trocam.
    await run_in_threadpool(_log_data_notices, instant(app.state.admin_clock))
    try:
        yield
    finally:
        await stop_launches(app)


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


# Os caracteres de controle: o NUL, a quebra de linha, o tabulador e o DEL entre eles.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _control_in_query(scope: Scope) -> bool:
    """Se o nome ou o valor de algum parâmetro da query, já decodificado, tem caractere de controle."""
    query = scope.get("query_string", b"").decode("latin-1")
    return any(
        _CONTROL.search(name) or _CONTROL.search(value)
        for name, value in parse_qsl(query, keep_blank_values=True)
    )


class ControlCharacters:
    """Um caractere de controle (o NUL, uma quebra de linha...) no caminho ou na query.

    - **No caminho** não é o endereço de nada: nenhum slug, código ou id tem um, e o Postgres recusa o
      NUL num texto, então um `%00` num parâmetro do caminho chegava ao banco e saía como `500`. É
      `404`, antes de qualquer rota e sem repetir o caminho, na API pública, nas páginas do admin e em
      toda operação da API de admin (uma operação nova não precisa lembrar de nada). Sob a API de
      admin, a resposta é a de um endereço sem operação, no formato de erro dela.
    - **Na query** o valor ia da busca (`?q=%00`) ao banco do mesmo jeito. Nas páginas do admin e na API
      pública é `400`, sem repetir o valor. Na API de admin não é daqui: a recusa é `422 validation_failed`
      no campo do parâmetro, depois da chave, do papel e da confirmação, como as outras de parâmetro
      (`app.admin.api.operations.plain_query`)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            under_api = _under(path, admin_api_errors.PREFIX)
            refusal: Response | None = None
            if _CONTROL.search(path):
                if under_api:
                    refusal = admin_api_errors.error_response(
                        admin_api_errors.ApiError("not_found", admin_api_errors.NO_OPERATION)
                    )
                else:
                    refusal = JSONResponse(
                        {"detail": "não encontrado"}, status_code=404, headers=PRIVATE_HEADERS
                    )
            elif not under_api and _control_in_query(scope):
                refusal = JSONResponse(
                    {"detail": "a consulta tem um caractere de controle"},
                    status_code=400,
                    headers=PRIVATE_HEADERS,
                )
            if refusal is not None:
                await refusal(scope, receive, send)
                return
        await self.app(scope, receive, send)


class BodySizeLimit:
    """Recusa corpos acima do limite contando os bytes recebidos (não confia em Content-Length).
    Os envios de `upload_routes` (método e caminho, ver `UPLOAD_ROUTES`) têm o limite `upload_bytes`,
    o dos arquivos de imagem do admin. Sob a API de admin, a recusa sai no formato de erro dela; no
    resto, `{"detail": ...}`."""

    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int,
        *,
        upload_routes: tuple[tuple[str, re.Pattern[str]], ...] = (),
        upload_bytes: int = 0,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.upload_routes = upload_routes
        self.upload_bytes = upload_bytes

    def _limit(self, scope: Scope) -> int:
        method, path = scope.get("method"), scope.get("path", "")
        is_upload = any(
            method == upload_method and pattern.fullmatch(path)
            for upload_method, pattern in self.upload_routes
        )
        return max(self.max_bytes, self.upload_bytes) if is_upload else self.max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            if _under(scope.get("path", ""), admin_api_errors.PREFIX):
                refusal = admin_api_errors.too_large_response()
            else:
                refusal = JSONResponse({"detail": "corpo muito grande"}, status_code=413)
            await refusal(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=413, detail="corpo muito grande")
            return message

        await self.app(scope, limited_receive, send)


def _validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    # Sem o valor recebido: as respostas do lead não voltam em mensagens de erro.
    return [
        {"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
        for error in exc.errors(include_input=False, include_url=False)
    ]


def _refusal(status_code: int, code: str, detail: str, retry_after: int) -> JSONResponse:
    """429 ou 503 de um limite de envio: o `Retry-After` diz quando tentar de novo."""
    return JSONResponse(
        {"code": code, "detail": detail},
        status_code=status_code,
        headers={**PRIVATE_HEADERS, "Retry-After": str(retry_after)},
    )


def _token_from(authorization: str | None, query_token: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    if query_token:
        return query_token
    raise HTTPException(status_code=401, detail="link inválido", headers=PRIVATE_HEADERS)


def _authorized_job(deps: Deps, public_id: str, token: str) -> Job:
    with deps.sessions() as session:
        job = session.scalar(sa.select(Job).where(Job.public_id == public_id))
    try:
        if job is None:
            raise TokenError("invalid")
        deps.signer.verify(token, job)
    except TokenError as exc:
        status = 410 if exc.reason == "expired" else 401
        message = "link expirado" if exc.reason == "expired" else "link inválido"
        raise HTTPException(status_code=status, detail=message, headers=PRIVATE_HEADERS) from None
    return job


def create_app(
    *,
    trigger: Trigger | None = None,
    test_trigger: TestTrigger | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """`trigger` dispara o `generate_plan`, e `test_trigger`, o teste do rascunho; `clock` (epoch
    em segundos) mede a sessão e o bloqueio de login do admin, o intervalo entre os testes do
    rascunho e o dia dos avisos de dados vencidos dos formulários. Os testes trocam os três."""
    hide_link_tokens_in_access_log()
    settings = get_deps().settings
    production = settings.app_env == "production"
    app = FastAPI(
        title="Leadkit",
        docs_url=None if production else "/docs",
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
        lifespan=_lifespan,
    )
    app.add_middleware(
        BodySizeLimit,
        max_bytes=MAX_BODY_BYTES,
        upload_routes=UPLOAD_ROUTES,
        upload_bytes=MAX_UPLOAD_BODY_BYTES,
    )
    app.add_middleware(ControlCharacters)  # a mais externa: recusa antes de tudo
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    from app.admin.api.limits import RateLimiter
    from app.api.partials import router as partial_router

    app.state.partial_limiter = RateLimiter(30, clock)
    app.include_router(partial_router)
    app.include_router(web_router)
    app.include_router(images_router)
    admin.install(app, settings, clock=clock)

    async def default_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return await alaunch_generate_plan(
            job_id, attempt, timeout=settings.hatchet_trigger_timeout_seconds
        )

    async def default_test_trigger(test_id: uuid.UUID) -> str | None:
        return await alaunch_draft_test(test_id, timeout=settings.hatchet_trigger_timeout_seconds)

    app.state.trigger = trigger or default_trigger
    app.state.submission_launches = set()
    app.state.test_trigger = test_trigger or default_test_trigger
    # A API de admin é uma sub-aplicação montada, com o OpenAPI, os erros e a autenticação dela: o
    # formato de erro das páginas e o da API pública não mudam. Recebe o relógio e os disparadores
    # do app. `app.state.admin_api` dá aos testes (e ao gerador do OpenAPI) a sub-aplicação.
    app.state.admin_api = create_admin_api(
        clock=clock,
        trigger=app.state.trigger,
        test_trigger=app.state.test_trigger,
        rate_limit_per_minute=settings.api_rate_limit_per_minute,
        # A referência (`/reference` e `/openapi.json`) é a única parte que lê a sessão do admin, que
        # a sub-aplicação não enxerga, e abre sem login em `development` e `test`.
        admin_auth=app.state.admin_auth,
        environment=settings.app_env,
    )
    app.mount(admin_api_errors.PREFIX, app.state.admin_api, name="admin-api")
    from app.api.handoffs import create_integration_api

    app.state.integration_api = create_integration_api(clock=app.state.admin_clock)
    app.mount("/integrations/v1", app.state.integration_api, name="integration-api")
    app.router.add_route(
        admin_api_errors.PREFIX, admin_api_errors.BarePrefix(), include_in_schema=False
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        try:
            with get_deps().sessions() as session:
                session.execute(sa.text("select 1"))
        except (sa.exc.SQLAlchemyError, OSError):
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.post(
        "/v1/forms/{slug}/submissions",
        status_code=202,
        response_model=SubmissionAccepted | CaptureSubmissionAccepted,
    )
    async def submit(
        slug: str,
        request: Request,
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ) -> JSONResponse:
        deps = get_deps()
        key = idempotency_key or f"auto:{uuid.uuid4()}"
        try:
            # Um envio novo usa sempre a versão atual. A consulta vai ao banco: fora do event loop.
            def _spec() -> FormSpec:
                with deps.sessions() as session:
                    return submission_spec(session, deps.forms, slug, key)

            await run_in_threadpool(_spec)
        except InactiveForm:  # desativado no admin: não recebe envios novos
            return JSONResponse(
                {"code": "form_unavailable", "detail": "formulário indisponível"}, status_code=410
            )
        except UnknownForm:
            raise HTTPException(status_code=404, detail="formulário não encontrado") from None
        except sa.exc.SQLAlchemyError:
            return _refusal(
                503, "unavailable", "não foi possível confirmar o envio; tente de novo", 5
            )
        try:
            body = SubmissionIn.model_validate(await request.json())
        except ValidationError as exc:
            return JSONResponse({"errors": _validation_errors(exc)}, status_code=422)
        except ValueError:
            return JSONResponse(
                {"errors": [{"field": "", "message": "JSON inválido"}]}, status_code=422
            )

        if body.partial_token:
            wait = request.app.state.partial_limiter.take(
                request.client.host if request.client else "unknown"
            )
            if wait:
                return JSONResponse(
                    {"code": "rate_limited"}, status_code=429, headers={"Retry-After": str(wait)}
                )

        def _accept() -> Any:
            with deps.sessions.begin() as session:
                # Conserva a trava da chave da resolução da versão até confirmar o envio.
                resolved = submission_spec(session, deps.forms, slug, key)
                accepted = accept_submission(
                    session,
                    resolved,
                    body,
                    idempotency_key=key,
                    signer=deps.signer,
                    link_ttl=deps.link_ttl,
                    limits=deps.submission_limits,
                    forms=deps.forms,
                    cipher=destinations.SecretCipher.from_settings(deps.settings),
                )
                return accepted

        try:
            accepted = await run_in_threadpool(_accept)
        except InactiveForm:
            return JSONResponse(
                {"code": "form_unavailable", "detail": "formulário indisponível"}, status_code=410
            )
        except UnknownForm:
            raise HTTPException(status_code=404, detail="formulário não encontrado") from None
        except sa.exc.SQLAlchemyError:
            return _refusal(
                503, "unavailable", "não foi possível confirmar o envio; tente de novo", 5
            )
        except ValidationError as exc:
            return JSONResponse({"errors": _validation_errors(exc)}, status_code=422)
        except ConsentTextMismatch as exc:
            return JSONResponse(
                {"code": "consent_text_outdated", "current_version": str(exc)}, status_code=409
            )
        except PartialUnavailable:
            return JSONResponse({"code": "partial_unavailable"}, status_code=404)
        except IdempotencyConflict:
            return JSONResponse({"code": "idempotency_conflict"}, status_code=409)
        except SubmissionLimitExceeded as exc:
            return _refusal(
                429,
                "submission_limit_exceeded",
                "limite de envios por e-mail atingido neste formulário",
                exc.retry_after,
            )
        except QueueFull as exc:
            return _refusal(
                503, "queue_full", "há pedidos demais em andamento; tente de novo", exc.retry_after
            )

        from app.web.completion import accepted_completion

        completion = await run_in_threadpool(
            accepted_completion, deps, accepted, now=instant(request.app.state.admin_clock)
        )
        if isinstance(accepted, CaptureAccepted):
            if not accepted.replay:
                launch_capture_after_commit(request.app, accepted.submission.id)
            receipt_url = f"{deps.settings.public_base_url.rstrip('/')}/s/{accepted.receipt.public_id}/conclusao?t={accepted.token}"
            return JSONResponse(
                {
                    "kind": "capture",
                    "status": "captured",
                    "submission_id": str(accepted.submission.id),
                    "receipt_token": accepted.token,
                    "receipt_url": receipt_url,
                    "replay": accepted.replay,
                    "completion": {**vars(completion), "receipt_url": receipt_url},
                },
                status_code=202,
                headers=PRIVATE_HEADERS,
            )
        job_id, attempt, public_id = accepted.job.id, accepted.job.attempt, accepted.job.public_id
        if not accepted.replay:
            launch_after_commit(request.app, job_id, attempt)
            launch_capture_after_commit(request.app, accepted.job.submission_id)
        links = pipeline.lead_links(deps, accepted.job)
        with deps.sessions() as session:
            artifact = session.scalar(
                sa.select(Artifact).where(
                    Artifact.job_id == accepted.job.id,
                    Artifact.version == accepted.job.attempt,
                    Artifact.kind == "pdf",
                )
            )
            access = participant_access(session, accepted.job, artifact)
        if not access.allowed:
            links["pdf_url"] = None
        return JSONResponse(
            {
                "public_id": public_id,
                "release": {"policy": access.policy, "state": access.state},
                "token": accepted.token,
                "status": accepted.job.status,
                "replay": accepted.replay,
                **links,
                "completion": {
                    **vars(completion),
                    "receipt_url": links["page_url"].replace("?t=", "/conclusao?t="),
                },
            },
            status_code=202,
            headers=PRIVATE_HEADERS,
        )

    @app.get("/v1/jobs/{public_id}", response_model=PublicJobStatus)
    def job_status(
        public_id: str,
        authorization: Annotated[str | None, Header()] = None,
        t: Annotated[str | None, Query(max_length=128)] = None,
    ) -> JSONResponse:
        deps = get_deps()
        job = _authorized_job(deps, public_id, _token_from(authorization, t))
        status = jobs.public_status(job)
        body: dict[str, Any] = {
            "public_id": job.public_id,
            "status": status,
            "stage": job.stage,
            "created_at": job.created_at.isoformat(),
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "expires_at": job.expires_at.isoformat(),
        }
        with deps.sessions() as session:
            availability = current_pdf(session, deps.storage, job)
            artifact = session.scalar(
                sa.select(Artifact).where(
                    Artifact.job_id == job.id,
                    Artifact.version == job.attempt,
                    Artifact.kind == "pdf",
                )
            )
            try:
                access = participant_access(session, job, artifact)
            except ReleaseRefused:
                return JSONResponse(
                    {"code": "access_unavailable"}, status_code=503, headers=PRIVATE_HEADERS
                )
            body["release"] = {"policy": access.policy, "state": access.state}
            allowed = access.allowed
            if access.policy == "manual":
                try:
                    assert_contract(session, required=2)
                except DeploymentContractError:
                    allowed = False
        body["attempt"] = job.attempt
        body["current_pdf"] = availability_dict(availability)
        if availability.available and allowed:
            body["pdf_url"] = pipeline.lead_links(deps, job)["pdf_url"]
        if status == "failed":
            body["failure"] = {"code": job.failure_code}
        return JSONResponse(body, headers=PRIVATE_HEADERS)

    @app.get("/v1/jobs/{public_id}/pdf")
    def job_pdf(
        public_id: str,
        attempt: Annotated[int | None, Query(ge=1)] = None,
        authorization: Annotated[str | None, Header()] = None,
        t: Annotated[str | None, Query(max_length=128)] = None,
    ) -> Response:
        deps = get_deps()
        job = _authorized_job(deps, public_id, _token_from(authorization, t))
        if attempt is not None and job.attempt != attempt:
            raise HTTPException(409, "tentativa desatualizada", headers=PRIVATE_HEADERS)
        if job.status != "completed":
            raise HTTPException(
                status_code=409, detail="o plano ainda não está pronto", headers=PRIVATE_HEADERS
            )
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
            raise HTTPException(410, "link expirado", headers=PRIVATE_HEADERS) from None
        except PlanHeld:
            return JSONResponse({"code": "plan_held"}, status_code=403, headers=PRIVATE_HEADERS)
        except (ReleaseRefused, DeploymentContractError):
            raise HTTPException(503, "acesso indisponível", headers=PRIVATE_HEADERS) from None
        except NoPdf:
            raise HTTPException(404, "PDF não encontrado", headers=PRIVATE_HEADERS) from None
        except PdfRemoved:
            raise HTTPException(
                410, "PDF removido pela retenção", headers=PRIVATE_HEADERS
            ) from None
        except (RetryableError, TerminalError, OSError):
            raise HTTPException(
                502, "PDF indisponível no momento", headers=PRIVATE_HEADERS
            ) from None
        if pdf.url:
            return RedirectResponse(pdf.url, status_code=302, headers=PRIVATE_HEADERS)
        return Response(
            pdf.content,
            media_type="application/pdf",
            headers={
                **PRIVATE_HEADERS,
                "Content-Disposition": f'attachment; filename="plano-{job.public_id}.pdf"',
            },
        )

    from app.web.internal import router as internal_router

    app.include_router(internal_router)
    return app


def __getattr__(name: str) -> FastAPI:
    # `uvicorn app.api.main:app` cria o app só quando pedido: importar o módulo nos testes não
    # exige configuração.
    if name == "app":
        return create_app()
    raise AttributeError(name)
