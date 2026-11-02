"""Baseline pública: banco vazio e guard real, sem sementes nem cadeia privada."""

from __future__ import annotations

import json
import pprint
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from conftest import reset_test_schema

from app.db import make_engine
from app.deps import build_deps
from app.forms.stored import import_definition
from app.internal_links import AdminPdfAccess
from app.models import Base, Form
from app.settings import Settings

MIGRATIONS = Path(__file__).resolve().parents[1] / "app" / "migrations"


def config() -> Config:
    current = Config()
    current.set_main_option("script_location", str(MIGRATIONS))
    return current


@pytest.fixture
def fresh(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[sa.Engine]:
    """Só recria o schema do banco descartável da própria worker, sem desabilitar o guard."""
    reset_test_schema(database_url)
    monkeypatch.setenv("DATABASE_URL", database_url)
    engine = make_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()
        reset_test_schema(database_url)
        command.upgrade(config(), "head")


def snapshot(engine: sa.Engine) -> dict[str, list[tuple]]:
    with engine.connect() as connection:
        return {
            table: [
                tuple(row)
                for row in connection.exec_driver_sql(f'SELECT * FROM "{table}" ORDER BY 1')
            ]
            for table in sa.inspect(connection).get_table_names(schema="public")
        }


def statements_during(run) -> list[str]:
    statements: list[str] = []

    def receive(connection, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    sa.event.listen(sa.Engine, "before_cursor_execute", receive)
    try:
        run()
    finally:
        sa.event.remove(sa.Engine, "before_cursor_execute", receive)
    return statements


def assert_read_only(statements: list[str]) -> None:
    assert statements
    assert all(sql.lstrip().split()[0].upper() in {"SELECT", "SHOW", "SET"} for sql in statements)


def test_public_revision_graph_has_one_seedless_baseline() -> None:
    scripts = ScriptDirectory.from_config(config())
    assert scripts.get_heads() == ["0021_partial_capture"]
    assert scripts.get_bases() == ["0012"]
    assert scripts.get_revision("0012").down_revision is None
    assert not (MIGRATIONS / "legacy_forms").exists()


def test_empty_database_gets_all_tables_and_no_business_rows(fresh: sa.Engine) -> None:
    command.upgrade(config(), "head")
    with fresh.connect() as connection:
        assert set(sa.inspect(connection).get_table_names()) == set(Base.metadata.tables) | {
            "alembic_version"
        }
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalars().all() == ["0021_partial_capture"]
        assert all(
            connection.exec_driver_sql(f'SELECT count(*) FROM "{table}"').scalar_one() == 0
            for table in Base.metadata.tables
            if table not in {"deployment_contract", "webhook_issuer"}
        )
    command.check(config())


def test_demo_is_imported_explicitly_and_survives_read_only_upgrade(
    fresh: sa.Engine, database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command.upgrade(config(), "head")
    settings = Settings(
        _env_file=None, DATABASE_URL=database_url, LOCAL_STORAGE_DIR=str(tmp_path / "storage")
    )
    deps = build_deps(settings)
    try:
        text = (MIGRATIONS.parents[1] / "examples" / "demo.json").read_text()
        imported = import_definition(deps, text, actor="fixture pública")
        assert imported.slug == "demo"
        with deps.sessions.begin() as session:
            session.execute(sa.update(Form).values(active=False, title="Projeto preservado"))
        before = snapshot(fresh)
        statements = statements_during(lambda: command.upgrade(config(), "head"))
        assert_read_only(statements)
        assert snapshot(fresh) == before
        assert deps.forms.get("demo", 1).example_plan == json.loads(text)["examples"]["plan"]
    finally:
        deps.sessions.kw["bind"].dispose()


@pytest.mark.parametrize(
    "sql",
    [
        "DROP INDEX ix_form_assets_sha256",
        "ALTER TABLE form_versions DISABLE TRIGGER form_versions_are_immutable",
        "ALTER TABLE forms DROP CONSTRAINT ck_forms_retention_days",
        "ALTER TABLE form_assets DISABLE TRIGGER ALL",
        "CREATE RULE hidden_write AS ON INSERT TO leads DO ALSO NOTIFY synthetic_event",
        "CREATE TABLE unexpected (id integer)",
    ],
)
def test_upgrade_at_head_refuses_drift_before_writes(
    fresh: sa.Engine, monkeypatch: pytest.MonkeyPatch, sql: str
) -> None:
    command.upgrade(config(), "head")
    with fresh.begin() as connection:
        connection.exec_driver_sql(sql)
    before = snapshot(fresh)
    statements: list[str] = []

    def refuse() -> None:
        with pytest.raises(RuntimeError, match="estrutura incompatível"):
            command.upgrade(config(), "head")

    statements = statements_during(refuse)
    assert_read_only(statements)
    assert snapshot(fresh) == before


@pytest.mark.parametrize("revision", [None, "0011", "unknown"])
def test_nonempty_database_without_supported_marker_is_refused(
    fresh: sa.Engine, revision: str | None
) -> None:
    with fresh.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE preserved (id integer)")
        connection.exec_driver_sql("INSERT INTO preserved VALUES (7)")
        if revision is not None:
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version (version_num varchar(32) primary key)"
            )
            connection.execute(
                sa.text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": revision}
            )
    before = snapshot(fresh)
    with pytest.raises(RuntimeError, match="ocupado|revisão incompatível"):
        command.upgrade(config(), "head")
    assert snapshot(fresh) == before


def test_downgrade_and_offline_sql_have_no_destructive_escape(fresh: sa.Engine) -> None:
    command.upgrade(config(), "head")
    before = snapshot(fresh)
    with pytest.raises(RuntimeError, match="downgrade|baseline"):
        command.downgrade(config(), "base")
    assert snapshot(fresh) == before
    with pytest.raises(RuntimeError, match="online|offline"):
        command.upgrade(config(), "head", sql=True)
    assert snapshot(fresh) == before


def test_future_contract_can_be_prepared_from_public_sources(
    fresh: sa.Engine, tmp_path: Path
) -> None:
    """A referência da próxima revisão usa só a baseline e o inventário públicos, sem stamp."""
    from app.migrations.compatibility import load_contracts, schema_signature, validate_database

    command.upgrade(config(), "head")
    with fresh.begin() as connection:
        assert validate_database(connection, load_contracts(MIGRATIONS)) == "0021_partial_capture"
        with Operations.context(MigrationContext.configure(connection)) as operations:
            operations.add_column(
                "jobs", sa.Column("contract_example", sa.Integer(), nullable=True)
            )
        signature = schema_signature(connection)
    contracts = load_contracts(MIGRATIONS)
    contracts["0022_example"] = signature
    directory = tmp_path / "public-reference"
    for relative in (
        "env.py",
        "compatibility.py",
        "script.py.mako",
        "versions/0012_leadkit_baseline.py",
        "versions/0013_artifact_availability.py",
        "versions/0014_capture_receipts.py",
        "versions/0015_capture_delivery.py",
        "versions/0016_plan_audiences.py",
        "versions/0017_continuation_release.py",
        "versions/0018_journey_events.py",
        "versions/0019_persisted_webhook_v2.py",
        "versions/0020_draft_test_commands.py",
        "versions/0021_partial_capture.py",
    ):
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((MIGRATIONS / relative).read_bytes())
    (directory / "contracts.py").write_text(
        "SIGNATURES = " + pprint.pformat(contracts, sort_dicts=True)
    )
    (directory / "versions" / "0022_example.py").write_text(
        'from alembic import op\nimport sqlalchemy as sa\nrevision = "0022_example"\ndown_revision = "0021_partial_capture"\n'
        'def upgrade():\n    op.add_column("jobs", sa.Column("contract_example", sa.Integer(), nullable=True))\n'
    )
    reset_test_schema(fresh.url.render_as_string(hide_password=False))
    proposed = Config()
    proposed.set_main_option("script_location", str(directory))
    command.upgrade(proposed, "head")
    with fresh.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0022_example"
        )
        assert validate_database(connection, contracts) == "0022_example"
    assert_read_only(statements_during(lambda: command.upgrade(proposed, "head")))


def test_artifact_migration_preserves_old_writes_and_verifies_history_only_on_download(
    fresh: sa.Engine, database_url: str, tmp_path: Path
) -> None:
    import hashlib
    import uuid
    from datetime import timedelta

    from app.adapters.pdf import minimal_pdf
    from app.admin.actions.jobs import pdf_file
    from app.models import Job, utcnow
    from app.pdf_availability import current_pdf

    command.upgrade(config(), "0012")
    old = sa.MetaData()
    old.reflect(fresh)
    content = minimal_pdf("Plano sintético anterior")
    now = utcnow()

    def old_write(public_id: str) -> uuid.UUID:
        lead, submission, job, plan, artifact = [uuid.uuid4() for _ in range(5)]
        with fresh.begin() as connection:
            connection.execute(
                old.tables["leads"]
                .insert()
                .values(
                    id=lead,
                    email=f"{public_id}@example.test",
                    first_touch={},
                    created_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                old.tables["submissions"]
                .insert()
                .values(
                    id=submission,
                    lead_id=lead,
                    form_slug="synthetic",
                    form_version="synthetic-v1",
                    idempotency_key=public_id,
                    answers={},
                    consent={},
                    tracking={},
                    scores={},
                    created_at=now,
                )
            )
            connection.execute(
                old.tables["jobs"]
                .insert()
                .values(
                    id=job,
                    submission_id=submission,
                    public_id=public_id,
                    status="completed",
                    stage="completed",
                    attempt=1,
                    token_version=1,
                    expires_at=now + timedelta(days=1),
                    completed_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                old.tables["plans"]
                .insert()
                .values(
                    id=plan,
                    job_id=job,
                    version=1,
                    document={},
                    model="synthetic",
                    prompt_version="v1",
                    usage={},
                    created_at=now,
                )
            )
            connection.execute(
                old.tables["artifacts"]
                .insert()
                .values(
                    id=artifact,
                    job_id=job,
                    plan_id=plan,
                    kind="pdf",
                    version=1,
                    storage_key=f"{public_id}.pdf",
                    sha256=hashlib.sha256(content).hexdigest(),
                    byte_size=len(content),
                    renderer="synthetic",
                    created_at=now,
                )
            )
        return job

    before = old_write("historical-before")
    with fresh.connect() as connection:
        legacy = {
            name: [
                tuple(row)
                for row in connection.execute(table.select().order_by(*table.primary_key.columns))
            ]
            for name, table in old.tables.items()
            if name != "alembic_version"
        }
    for revision in (
        "0013",
        "0014_capture",
        "0015_capture_delivery",
        "0016_plan_audiences",
        "0017_continuation_release",
        "0018_journey_events",
        "0019_persisted_webhook_v2",
        "0020_draft_test_commands",
        "0021_partial_capture",
    ):
        command.upgrade(config(), revision)
        with fresh.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == revision
            )
            for name, rows in legacy.items():
                assert [
                    tuple(row)
                    for row in connection.execute(
                        old.tables[name].select().order_by(*old.tables[name].primary_key.columns)
                    )
                ] == rows
    assert_read_only(statements_during(lambda: command.upgrade(config(), "head")))
    after = old_write("historical-after")
    deps = build_deps(
        Settings(
            _env_file=None, DATABASE_URL=database_url, LOCAL_STORAGE_DIR=str(tmp_path / "storage")
        )
    )
    try:
        for job_id, public_id in ((before, "historical-before"), (after, "historical-after")):
            deps.storage.put(f"{public_id}.pdf", content, "application/pdf")
            with deps.sessions() as session:
                job = session.get_one(Job, job_id)
                assert job.status == "completed"
                state = current_pdf(session, deps.storage, job)
                assert state.state == "unknown" and state.verified_at is None
            assert (
                pdf_file(deps, public_id, None, access=AdminPdfAccess(), signed_url=False).content
                == content
            )
            with deps.sessions() as session:
                state = current_pdf(session, deps.storage, session.get_one(Job, job_id))
                assert state.available and state.verified_at is not None
    finally:
        deps.sessions.kw["bind"].dispose()
