"""Retenção concorrente nas costuras HTTP/MCP, com PostgreSQL real."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import UUID

import pytest
import sqlalchemy as sa
from admin_api_support import api_client
from admin_mcp_live import Live
from test_admin_capture_reading import stored_capture
from test_admin_submission_context import context_url
from test_intake import submit

from app import retention
from app.models import Submission, utcnow


@pytest.mark.parametrize("surface", ["http", "mcp"])
def test_capture_removed_between_metadata_and_tracking_returns_not_found(deps, tmp_path, surface):
    submission_id = stored_capture(deps)
    with deps.sessions.begin() as session:
        session.get_one(Submission, UUID(submission_id)).tracking = {"utm_source": "synthetic"}
    client = api_client(deps)
    live = Live(client, tmp_path, lead_data=True) if surface == "mcp" else None
    entered, resume = Event(), Event()
    connections = {}
    engine = deps.sessions.kw["bind"]

    def pause_tracking(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT submissions.tracking "):
            assert connection.get_isolation_level() == "READ COMMITTED"
            connections["reader"] = connection.connection.driver_connection.info.backend_pid
            entered.set()
            assert resume.wait(10), "A retenção não liberou a leitura de tracking"
        elif statement.startswith("DELETE FROM submissions "):
            assert connection.get_isolation_level() == "READ COMMITTED"
            connections["retention"] = connection.connection.driver_connection.info.backend_pid

    sa.event.listen(engine, "before_cursor_execute", pause_tracking)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            params = {"form": "capture-test", "include_lead_data": True}
            if live:
                reader = pool.submit(
                    live.call,
                    "get_submission_commercial_context",
                    {"submission_id": submission_id, **params},
                )
            else:
                reader = pool.submit(client.get, context_url(submission_id), params=params)
            try:
                assert entered.wait(10), "A consulta não chegou à leitura de tracking"
                report = retention.RetentionReport()
                retention._purge_captures(deps, utcnow() + timedelta(days=3650), report)
                assert report.captures_deleted == 1
                assert connections["reader"] != connections["retention"]
            finally:
                resume.set()
            response = reader.result(timeout=10)
        after = client.get(context_url(submission_id), params={"form": "capture-test"})
    finally:
        sa.event.remove(engine, "before_cursor_execute", pause_tracking)
        if live:
            live.close()
        client.close()
    if live:
        assert response.is_error and response.status == 404, response.text
        assert "not_found" in response.text
    else:
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "not_found"
    assert after.status_code == 404


@pytest.mark.parametrize("role", ["leitura", "total"])
def test_default_and_forbidden_requests_never_select_tracking(deps, tmp_path, role):
    submission_id = stored_capture(deps)
    engine = deps.sessions.kw["bind"]
    selected = []

    def record(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT"):
            selected.append(statement)

    with api_client(deps, role=role) as client:
        live = Live(client, tmp_path, lead_data=True)
        sa.event.listen(engine, "before_cursor_execute", record)
        try:
            args = {"submission_id": submission_id, "form": "capture-test"}
            default = client.get(context_url(submission_id), params={"form": "capture-test"})
            assert default.status_code == 200
            assert default.json()["lead_data"] is None
            assert live.ok("get_submission_commercial_context", args)["lead_data"] is None
            if role == "leitura":
                refused = client.get(
                    context_url(submission_id),
                    params={"form": "capture-test", "include_lead_data": True},
                )
                assert refused.status_code == 403
                assert refused.json()["code"] == "forbidden_role"
                denied = live.call(
                    "get_submission_commercial_context", {**args, "include_lead_data": True}
                )
                assert denied.is_error
        finally:
            sa.event.remove(engine, "before_cursor_execute", record)
            live.close()
    assert selected
    assert all("submissions.tracking" not in statement for statement in selected)


@pytest.mark.parametrize("include_lead_data", [False, True])
def test_request_retained_away_before_job_read_has_controlled_response(deps, include_lead_data):
    job = submit(deps).job
    entered, resume = Event(), Event()
    engine = deps.sessions.kw["bind"]

    def pause_job(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT jobs.id") and "WHERE jobs.submission_id" in statement:
            entered.set()
            assert resume.wait(10), "A retenção não liberou a consulta do pedido"

    sa.event.listen(engine, "before_cursor_execute", pause_job)
    try:
        with api_client(deps) as client, ThreadPoolExecutor(max_workers=1) as pool:
            reader = pool.submit(
                client.get,
                context_url(job.submission_id),
                params={"form": "demo", "include_lead_data": include_lead_data},
            )
            try:
                assert entered.wait(10), "A consulta não chegou ao pedido"
                report = retention.purge_expired(deps, now=utcnow() + timedelta(days=3650))
                assert report.requests_deleted == 1
            finally:
                resume.set()
            response = reader.result(timeout=10)
            after = client.get(context_url(job.submission_id), params={"form": "demo"})
    finally:
        sa.event.remove(engine, "before_cursor_execute", pause_job)
    if include_lead_data:
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "not_found"
    else:
        assert response.status_code == 200, response.text
        assert response.json()["submission_id"] == str(job.submission_id)
        assert response.json()["job"] is None
        assert response.json()["lead_data"] is None
    assert after.status_code == 404
