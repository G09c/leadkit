"""Captura parcial nas costuras públicas de configuração, persistência e retomada."""

from test_capture import capture_document

from app.forms.definition import compile_definition


def test_existing_forms_do_not_enable_partial_capture():
    assert compile_definition(capture_document()).partial_capture is None


def partial_document():
    document = capture_document()
    document["steps"][0]["questions"].append(
        {
            "name": "contact_permission",
            "kind": "choice",
            "label": "Podemos entrar em contato?",
            "options": [{"value": "yes", "label": "Sim"}, {"value": "no", "label": "Não"}],
        }
    )
    document["examples"]["answers"]["contact_permission"] = "yes"
    document["partial_capture"] = {
        "contact_permission_field": "contact_permission",
        "contact_permission_value": "yes",
        "ttl_hours": 72,
    }
    return document


def test_partial_capture_configuration_pins_explicit_contact_permission():
    spec = compile_definition(partial_document())
    assert spec.partial_capture["ttl_hours"] == 72
    assert spec.partial_capture["contact_permission_field"] == "contact_permission"


def test_partial_capture_rejects_consent_only_at_final_submit():
    import pytest

    from app.forms.definition import DefinitionError

    document = partial_document()
    document["consent"]["position"] = "before_submit"
    with pytest.raises(DefinitionError, match="partial_capture"):
        compile_definition(document)


def test_partial_save_and_resume_from_another_client(deps):
    import json
    import uuid

    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.forms.stored import import_definition

    document = partial_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    first_fields = {q["name"] for q in document["steps"][0]["questions"]}
    payload = {
        "request_id": str(uuid.uuid4()),
        "step": 0,
        "answers": {k: v for k, v in document["examples"]["answers"].items() if k in first_fields},
        "consent": {"privacy": True, "text_version": document["consent"]["version"]},
        "tracking": {"utm_source": "synthetic"},
    }
    with TestClient(create_app()) as first:
        saved = first.post("/v1/forms/capture-test/partials", json=payload)
        assert saved.status_code == 201, saved.text
        token = saved.json()["token"]
    with TestClient(create_app()) as other:
        resumed = other.post("/v1/forms/capture-test/partials/resume", json={"token": token})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["answers"] == payload["answers"]
        assert resumed.json()["revision"] == 1
        assert resumed.json()["version_number"] == 1


def test_partial_finalization_correlates_and_replays_one_submission(deps):
    import json
    import uuid

    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.forms.stored import import_definition

    document = partial_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    first_fields = {q["name"] for q in document["steps"][0]["questions"]}
    consent = {"privacy": True, "text_version": document["consent"]["version"]}
    with TestClient(create_app()) as client:
        partial = client.post(
            "/v1/forms/capture-test/partials",
            json={
                "request_id": str(uuid.uuid4()),
                "step": 0,
                "answers": {
                    k: v for k, v in document["examples"]["answers"].items() if k in first_fields
                },
                "consent": consent,
            },
        ).json()
        body = {
            "answers": document["examples"]["answers"],
            "consent": consent,
            "partial_token": partial["token"],
        }
        first = client.post("/v1/forms/capture-test/submissions", json=body)
        assert first.status_code == 202, first.text
        replay = client.post("/v1/forms/capture-test/submissions", json=body)
        assert replay.status_code == 202, replay.text
        assert replay.json()["submission_id"] == first.json()["submission_id"]
        assert (
            client.post(
                "/v1/forms/capture-test/partials/resume", json={"token": partial["token"]}
            ).status_code
            == 404
        )


def test_public_page_offers_opt_in_partial_save_and_resume(deps):
    import json
    import uuid

    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.forms.stored import import_definition

    document = partial_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    first_fields = {q["name"] for q in document["steps"][0]["questions"]}
    with TestClient(create_app()) as client:
        page = client.get("/p/capture-test")
        assert 'data-partial-url="/v1/forms/capture-test/partials"' in page.text
        saved = client.post(
            "/v1/forms/capture-test/partials",
            json={
                "request_id": str(uuid.uuid4()),
                "step": 0,
                "answers": {
                    k: v for k, v in document["examples"]["answers"].items() if k in first_fields
                },
                "consent": {"privacy": True, "text_version": document["consent"]["version"]},
            },
        ).json()
        resumed = client.post("/p/capture-test/retomar", data={"token": saved["token"]})
        assert resumed.status_code == 200
        assert 'name="_partial_token"' in resumed.text
        assert 'data-partial-revision="1"' in resumed.text
        assert document["examples"]["answers"]["email"] in resumed.text


def test_partial_contact_delivery_is_durable_and_idempotent(deps):
    import json
    import uuid

    from fastapi.testclient import TestClient

    from app import partial_deliveries
    from app.api.main import create_app
    from app.destinations import save
    from app.forms.stored import import_definition

    document = partial_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    with deps.sessions.begin() as session:
        save(
            session,
            deps.cipher,
            form_slug="capture-test",
            url="https://example.test/partial",
            actor="operadora",
            expected_version=0,
            production=False,
            http_confirmed=False,
            purpose="internal",
            events=["contato.capturado", "formulario.concluido", "contato.revogado"],
        )
    first_fields = {q["name"] for q in document["steps"][0]["questions"]}
    payload = {
        "request_id": str(uuid.uuid4()),
        "step": 0,
        "answers": {k: v for k, v in document["examples"]["answers"].items() if k in first_fields},
        "consent": {"privacy": True, "text_version": document["consent"]["version"]},
    }
    calls = []

    class Transport:
        def send(self, url, secret, event, event_id, payload):
            calls.append((event, event_id, payload))
            return 204

    deps.webhook = Transport()
    with TestClient(create_app()) as client:
        saved = client.post("/v1/forms/capture-test/partials", json=payload)
        assert saved.status_code == 201
        assert client.post("/v1/forms/capture-test/partials", json=payload).status_code == 200
    partial_deliveries.dispatch_pending(deps)
    partial_deliveries.dispatch_pending(deps)
    assert len(calls) == 1
    assert calls[0][0] == "contato.capturado"
    event = calls[0][2]
    assert event["event_id"] == calls[0][1]
    assert event["partial"]["id"] == saved.json()["id"]
    assert event["contact"]["email"] == payload["answers"]["email"]
    assert event["resume_url"].startswith("http://testserver/p/capture-test#resume=")
    assert event["consent"]["contact"]["authorized"] is True
    with TestClient(create_app()) as client:
        final = client.post(
            "/v1/forms/capture-test/submissions",
            json={
                "answers": document["examples"]["answers"],
                "consent": payload["consent"],
                "partial_token": saved.json()["token"],
            },
        )
        assert final.status_code == 202
    from datetime import timedelta

    from app.models import utcnow

    partial_deliveries.dispatch_pending(deps, now=utcnow() + timedelta(days=4))
    assert len(calls) == 2
    assert calls[1][0] == "formulario.concluido"
    assert calls[1][2]["partial"]["id"] == event["partial"]["id"]
    assert calls[1][2]["submission"]["id"] == final.json()["submission_id"]
    assert "resume_url" not in calls[1][2]


def test_partial_capability_lookup_is_rate_limited(deps):
    from fastapi.testclient import TestClient

    from app.api.main import create_app

    with TestClient(create_app(clock=lambda: 1000.0)) as client:
        for _ in range(30):
            assert (
                client.post(
                    "/v1/forms/unknown/partials/resume", json={"token": "invalid"}
                ).status_code
                == 404
            )
        limited = client.post("/v1/forms/unknown/partials/resume", json={"token": "invalid"})
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) > 0


def test_expired_partial_is_unavailable_and_retention_erases_it(deps, monkeypatch):
    import json
    import uuid
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from app import partial_capture
    from app.api.main import create_app
    from app.forms.stored import import_definition
    from app.retention import purge_expired

    document = partial_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    fields = {q["name"] for q in document["steps"][0]["questions"]}
    with TestClient(create_app()) as client:
        saved = client.post(
            "/v1/forms/capture-test/partials",
            json={
                "request_id": str(uuid.uuid4()),
                "step": 0,
                "answers": {
                    k: v for k, v in document["examples"]["answers"].items() if k in fields
                },
                "consent": {"privacy": True, "text_version": document["consent"]["version"]},
            },
        ).json()
        expired = partial_capture.utcnow() + timedelta(days=4)
        monkeypatch.setattr(partial_capture, "utcnow", lambda: expired)
        assert (
            client.post(
                "/v1/forms/capture-test/partials/resume", json={"token": saved["token"]}
            ).status_code
            == 404
        )
        assert purge_expired(deps, now=expired).partials_deleted == 1
        assert purge_expired(deps, now=expired).partials_deleted == 0


def test_partial_save_rollback_can_be_retried_without_false_confirmation(deps):
    import json
    import uuid

    import sqlalchemy as sa
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from app.api.main import create_app
    from app.forms.stored import import_definition

    document = partial_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    fields = {q["name"] for q in document["steps"][0]["questions"]}
    body = {
        "request_id": str(uuid.uuid4()),
        "step": 0,
        "answers": {k: v for k, v in document["examples"]["answers"].items() if k in fields},
        "consent": {"privacy": True, "text_version": document["consent"]["version"]},
    }

    def fail_commit(session):
        raise sa.exc.OperationalError("synthetic failure", None, Exception())

    with TestClient(create_app()) as client:
        sa.event.listen(Session, "before_commit", fail_commit)
        try:
            assert client.post("/v1/forms/capture-test/partials", json=body).status_code == 503
        finally:
            sa.event.remove(Session, "before_commit", fail_commit)
        saved = client.post("/v1/forms/capture-test/partials", json=body)
        assert saved.status_code == 201
        assert (
            client.post("/v1/forms/capture-test/partials", json=body).json()["id"]
            == saved.json()["id"]
        )


def test_saved_steps_keep_revision_and_historical_version(deps):
    import json
    import uuid

    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.forms import drafts
    from app.forms.stored import import_definition

    document = partial_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    fields = {q["name"] for q in document["steps"][0]["questions"]}
    consent = {"privacy": True, "text_version": document["consent"]["version"]}
    with TestClient(create_app()) as client:
        saved = client.post(
            "/v1/forms/capture-test/partials",
            json={
                "request_id": str(uuid.uuid4()),
                "step": 0,
                "answers": {
                    k: v for k, v in document["examples"]["answers"].items() if k in fields
                },
                "consent": consent,
            },
        ).json()
        with deps.sessions() as session:
            draft = drafts.load(session, "capture-test")
        changed = dict(document, title="Versão seguinte")
        changed.pop("partial_capture")
        draft = drafts.save_document(
            deps, "capture-test", changed, actor="operadora", expected=draft.token
        )
        drafts.publish(
            deps, "capture-test", actor="operadora", note="Nova versão", expected=draft.token
        )
        fields.update(q["name"] for q in document["steps"][1]["questions"])
        update = {
            "request_id": str(uuid.uuid4()),
            "step": 1,
            "revision": 1,
            "token": saved["token"],
            "answers": {k: v for k, v in document["examples"]["answers"].items() if k in fields},
            "consent": consent,
        }
        advanced = client.post("/v1/forms/capture-test/partials", json=update)
        assert advanced.status_code == 201, advanced.text
        assert advanced.json()["revision"] == 2
        assert advanced.json()["version_number"] == 1
        assert advanced.json()["last_activity_at"] >= saved["last_activity_at"]
        first_names = {q["name"] for q in document["steps"][0]["questions"]}
        back = dict(
            update,
            request_id=str(uuid.uuid4()),
            step=0,
            revision=2,
            answers={k: v for k, v in update["answers"].items() if k in first_names},
        )
        back_saved = client.post("/v1/forms/capture-test/partials", json=back)
        assert back_saved.status_code == 201, back_saved.text
        assert back_saved.json()["answers"] == advanced.json()["answers"]

        update["request_id"] = str(uuid.uuid4())
        assert client.post("/v1/forms/capture-test/partials", json=update).status_code == 409
        final = client.post(
            "/v1/forms/capture-test/submissions",
            json={
                "answers": document["examples"]["answers"],
                "consent": consent,
                "partial_token": saved["token"],
            },
        )
        assert final.status_code == 202, final.text


def test_partial_retry_preserves_event_and_does_not_generate(deps):
    import json
    import uuid
    from datetime import timedelta

    import sqlalchemy as sa
    from fastapi.testclient import TestClient

    from app import partial_deliveries
    from app.api.main import create_app
    from app.destinations import save
    from app.errors import FailureCode, RetryableError
    from app.forms.stored import import_definition
    from app.models import Job, utcnow

    document = partial_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    with deps.sessions.begin() as session:
        save(
            session,
            deps.cipher,
            form_slug="capture-test",
            url="https://example.test/partial",
            actor="operadora",
            expected_version=0,
            production=False,
            http_confirmed=False,
            purpose="internal",
            events=["contato.capturado", "formulario.concluido", "contato.revogado"],
        )
    fields = {q["name"] for q in document["steps"][0]["questions"]}
    calls = []

    class Transport:
        def send(self, url, secret, event, event_id, payload):
            calls.append((event_id, payload))
            if len(calls) == 1:
                raise RetryableError(FailureCode.DELIVERY_UNAVAILABLE, "synthetic")
            return 204

    deps.webhook = Transport()
    body = {
        "request_id": str(uuid.uuid4()),
        "step": 0,
        "answers": {k: v for k, v in document["examples"]["answers"].items() if k in fields},
        "consent": {"privacy": True, "text_version": document["consent"]["version"]},
    }
    with TestClient(create_app()) as client:
        assert client.post("/v1/forms/capture-test/partials", json=body).status_code == 201
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Job)) == 0
    now = utcnow()
    assert partial_deliveries.dispatch_pending(deps, now=now) == 0
    assert partial_deliveries.dispatch_pending(deps, now=now) == 0
    assert partial_deliveries.dispatch_pending(deps, now=now + timedelta(minutes=2)) == 1
    assert len(calls) == 2
    assert calls[0] == calls[1]


def test_refused_contact_creates_no_delivery_and_still_completes(deps):
    import json
    import uuid

    import sqlalchemy as sa
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.forms.stored import import_definition
    from app.models import PartialDelivery

    document = partial_document()
    document["examples"]["answers"]["contact_permission"] = "no"
    import_definition(deps, json.dumps(document), actor="operadora")
    from app.destinations import save

    with deps.sessions.begin() as session:
        save(
            session,
            deps.cipher,
            form_slug="capture-test",
            url="https://example.test/partial",
            actor="operadora",
            expected_version=0,
            production=False,
            http_confirmed=False,
            purpose="internal",
            events=["contato.capturado", "formulario.concluido", "contato.revogado"],
        )
    fields = {q["name"] for q in document["steps"][0]["questions"]}
    consent = {"privacy": True, "text_version": document["consent"]["version"]}
    with TestClient(create_app()) as client:
        saved = client.post(
            "/v1/forms/capture-test/partials",
            json={
                "request_id": str(uuid.uuid4()),
                "step": 0,
                "consent": consent,
                "answers": {
                    k: v for k, v in document["examples"]["answers"].items() if k in fields
                },
            },
        )
        assert saved.status_code == 201
        final = client.post(
            "/v1/forms/capture-test/submissions",
            json={
                "answers": document["examples"]["answers"],
                "consent": consent,
                "partial_token": saved.json()["token"],
            },
        )
        assert final.status_code == 202
    with deps.sessions() as session:
        events = list(session.scalars(sa.select(PartialDelivery)))
        assert len(events) == 1
        assert events[0].event == "formulario.concluido"
        assert events[0].payload["consent"]["contact"]["authorized"] is False


def test_contact_revocation_is_delivered_before_final_submission(deps):
    import json
    import uuid

    from fastapi.testclient import TestClient

    from app import partial_deliveries
    from app.api.main import create_app
    from app.destinations import save
    from app.forms.stored import import_definition

    document = partial_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    with deps.sessions.begin() as session:
        save(
            session,
            deps.cipher,
            form_slug="capture-test",
            url="https://example.test/partial",
            actor="operadora",
            expected_version=0,
            production=False,
            http_confirmed=False,
            purpose="internal",
            events=["contato.capturado", "formulario.concluido", "contato.revogado"],
        )
    fields = {q["name"] for q in document["steps"][0]["questions"]}
    calls = []

    class Transport:
        def send(self, url, secret, event, event_id, payload):
            calls.append(payload)
            return 204

    deps.webhook = Transport()
    body = {
        "request_id": str(uuid.uuid4()),
        "step": 0,
        "answers": {k: v for k, v in document["examples"]["answers"].items() if k in fields},
        "consent": {"privacy": True, "text_version": document["consent"]["version"]},
    }
    with TestClient(create_app()) as client:
        first = client.post("/v1/forms/capture-test/partials", json=body).json()
        partial_deliveries.dispatch_pending(deps)
        body.update(request_id=str(uuid.uuid4()), token=first["token"], revision=1)
        body["answers"]["contact_permission"] = "no"
        assert client.post("/v1/forms/capture-test/partials", json=body).status_code == 201
        partial_deliveries.dispatch_pending(deps)
    assert [c["event"] for c in calls] == ["contato.capturado", "contato.revogado"]
    assert calls[1]["consent"]["contact"]["authorized"] is False
    assert calls[1]["submission"] is None
    assert calls[1]["partial"]["id"] == calls[0]["partial"]["id"]
    assert "resume_url" not in calls[1]


def test_pdf_final_event_identifies_job_without_access_token(deps):
    import json
    import uuid

    import sqlalchemy as sa
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.destinations import save
    from app.forms.starter import definition
    from app.forms.stored import import_definition
    from app.models import PartialDelivery

    partial = partial_document()
    document = definition(slug="capture-test", title="Pesquisa de exemplo")
    document["steps"] = partial["steps"]
    document["partial_capture"] = partial["partial_capture"]
    document["examples"]["answers"] = partial["examples"]["answers"]
    import_definition(deps, json.dumps(document), actor="operadora")
    with deps.sessions.begin() as session:
        save(
            session,
            deps.cipher,
            form_slug="capture-test",
            url="https://example.test/partial",
            actor="operadora",
            expected_version=0,
            production=False,
            http_confirmed=False,
            purpose="internal",
            events=["contato.capturado", "formulario.concluido", "contato.revogado"],
        )

    async def trigger(*args):
        return None

    fields = {q["name"] for q in document["steps"][0]["questions"]}
    consent = {"privacy": True, "text_version": document["consent"]["version"]}
    with TestClient(create_app(trigger=trigger)) as client:
        saved = client.post(
            "/v1/forms/capture-test/partials",
            json={
                "request_id": str(uuid.uuid4()),
                "step": 0,
                "answers": {
                    k: v for k, v in document["examples"]["answers"].items() if k in fields
                },
                "consent": consent,
            },
        ).json()
        final = client.post(
            "/v1/forms/capture-test/submissions",
            json={
                "answers": document["examples"]["answers"],
                "consent": consent,
                "partial_token": saved["token"],
            },
        )
        assert final.status_code == 202, final.text
    with deps.sessions() as session:
        event = session.scalar(
            sa.select(PartialDelivery).where(PartialDelivery.event == "formulario.concluido")
        )
        assert event.payload["job"] == {"public_id": final.json()["public_id"], "attempt": 1}
        assert "token" not in event.payload["job"]
