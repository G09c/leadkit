"""API pública de retomada; capacidades trafegam no corpo, nunca no caminho."""

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.deps import get_deps
from app.forms import UnknownForm
from app.forms.stored import InactiveForm
from app.partial_capture import (
    PartialConflict,
    PartialUnavailable,
    SavePartial,
    lookup,
    save,
    snapshot,
)

router = APIRouter()


@router.post("/v1/forms/{slug}/partials")
@router.post("/v1/forms/{slug}/partials/resume")
async def partial(slug: str, request: Request):
    wait = request.app.state.partial_limiter.take(
        request.client.host if request.client else "unknown"
    )
    if wait:
        return JSONResponse(
            {"code": "rate_limited"},
            status_code=429,
            headers={"Retry-After": str(wait), "Cache-Control": "no-store"},
        )
    try:
        raw = await request.body()
        if len(raw) > 32768:
            return JSONResponse({"code": "invalid_partial"}, status_code=413)
        import json

        body = json.loads(raw)
        if not isinstance(body, dict):
            raise TypeError("invalid_body")
        deps = get_deps()
        resume = request.url.path.endswith("/resume")

        def operate():
            with deps.sessions.begin() as session:
                if resume:
                    token = body.get("token", "")
                    if not isinstance(token, str) or len(token) > 100:
                        raise PartialUnavailable()
                    return snapshot(lookup(session, slug, token)), True
                return save(session, deps, slug, SavePartial.model_validate(body))

        result, replay = await run_in_threadpool(operate)
        return JSONResponse(
            result, status_code=200 if replay else 201, headers={"Cache-Control": "no-store"}
        )
    except (PartialUnavailable, UnknownForm, InactiveForm):
        return JSONResponse({"code": "partial_unavailable"}, status_code=404)
    except PartialConflict:
        return JSONResponse({"code": "partial_conflict"}, status_code=409)
    except (ValueError, TypeError, ValidationError):
        return JSONResponse({"code": "invalid_partial"}, status_code=422)
    except sa.exc.SQLAlchemyError:
        return JSONResponse({"code": "unavailable"}, status_code=503)
