"""Tentativa e liberação coerentes durante comandos concorrentes reais."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

import pytest
import sqlalchemy as sa
from admin_api_support import api_client
from test_admin_submission_context import context_url
from test_plan_release import command_for
from test_plan_release import manual_pdf as manual_pdf  # noqa: PLC0414 — fixture compartilhada

from app import jobs
from app.admin.queries import submissions
from app.models import Job
from app.plan_release import release_pdf


def wait_for_blocker(deps, waiting_pid, blocker_pid, future):
    """Espera limitada pelo bloqueio real, sem depender da velocidade das threads."""
    deadline = monotonic() + 5
    while monotonic() < deadline:
        with deps.sessions() as session:
            blockers = session.scalar(
                sa.text("SELECT pg_blocking_pids(:pid)"), {"pid": waiting_pid}
            )
        if blocker_pid in blockers:
            assert not future.done()
            return
        if future.done():
            break
        Event().wait(0.02)
    pytest.fail("A transação não aguardou a conexão esperada")


@pytest.mark.parametrize("operation", ["reprocess", "release"])
def test_context_serializes_versioned_writer(deps, manual_pdf, monkeypatch, operation):
    job_id, artifact_id = manual_pdf
    with deps.sessions() as session:
        job = session.get_one(Job, job_id)
        submission_id, public_id = job.submission_id, job.public_id
    entered, resume, writer_ready = Event(), Event(), Event()
    original = submissions.current_pdf
    reader_pid, writer_pid = [], []

    def pause(session, storage, job):
        reader_pid.append(session.scalar(sa.text("SELECT pg_backend_pid()")))
        entered.set()
        assert resume.wait(10), "A escrita não liberou a consulta"
        return original(session, storage, job)

    def writer():
        with deps.sessions.begin() as session:
            session.execute(sa.text("SET LOCAL lock_timeout = '8s'"))
            writer_pid.append(session.scalar(sa.text("SELECT pg_backend_pid()")))
            writer_ready.set()
            if operation == "reprocess":
                return jobs.reprocess(
                    session,
                    job_id,
                    actor="operadora",
                    reason="Ensaio sintético",
                    expected_attempt=1,
                )
            return release_pdf(
                session, deps, public_id, actor="operadora", command=command_for(artifact_id)
            )

    monkeypatch.setattr(submissions, "current_pdf", pause)
    with api_client(deps) as client, ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(client.get, context_url(submission_id), params={"form": "manual"})
        try:
            assert entered.wait(10), "A consulta não chegou ao PDF corrente"
            change = pool.submit(writer)
            assert writer_ready.wait(10), "A escritora não abriu sua conexão"
            wait_for_blocker(deps, writer_pid[0], reader_pid[0], change)
        finally:
            resume.set()
        response = reader.result(timeout=10)
        changed = change.result(timeout=10)
        assert response.status_code == 200, response.text
        before = response.json()["job"]
        assert (
            before["attempt"]
            == before["current_pdf"]["attempt"]
            == before["release"]["attempt"]
            == 1
        )
        assert before["status"] == "completed"
        assert before["current_pdf"]["state"] == "unknown"
        assert before["release"]["state"] == "held"
        assert before["release"]["artifact_id"] == str(artifact_id)
        assert before["release"]["release_version"] == 0
        monkeypatch.setattr(submissions, "current_pdf", original)
        after = client.get(context_url(submission_id), params={"form": "manual"}).json()["job"]
        attempt = 2 if operation == "reprocess" else 1
        assert (
            after["attempt"]
            == after["current_pdf"]["attempt"]
            == after["release"]["attempt"]
            == attempt
        )
        if operation == "reprocess":
            assert changed == 2
            assert after["status"] == "queued"
            assert after["current_pdf"]["state"] == "missing"
            assert after["release"]["state"] == "held"
            assert after["release"]["artifact_id"] is None
        else:
            assert changed.attempt == 1 and changed.release_version == 1
            assert after["status"] == "completed"
            assert after["current_pdf"]["state"] == "unknown"
            assert after["release"]["state"] == "released"
            assert after["release"]["release_version"] == 1
            assert after["release"]["artifact_id"] == str(artifact_id)


@pytest.mark.parametrize("operation", ["reprocess", "release"])
def test_context_waits_for_versioned_writer_that_wins_first(deps, manual_pdf, operation):
    job_id, artifact_id = manual_pdf
    with deps.sessions() as session:
        job = session.get_one(Job, job_id)
        submission_id, public_id = job.submission_id, job.public_id
    written, resume, reader_ready = Event(), Event(), Event()
    writer_pid, reader_pid = [], []
    engine = deps.sessions.kw["bind"]

    def record_reader(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT jobs.id") and "WHERE jobs.submission_id" in statement:
            reader_pid.append(connection.connection.driver_connection.info.backend_pid)
            reader_ready.set()

    def writer():
        with deps.sessions.begin() as session:
            session.execute(sa.text("SET LOCAL lock_timeout = '8s'"))
            writer_pid.append(session.scalar(sa.text("SELECT pg_backend_pid()")))
            if operation == "reprocess":
                assert (
                    jobs.reprocess(
                        session,
                        job_id,
                        actor="operadora",
                        reason="Ensaio sintético",
                        expected_attempt=1,
                    )
                    == 2
                )
            else:
                result = release_pdf(
                    session, deps, public_id, actor="operadora", command=command_for(artifact_id)
                )
                assert result.release_version == 1
            written.set()
            assert resume.wait(10), "A consulta não liberou o commit da escritora"

    sa.event.listen(engine, "before_cursor_execute", record_reader)
    try:
        with api_client(deps) as client, ThreadPoolExecutor(max_workers=2) as pool:
            change = pool.submit(writer)
            try:
                assert written.wait(10), "O comando não concluiu antes da consulta"
                reader = pool.submit(
                    client.get, context_url(submission_id), params={"form": "manual"}
                )
                assert reader_ready.wait(10), "A consulta não chegou ao pedido"
                wait_for_blocker(deps, reader_pid[0], writer_pid[0], reader)
            finally:
                resume.set()
            change.result(timeout=10)
            response = reader.result(timeout=10)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record_reader)
    assert response.status_code == 200, response.text
    body = response.json()["job"]
    attempt = 2 if operation == "reprocess" else 1
    assert (
        body["attempt"] == body["current_pdf"]["attempt"] == body["release"]["attempt"] == attempt
    )
    if operation == "reprocess":
        assert body["status"] == "queued"
        assert body["current_pdf"]["state"] == "missing"
        assert body["release"]["state"] == "held"
        assert body["release"]["artifact_id"] is None
    else:
        assert body["status"] == "completed"
        assert body["current_pdf"]["state"] == "unknown"
        assert body["release"]["state"] == "released"
        assert body["release"]["release_version"] == 1
        assert body["release"]["artifact_id"] == str(artifact_id)
