import json
import uuid
from copy import deepcopy
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_partial_capture import partial_document

from app import partial_capture, partial_deliveries
from app.api.main import create_app
from app.destinations import save
from app.forms.stored import import_definition
from app.models import PartialDelivery, utcnow


def setup(deps, permission="yes", *, one_step=False, generation="none"):
    doc = partial_document()
    if one_step:
        doc["steps"] = [
            {
                **doc["steps"][0],
                "questions": [q for step in doc["steps"] for q in step["questions"]],
            }
        ]
    if generation == "pdf":
        from app.forms.starter import definition

        original = definition(slug="capture-test", title="Pesquisa")
        for key in ("instructions", "prompt", "plan", "pdf", "preview"):
            doc[key] = original[key]
        doc["examples"]["plan"] = original["examples"]["plan"]
        doc["generation"] = "pdf"
    doc["consent"]["marketing"] = {"email": "Autorizo email", "whatsapp": "Autorizo WhatsApp"}
    doc["examples"]["answers"]["contact_permission"] = permission
    import_definition(deps, json.dumps(doc), actor="review")
    with deps.sessions.begin() as s:
        save(
            s,
            deps.cipher,
            form_slug="capture-test",
            url="https://example.test/partial",
            actor="review",
            expected_version=0,
            production=False,
            http_confirmed=False,
            purpose="internal",
            events=list(partial_deliveries.EVENTS),
        )
    names = {q["name"] for q in doc["steps"][0]["questions"]}
    body = {
        "request_id": str(uuid.uuid4()),
        "step": 0,
        "answers": {k: v for k, v in doc["examples"]["answers"].items() if k in names},
        "consent": {
            "privacy": True,
            "text_version": doc["consent"]["version"],
            "marketing_email": True,
        },
    }
    return doc, body


@pytest.mark.parametrize("channel", ["marketing_email", "marketing_whatsapp"])
def test_updated_consent_is_persisted(deps, channel):
    _doc, body = setup(deps)
    body["consent"][channel] = True
    with TestClient(create_app()) as c:
        first = c.post("/v1/forms/capture-test/partials", json=body).json()
        body.update(request_id=str(uuid.uuid4()), token=first["token"], revision=1)
        body["consent"][channel] = False
        updated = c.post("/v1/forms/capture-test/partials", json=body)
        assert updated.status_code == 201
        assert updated.json()["consent"][channel] is False
        page = c.post("/p/capture-test/retomar", data={"token": updated.json()["token"]})
        assert page.status_code == 200
        from html.parser import HTMLParser

        class Inputs(HTMLParser):
            def __init__(self):
                super().__init__()
                self.inputs = []

            def handle_starttag(self, tag, attrs):
                if tag == "input":
                    self.inputs.append(dict(attrs))

        inputs = Inputs()
        inputs.feed(page.text)
        checkbox = next(
            item
            for item in inputs.inputs
            if item.get("name") == "_mkt_" + channel.removeprefix("marketing_")
        )
        assert "checked" not in checkbox


def test_later_contact_permission_creates_event(deps):
    _doc, body = setup(deps, "no")
    with TestClient(create_app()) as c:
        first = c.post("/v1/forms/capture-test/partials", json=body).json()
        body.update(request_id=str(uuid.uuid4()), token=first["token"], revision=1)
        body["answers"]["contact_permission"] = "yes"
        assert c.post("/v1/forms/capture-test/partials", json=body).status_code == 201
    with deps.sessions() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(PartialDelivery)) == 1


@pytest.mark.parametrize("generation", ["none", "pdf"])
def test_retention_blocks_late_creation_replay(deps, monkeypatch, generation):
    from app.retention import purge_expired

    doc, body = setup(deps, generation=generation)
    original = deepcopy(body)

    async def trigger(job_id, attempt):
        return "synthetic-run"

    with TestClient(create_app(trigger=trigger)) as c:
        first = c.post("/v1/forms/capture-test/partials", json=body).json()
        final = c.post(
            "/v1/forms/capture-test/submissions",
            json={
                "answers": doc["examples"]["answers"],
                "consent": body["consent"],
                "partial_token": first["token"],
            },
        )
        assert final.status_code == 202
        future = utcnow() + timedelta(days=10000)
        report = purge_expired(deps, now=future)
        assert report.captures_deleted + report.requests_deleted == 1
        monkeypatch.setattr(partial_capture, "utcnow", lambda: future)
        replay = c.post("/v1/forms/capture-test/partials", json=original)
        assert replay.status_code == 404


def test_pending_contact_uses_corrected_identity(deps):
    _doc, body = setup(deps)
    calls = []

    class Transport:
        def send(self, url, secret, event, event_id, payload):
            calls.append(payload)

    deps.webhook = Transport()
    with TestClient(create_app()) as c:
        first = c.post("/v1/forms/capture-test/partials", json=body).json()
        body.update(request_id=str(uuid.uuid4()), token=first["token"], revision=1)
        body["answers"]["email"] = "corrected@example.test"
        assert c.post("/v1/forms/capture-test/partials", json=body).status_code == 201
        partial_deliveries.dispatch_pending(deps)
    contacts = [p for p in calls if p["event"] == "contato.capturado"]
    assert len(contacts) == 1
    assert contacts[0]["contact"]["email"] == "corrected@example.test"
    assert "resume_url" in contacts[0]


def test_back_navigation_preserves_later_steps(deps):
    doc, body = setup(deps)
    first_answers = deepcopy(body["answers"])
    with TestClient(create_app()) as c:
        first = c.post("/v1/forms/capture-test/partials", json=body).json()
        body.update(
            request_id=str(uuid.uuid4()),
            token=first["token"],
            revision=1,
            step=len(doc["steps"]) - 1,
            answers=doc["examples"]["answers"],
        )
        advanced = c.post("/v1/forms/capture-test/partials", json=body)
        assert advanced.status_code == 201
        later = set(advanced.json()["answers"]) - set(first_answers)
        assert later
        body.update(request_id=str(uuid.uuid4()), revision=2, step=0, answers=first_answers)
        assert c.post("/v1/forms/capture-test/partials", json=body).status_code == 201
        resumed = c.post("/v1/forms/capture-test/partials/resume", json={"token": first["token"]})
        assert later.issubset(resumed.json()["answers"])


@pytest.mark.parametrize("generation", ["none", "pdf"])
def test_final_without_partial_token_emits_completion(deps, generation):
    doc, body = setup(deps, generation=generation)
    triggered = []

    async def trigger(job_id, attempt):
        triggered.append(job_id)
        return "synthetic-run"

    with TestClient(create_app(trigger=trigger)) as c:
        for _ in range(2):
            final = c.post(
                "/v1/forms/capture-test/submissions",
                json={"answers": doc["examples"]["answers"], "consent": body["consent"]},
                headers={"Idempotency-Key": "normal-final"},
            )
            assert final.status_code == 202
    with deps.sessions() as s:
        deliveries = s.scalars(sa.select(PartialDelivery)).all()
        assert len(deliveries) == 1
        assert deliveries[0].event == "formulario.concluido"
        assert ("job" in deliveries[0].payload) == (generation == "pdf")
    assert len(triggered) == (1 if generation == "pdf" else 0)


def test_identity_successor_invalidates_attempted_capability_and_replays(deps):
    from app.errors import FailureCode, RetryableError
    from app.retention import purge_expired

    _doc, body = setup(deps)
    calls = []

    class Transport:
        def send(self, url, secret, event, event_id, payload):
            calls.append(deepcopy(payload))
            if len(calls) == 1:
                raise RetryableError(FailureCode.DELIVERY_UNAVAILABLE, "synthetic")

    deps.webhook = Transport()
    with TestClient(create_app()) as c:
        first = c.post("/v1/forms/capture-test/partials", json=body).json()
        partial_deliveries.dispatch_pending(deps)
        body.update(request_id=str(uuid.uuid4()), token=first["token"], revision=1)
        body["answers"]["email"] = "corrected@example.test"
        response = c.post("/v1/forms/capture-test/partials", json=body)
        assert response.status_code == 201
        successor = response.json()
        assert successor["id"] != first["id"]
        assert successor["token"] != first["token"]
        assert datetime.fromisoformat(successor["expires_at"]) == datetime.fromisoformat(
            first["expires_at"]
        )
        replay = c.post("/v1/forms/capture-test/partials", json=body)
        assert replay.status_code == 200
        assert replay.json()["id"] == successor["id"]
        assert replay.json()["token"] == successor["token"]
        assert replay.json()["answers"] == successor["answers"]
        assert (
            c.post(
                "/v1/forms/capture-test/partials/resume", json={"token": first["token"]}
            ).status_code
            == 404
        )
        assert (
            c.post(
                "/v1/forms/capture-test/partials/resume", json={"token": successor["token"]}
            ).json()["answers"]["email"]
            == "corrected@example.test"
        )
        purge_expired(deps)
        partial_deliveries.dispatch_pending(deps, now=utcnow() + timedelta(minutes=5))
    assert [p["event"] for p in calls].count("contato.revogado") == 1
    revoked = next(p for p in calls if p["event"] == "contato.revogado")
    assert revoked["partial"]["id"] == first["id"]
    assert revoked["consent"]["contact"]["authorized"] is False
    assert calls[-1]["partial"]["id"] == successor["id"]
    with deps.sessions() as s:
        old = s.get(PartialDelivery, uuid.UUID(calls[0]["event_id"]))
        assert old.attempts == 1
        assert old.status == "cancelled"
        assert old.payload == {k: v for k, v in calls[0].items() if k != "resume_url"}


def test_regrant_creates_new_correlation_without_reopening_revoked_one(deps):
    _doc, body = setup(deps)
    calls = []

    class Transport:
        def send(self, url, secret, event, event_id, payload):
            calls.append(payload)

    deps.webhook = Transport()
    with TestClient(create_app()) as c:
        current = c.post("/v1/forms/capture-test/partials", json=body).json()
        first = current
        partial_deliveries.dispatch_pending(deps)
        for permission in ("no", "yes"):
            body.update(
                request_id=str(uuid.uuid4()), token=current["token"], revision=current["revision"]
            )
            body["answers"]["contact_permission"] = permission
            response = c.post("/v1/forms/capture-test/partials", json=body)
            assert response.status_code == 201
            current = response.json()
            partial_deliveries.dispatch_pending(deps)
        assert (
            c.post(
                "/v1/forms/capture-test/partials/resume", json={"token": first["token"]}
            ).status_code
            == 404
        )
    assert [p["event"] for p in calls] == [
        "contato.capturado",
        "contato.revogado",
        "contato.capturado",
    ]
    assert calls[0]["partial"]["id"] == calls[1]["partial"]["id"]
    assert calls[2]["partial"]["id"] != calls[0]["partial"]["id"]


@pytest.mark.parametrize("one_step", [False, True])
def test_html_completion_without_javascript_emits_once(deps, one_step):
    doc, _body = setup(deps, one_step=one_step)
    calls = []

    class Transport:
        def send(self, url, secret, event, event_id, payload):
            calls.append(payload)

    deps.webhook = Transport()
    data = {
        **doc["examples"]["answers"],
        "_idem": "html-final",
        "_privacy": "1",
        "_consent_version": doc["consent"]["version"],
    }
    with TestClient(create_app(), follow_redirects=False) as c:
        for _ in range(2):
            response = c.post("/p/capture-test", data=data)
            assert response.status_code == 303, response.text
    assert partial_deliveries.dispatch_pending(deps) == 1
    assert partial_deliveries.dispatch_pending(deps) == 0
    assert [p["event"] for p in calls] == ["formulario.concluido"]
    assert calls[0]["submission"]["id"]
    assert "resume_url" not in calls[0]
    assert calls[0]["form"]["version_number"] == 1


def test_conditional_required_partial_matches_full_validation(deps):
    doc = partial_document()
    doc["steps"][0]["questions"].append(
        {
            "name": "detail",
            "kind": "text",
            "label": "Detalhe",
            "required": True,
            "min_length": 2,
            "max_length": 100,
            "show_when": {
                "mode": "all",
                "rules": [{"field": "contact_permission", "operator": "equals", "value": "yes"}],
            },
        }
    )
    doc["examples"]["answers"]["detail"] = "Exemplo"
    import_definition(deps, json.dumps(doc), actor="review")
    fields = {q["name"] for q in doc["steps"][0]["questions"]}
    answers = {k: v for k, v in doc["examples"]["answers"].items() if k in fields and k != "detail"}
    consent = {"privacy": True, "text_version": doc["consent"]["version"]}
    with TestClient(create_app()) as c:
        full = c.post(
            "/v1/forms/capture-test/submissions",
            json={
                "answers": {k: v for k, v in doc["examples"]["answers"].items() if k != "detail"},
                "consent": consent,
            },
        )
        assert full.status_code == 422
        partial = c.post(
            "/v1/forms/capture-test/partials",
            json={
                "request_id": str(uuid.uuid4()),
                "step": 0,
                "answers": answers,
                "consent": consent,
            },
        )
        assert partial.status_code == 422, partial.text


def test_partial_conditional_scope_does_not_require_future_steps(deps):
    doc = partial_document()
    condition = {
        "mode": "all",
        "rules": [{"field": "contact_permission", "operator": "equals", "value": "yes"}],
    }
    doc["steps"][-1]["questions"].append(
        {
            "name": "future_detail",
            "kind": "text",
            "max_length": 100,
            "label": "Detalhe futuro",
            "required": True,
            "min_length": 2,
            "show_when": condition,
        }
    )
    doc["examples"]["answers"]["future_detail"] = "Exemplo"
    import_definition(deps, json.dumps(doc), actor="review")
    fields = {q["name"] for q in doc["steps"][0]["questions"]}
    body = {
        "request_id": str(uuid.uuid4()),
        "step": 0,
        "answers": {k: v for k, v in doc["examples"]["answers"].items() if k in fields},
        "consent": {"privacy": True, "text_version": doc["consent"]["version"]},
    }
    with TestClient(create_app()) as c:
        first = c.post("/v1/forms/capture-test/partials", json=body)
        assert first.status_code == 201
        body.update(
            request_id=str(uuid.uuid4()),
            token=first.json()["token"],
            revision=1,
            step=len(doc["steps"]) - 1,
            answers=doc["examples"]["answers"],
        )
        advanced = c.post("/v1/forms/capture-test/partials", json=body)
        assert advanced.status_code == 201
        body.update(
            request_id=str(uuid.uuid4()),
            revision=2,
            step=0,
            answers={k: v for k, v in body["answers"].items() if k in fields},
        )
        body["answers"]["contact_permission"] = "no"
        hidden = c.post("/v1/forms/capture-test/partials", json=body)
        assert hidden.status_code == 201
        assert "future_detail" not in hidden.json()["answers"]


def test_partial_rejects_conditionally_unavailable_option(deps):
    doc = partial_document()
    condition = {
        "mode": "all",
        "rules": [{"field": "contact_permission", "operator": "equals", "value": "yes"}],
    }
    doc["steps"][0]["questions"].append(
        {
            "name": "channel",
            "kind": "choice",
            "label": "Canal",
            "options": [
                {"value": "base", "label": "Padrão"},
                {"value": "gated", "label": "Condicionado", "show_when": condition},
            ],
        }
    )
    doc["examples"]["answers"]["channel"] = "base"
    import_definition(deps, json.dumps(doc), actor="review")
    fields = {q["name"] for q in doc["steps"][0]["questions"]}
    answers = {k: v for k, v in doc["examples"]["answers"].items() if k in fields}
    answers.update(contact_permission="no", channel="gated")
    with TestClient(create_app()) as c:
        response = c.post(
            "/v1/forms/capture-test/partials",
            json={
                "request_id": str(uuid.uuid4()),
                "step": 0,
                "answers": answers,
                "consent": {"privacy": True, "text_version": doc["consent"]["version"]},
            },
        )
        assert response.status_code == 422


def test_return_to_first_step_can_reveal_unanswered_later_question(deps):
    from copy import deepcopy

    doc = partial_document()
    doc["steps"][1]["questions"].append(
        {
            "name": "detail",
            "kind": "text",
            "label": "Detalhe",
            "required": True,
            "min_length": 2,
            "max_length": 100,
            "show_when": {
                "mode": "all",
                "rules": [{"field": "contact_permission", "operator": "equals", "value": "yes"}],
            },
        }
    )
    doc["examples"]["answers"]["detail"] = "Exemplo"
    import_definition(deps, json.dumps(doc), actor="review")
    fields = {q["name"] for q in doc["steps"][0]["questions"]}
    first_answers = {k: v for k, v in doc["examples"]["answers"].items() if k in fields}
    first_answers["contact_permission"] = "no"
    body = {
        "request_id": str(uuid.uuid4()),
        "step": 0,
        "answers": first_answers,
        "consent": {"privacy": True, "text_version": doc["consent"]["version"]},
    }
    with TestClient(create_app()) as c:
        first = c.post("/v1/forms/capture-test/partials", json=body).json()
        all_answers = {k: v for k, v in doc["examples"]["answers"].items() if k != "detail"}
        all_answers["contact_permission"] = "no"
        body.update(
            request_id=str(uuid.uuid4()),
            token=first["token"],
            revision=first["revision"],
            step=len(doc["steps"]) - 1,
            answers=all_answers,
        )
        advanced = c.post("/v1/forms/capture-test/partials", json=body)
        assert advanced.status_code == 201, advanced.text
        answers = deepcopy(first_answers)
        answers["contact_permission"] = "yes"
        body.update(
            request_id=str(uuid.uuid4()),
            revision=advanced.json()["revision"],
            step=0,
            answers=answers,
        )
        back = c.post("/v1/forms/capture-test/partials", json=body)
        assert back.status_code == 201, back.text
        body.update(
            request_id=str(uuid.uuid4()),
            token=back.json()["token"],
            revision=back.json()["revision"],
            step=len(doc["steps"]) - 1,
            answers={**all_answers, "contact_permission": "yes"},
        )
        assert c.post("/v1/forms/capture-test/partials", json=body).status_code == 422
        body["answers"]["detail"] = "Agora respondida"
        assert c.post("/v1/forms/capture-test/partials", json=body).status_code == 201


@pytest.mark.parametrize("kind, minimum", [("choice", 1), ("choices", 1), ("choices", 2)])
def test_back_navigation_removes_future_option_that_is_no_longer_available(deps, kind, minimum):
    doc = partial_document()
    condition = {
        "mode": "all",
        "rules": [{"field": "contact_permission", "operator": "equals", "value": "yes"}],
    }
    doc["steps"][-1]["questions"].append(
        {
            "name": "future_choice",
            "kind": kind,
            "label": "Escolha futura",
            "options": [
                {"value": "base", "label": "Padrão"},
                {"value": "extra", "label": "Outra"},
                {"value": "gated", "label": "Condicionada", "show_when": condition},
            ],
        }
    )
    if kind == "choices":
        doc["steps"][-1]["questions"][-1]["min_choices"] = minimum
    doc["examples"]["answers"]["future_choice"] = "gated" if kind == "choice" else ["base", "gated"]
    import_definition(deps, json.dumps(doc), actor="review")
    fields = {q["name"] for q in doc["steps"][0]["questions"]}
    first_answers = {k: v for k, v in doc["examples"]["answers"].items() if k in fields}
    body = {
        "request_id": str(uuid.uuid4()),
        "step": 0,
        "answers": first_answers,
        "consent": {"privacy": True, "text_version": doc["consent"]["version"]},
    }
    with TestClient(create_app()) as c:
        first = c.post("/v1/forms/capture-test/partials", json=body).json()
        body.update(
            request_id=str(uuid.uuid4()),
            token=first["token"],
            revision=1,
            step=len(doc["steps"]) - 1,
            answers=doc["examples"]["answers"],
        )
        assert c.post("/v1/forms/capture-test/partials", json=body).status_code == 201
        body.update(
            request_id=str(uuid.uuid4()),
            revision=2,
            step=0,
            answers={**first_answers, "contact_permission": "no"},
        )
        back = c.post("/v1/forms/capture-test/partials", json=body)
        assert back.status_code == 201
        assert back.json()["answers"]["future_choice"] == (
            ["base"] if kind == "choices" and minimum == 1 else None
        )
        body.update(
            request_id=str(uuid.uuid4()),
            token=back.json()["token"],
            revision=back.json()["revision"],
        )
        back = c.post("/v1/forms/capture-test/partials", json=body)
        assert back.status_code == 201, back.text
        middle_fields = {q["name"] for step in doc["steps"][:2] for q in step["questions"]}
        body.update(
            request_id=str(uuid.uuid4()),
            token=back.json()["token"],
            revision=back.json()["revision"],
            step=1,
            answers={k: v for k, v in doc["examples"]["answers"].items() if k in middle_fields},
        )
        body["answers"]["contact_permission"] = "no"
        middle = c.post("/v1/forms/capture-test/partials", json=body)
        assert middle.status_code == 201, middle.text
        body.update(
            request_id=str(uuid.uuid4()),
            token=middle.json()["token"],
            revision=middle.json()["revision"],
            step=len(doc["steps"]) - 1,
            answers={
                **doc["examples"]["answers"],
                "contact_permission": "no",
                "future_choice": "base" if kind == "choice" else ["base", "extra"],
            },
        )
        valid = body["answers"]["future_choice"]
        body["answers"]["future_choice"] = doc["examples"]["answers"]["future_choice"]
        assert c.post("/v1/forms/capture-test/partials", json=body).status_code == 422
        body["answers"]["future_choice"] = valid
        assert c.post("/v1/forms/capture-test/partials", json=body).status_code == 201
