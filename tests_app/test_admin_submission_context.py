"""Contexto comercial exato nas costuras HTTP e MCP, com dados sintéticos."""

from admin_api_support import api_client
from test_admin_capture_reading import stored_capture
from test_plan_release import manual_pdf as manual_pdf  # noqa: PLC0414 — fixture compartilhada


def context_url(submission_id):
    return f"/admin/api/v1/submissions/{submission_id}/commercial-context"


def test_capture_context_has_explicit_absence_and_no_lead_data(deps):
    """get_submission_commercial_context conserva a captura sem exigir pedido."""
    submission_id = stored_capture(deps)
    response = api_client(deps, role="leitura").get(
        context_url(submission_id), params={"form": "capture-test"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["submission_id"] == submission_id
    assert body["form_slug"] == "capture-test"
    assert body["form_version_number"] == 1
    assert body["generation"] == "none"
    assert body["historical_version_available"] is True
    assert body["job"] is None
    assert body["lead_data"] is None


def test_pdf_context_correlates_two_submissions_of_same_contact(deps):
    from test_intake import submit

    first = submit(deps, key="first").job
    second = submit(deps, key="second").job
    client = api_client(deps)
    for job in (first, second):
        response = client.get(context_url(job.submission_id), params={"form": "demo"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["generation"] == "pdf"
        assert body["job"]["id"] == str(job.id)
        assert body["job"]["public_id"] == job.public_id
        assert body["job"]["attempt"] == 1
        assert body["job"]["status"] == "queued"
        assert body["job"]["current_pdf"]["state"] == "missing"
        assert body["job"]["release"]["state"] == "automatic"
    assert first.submission_id != second.submission_id


def test_tracking_requires_total_and_explicit_opt_in(deps):
    from test_intake import submit

    job = submit(deps, tracking={"utm_source": "private-campaign", "gclid": "private-click"}).job
    for role in ("leitura", "total"):
        client = api_client(deps, role=role)
        response = client.get(context_url(job.submission_id), params={"form": "demo"})
        assert response.json()["lead_data"] is None
        assert "private-" not in response.text
        requested = client.get(
            context_url(job.submission_id), params={"form": "demo", "include_lead_data": True}
        )
        if role == "leitura":
            assert requested.status_code == 403
            assert requested.json()["code"] == "forbidden_role"
        else:
            assert requested.status_code == 200
            assert requested.json()["lead_data"] == {
                "tracking": {"utm_source": "private-campaign", "gclid": "private-click"}
            }


def test_inconsistent_capture_with_job_is_an_explicit_conflict(deps):
    from datetime import timedelta
    from uuid import UUID

    from app.models import Job, utcnow

    submission_id = stored_capture(deps)
    with deps.sessions.begin() as session:
        session.add(
            Job(
                submission_id=UUID(submission_id),
                public_id="inconsistent",
                expires_at=utcnow() + timedelta(days=1),
            )
        )
    response = api_client(deps).get(context_url(submission_id), params={"form": "capture-test"})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "state_conflict"


def test_context_validates_identity_and_auth_before_sensitive_reads(deps):
    from uuid import uuid4

    from test_intake import submit

    job = submit(deps).job
    client = api_client(deps)
    for submission_id, form in ((job.submission_id, "other-form"), (uuid4(), "demo")):
        response = client.get(context_url(submission_id), params={"form": form})
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"
    assert client.get(context_url(job.submission_id)).status_code == 422
    client.headers.pop("Authorization")
    assert client.get(context_url(job.submission_id), params={"form": "demo"}).status_code == 401


def test_invalid_job_attempt_returns_safe_state_conflict(deps):
    from test_intake import submit

    from app.models import Job

    job = submit(deps, tracking={"utm_source": "private-attempt-marker"}).job
    with deps.sessions.begin() as session:
        session.get_one(Job, job.id).attempt = 0

    response = api_client(deps).get(context_url(job.submission_id), params={"form": "demo"})

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "state_conflict"
    for private_value in ("private-attempt-marker", str(job.id), job.public_id):
        assert private_value not in response.text


def test_missing_tracking_is_not_invented(deps):
    submission_id = stored_capture(deps)
    response = api_client(deps).get(
        context_url(submission_id), params={"form": "capture-test", "include_lead_data": True}
    )
    assert response.json()["lead_data"] == {"tracking": {}}


def test_mcp_uses_same_context_and_protects_tracking(deps, tmp_path):
    from admin_mcp_live import Live
    from test_intake import submit

    job = submit(deps, tracking={"utm_source": "private-campaign"}).job
    arguments = {"submission_id": str(job.submission_id), "form": "demo"}
    name = "get_submission_commercial_context"
    for role, opt_in in (("leitura", False), ("total", False), ("total", True)):
        client = api_client(deps, role=role)
        live = Live(client, tmp_path, lead_data=opt_in)
        try:
            tool = live.tools[name]
            assert ("include_lead_data" in tool.input_schema["properties"]) is opt_in
            params = {"form": "demo"}
            if opt_in:
                params["include_lead_data"] = True
            expected = client.get(context_url(job.submission_id), params=params).json()
            assert live.ok(name, {**arguments, **params}) == expected
        finally:
            live.close()


def test_current_attempt_release_and_read_only_without_storage_calls(deps, manual_pdf, monkeypatch):
    from app.models import Artifact, Job, utcnow

    job_id, artifact_id = manual_pdf
    with deps.sessions() as session:
        job = session.get_one(Job, job_id)
        submission_id, public_id = job.submission_id, job.public_id
    client = api_client(deps)
    url = context_url(submission_id)
    params = {"form": "manual"}
    held = client.get(url, params=params).json()
    assert held["job"]["status"] == "completed"
    assert held["job"]["release"]["state"] == "held"
    assert held["job"]["current_pdf"]["state"] == "unknown"
    from test_plan_release import command_for

    released = client.post(
        f"/admin/api/v1/jobs/{public_id}/release",
        json=command_for(artifact_id).model_dump(mode="json"),
    )
    assert released.status_code == 200, released.text
    external_calls = []

    def forbidden(*args, **kwargs):
        external_calls.append("storage")
        raise AssertionError("A consulta não pode chamar storage")

    for method in ("head", "probe", "get", "signed_url"):
        if hasattr(deps.storage, method):
            monkeypatch.setattr(deps.storage, method, forbidden)
    current = client.get(url, params=params)
    assert current.json()["job"]["release"]["state"] == "released"
    assert current.json()["job"]["current_pdf"]["available"] is False
    assert external_calls == []
    assert client.get(url, params=params).json() == current.json()
    for secret in ("storage_key", "token", "url", "document", "answers", "failure_detail"):
        assert secret not in current.text
    reprocessed = client.post(
        f"/admin/api/v1/jobs/{public_id}/reprocess",
        json={"expected_attempt": 1, "confirm": True, "reason": "Nova tentativa sintética"},
    )
    assert reprocessed.status_code == 200, reprocessed.text
    after = client.get(url, params=params).json()["job"]
    assert after["attempt"] == 2
    assert after["status"] == "queued"
    assert after["current_pdf"]["state"] == "missing"
    assert after["release"]["state"] == "held"
    # Uma falha da tentativa corrente não recupera o PDF nem a autorização antigos.
    with deps.sessions.begin() as session:
        session.get_one(Job, job_id).status = "failed"
        session.get_one(Artifact, artifact_id).purged_at = utcnow()
    failed = client.get(url, params=params).json()["job"]
    assert failed["status"] == "failed"
    assert failed["current_pdf"]["state"] == "missing"
    assert failed["release"]["state"] == "held"


def test_missing_historical_version_does_not_use_current_definition(deps):
    from test_intake import submit

    from app.models import Submission

    job = submit(deps).job
    with deps.sessions.begin() as session:
        session.get_one(Submission, job.submission_id).form_version_number = 999
    response = api_client(deps).get(context_url(job.submission_id), params={"form": "demo"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["historical_version_available"] is False
    assert body["generation"] is None
    assert body["job"]["release"]["state"] == "unavailable"


def test_pdf_submission_without_job_reports_absence(deps):
    from test_intake import submit

    from app.models import Job

    job = submit(deps).job
    with deps.sessions.begin() as session:
        session.delete(session.get_one(Job, job.id))
    response = api_client(deps).get(context_url(job.submission_id), params={"form": "demo"})
    assert response.status_code == 200, response.text
    assert response.json()["generation"] == "pdf"
    assert response.json()["job"] is None


def test_removed_current_pdf_is_explicit_without_probing_storage(deps, manual_pdf):
    from app.models import Artifact, Job, utcnow

    job_id, artifact_id = manual_pdf
    with deps.sessions.begin() as session:
        submission_id = session.get_one(Job, job_id).submission_id
        session.get_one(Artifact, artifact_id).purged_at = utcnow()
    response = api_client(deps).get(context_url(submission_id), params={"form": "manual"})
    body = response.json()["job"]
    assert body["status"] == "completed"
    assert body["current_pdf"]["state"] == "removed"
    assert body["current_pdf"]["available"] is False
