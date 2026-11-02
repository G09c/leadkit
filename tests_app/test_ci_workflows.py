"""Gates do candidato público: um SHA, checks conclusivos e nenhuma publicação automática."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import yaml
from test_deployment_contract import rollout_engine  # noqa: F401 — fixture PostgreSQL compartilhada

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"


def load(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    return workflow.get("on", workflow.get(True))


def test_untrusted_jobs_do_not_receive_publication_permissions_or_credentials() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        workflow = load(path.name)
        assert workflow["permissions"] == {"contents": "read"}
        assert "pull_request_target" not in triggers(workflow)
        for job_name, job in workflow["jobs"].items():
            if job_name == "publish":
                assert name_of_publisher(workflow) == "publish"
                continue
            assert "write" not in job.get("permissions", {}).values()
            assert job.get("secrets") != "inherit"
            for step in job.get("steps", []):
                assert not step.get("with", {}).get("push", False)
                script = step.get("run", "")
                assert not re.search(
                    r"(?:docker\s+push|gh\s+release\s+create|git\s+push|docker\s+login)", script
                )
                assert "${{ secrets." not in str(step)
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] is False


def test_every_change_reports_the_required_test_and_readiness_checks() -> None:
    workflow = load("tests.yml")
    on = triggers(workflow)
    assert on["push"]["branches"] == ["main", "develop"]
    assert "pull_request" in on
    assert not any("paths" in str(on[event]) for event in ["push", "pull_request"])
    assert workflow["jobs"]["app-tests"].get("if") is None
    assert workflow["jobs"]["ci-ready"]["if"] == "always()"
    assert workflow["jobs"]["ci-ready"]["needs"] == ["app-tests", "packaging"]


def test_image_and_swarm_share_the_tested_commit_without_repeating_the_suite() -> None:
    tests = load("tests.yml")
    packaging = tests["jobs"]["packaging"]
    assert packaging["uses"] == "./.github/workflows/deploy-image.yml"
    assert set(triggers(load("deploy-image.yml"))) == {"workflow_call"}
    assert "concurrency" not in load("deploy-image.yml")
    assert "app-tests" not in load("deploy-image.yml")["jobs"]
    plan = tests["jobs"]["release-plan"]
    assert plan["needs"] == "ci-ready"
    assert "workflow_dispatch" in plan["if"]
    script = next(step["run"] for step in plan["steps"] if step.get("name") == "Pick the version")
    assert "--allow-bootstrap" in script and "BOOTSTRAP_RELEASE" in script
    assert script.index("git ls-remote") < script.index("python3 -m app.version")


def run_gate(job: dict[str, Any], results: dict[str, str], *, swarm_required: bool = True) -> int:
    script = "\n".join(step.get("run", "") for step in job["steps"])
    for name, result in results.items():
        script = script.replace("${{ needs." + name + ".result }}", result)
    script = re.sub(r"\$\{\{ needs\.[^}]+\}\}", "", script)
    return subprocess.run(
        ["bash", "-e", "-c", script],
        capture_output=True,
        check=False,
        env={**os.environ, "SWARM_REQUIRED": str(swarm_required).lower()},
    ).returncode


@pytest.mark.parametrize("test_result", ["failure", "cancelled", "skipped", ""])
def test_failed_or_absent_app_tests_cannot_prepare_a_release(test_result: str) -> None:
    gate = load("tests.yml")["jobs"]["ci-ready"]
    assert run_gate(gate, {"app-tests": test_result, "packaging": "success"}) != 0


@pytest.mark.parametrize("image_result", ["failure", "cancelled", "skipped", ""])
def test_failed_or_absent_image_validation_cannot_prepare_a_release(image_result: str) -> None:
    gate = load("deploy-image.yml")["jobs"]["validation"]
    assert run_gate(gate, {"changes": "success", "image": image_result, "swarm": "success"}) != 0


def test_successful_test_and_packaging_results_open_the_read_only_gate() -> None:
    assert (
        run_gate(
            load("tests.yml")["jobs"]["ci-ready"], {"app-tests": "success", "packaging": "success"}
        )
        == 0
    )


def test_image_smoke_imports_the_demo_explicitly_and_renders_real_pdfs() -> None:
    text = (WORKFLOWS / "deploy-image.yml").read_text()
    assert "legacy_forms" not in text and "/p/mcc" not in text
    assert "examples/demo.json" in text and "import_definition" in text
    assert "WeasyPrintRenderer" in text and "starter.definition" in text
    assert "len(documents) == 2" in text
    assert "python -I" in text


def test_the_wheel_is_built_and_tested_outside_the_source_tree() -> None:
    scripts = "\n".join(s.get("run", "") for s in load("tests.yml")["jobs"]["app-tests"]["steps"])
    assert "uv build --wheel" in scripts
    assert "--no-deps" in scripts and "--no-editable" in scripts
    assert 'python" -I' in scripts
    assert 'cd "$RUNNER_TEMP"' in scripts


def test_the_dockerfile_keeps_version_arguments_and_distribution_notices() -> None:
    text = (ROOT / "Dockerfile").read_text()
    assert 'ARG APP_VERSION=""' in text and 'ARG APP_BUILD_DATE=""' in text
    assert "THIRD_PARTY_NOTICES.md" in text and "LICENSE" in text
    assert "--no-editable" in text


NAMES = ("tests.yml", "deploy-image.yml")
BUILD_ACTION = "docker/build-push-action@"
BUILDX_ACTION = "docker/setup-buildx-action@"


def glob_regex(pattern: str) -> re.Pattern[str]:
    """Os padrões de caminho do Actions: `*` não passa de uma pasta, `**` passa."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def test_the_swarm_runs_on_pushes_and_on_pull_requests_that_touch_what_it_tests() -> None:
    jobs = load("deploy-image.yml")["jobs"]
    assert jobs["swarm"]["needs"] == "changes"
    assert jobs["swarm"]["if"] == "needs.changes.outputs.swarm == 'true'"

    output = jobs["changes"]["outputs"]["swarm"]
    # Fora de um PR (push em main), sempre; num PR para main (promoção e hotfix), sempre; nos
    # outros, só se o filtro achar mudança.
    assert "github.event_name != 'pull_request'" in output
    assert "github.base_ref == 'main'" in output
    assert "steps.filter.outputs.swarm == 'true'" in output

    (filter_step,) = [s for s in jobs["changes"]["steps"] if s.get("id") == "filter"]
    assert filter_step["if"] == "github.event_name == 'pull_request'"
    patterns = yaml.safe_load(filter_step["with"]["filters"])["swarm"]
    regexes = [glob_regex(pattern) for pattern in patterns]
    for path in (
        "deploy/swarm/stack.yaml",
        "deploy/swarm/deploy.sh",
        "deploy/swarm/rollout.py",
        "deploy/entrypoint.sh",
        "Dockerfile",
        ".dockerignore",
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/deploy-image.yml",
        "app/migrations/versions/0008_legacy_forms_as_version_one.py",
        "app/settings.py",
        "app/deployment_contract.py",
        "app/worker.py",
        "app/api/main.py",
    ):
        assert any(regex.match(path) for regex in regexes), path
    # O que só os Tests cobrem não dispara o job mais caro.
    for path in ("app/admin/views.py", "app/forms/definition/__init__.py", "tests_app/conftest.py"):
        assert not any(regex.match(path) for regex in regexes), path


def test_third_party_actions_are_pinned_to_an_exact_version() -> None:
    """Sem tag flutuante (`@v3`): a action só muda quando alguém a troca (CONTRIBUTING.md,
    "Dependências"). As da própria GitHub (`actions/`) seguem a tag maior."""
    floating = []
    for name in NAMES:
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        for use in re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", text, re.MULTILINE):
            owner, _, ref = use.partition("@")
            if owner.startswith(("actions/", "./")):
                continue
            if not re.fullmatch(r"[0-9a-f]{40}|v\d+\.\d+\.\d+", ref):
                floating.append(use)
    assert floating == []


def steps_of(job: str) -> list[dict[str, Any]]:
    return load("deploy-image.yml")["jobs"][job]["steps"]  # type: ignore[no-any-return]


def uses_of(step: dict[str, Any]) -> str:
    return str(step.get("uses", ""))


@pytest.mark.parametrize("job", ["image", "swarm"])
def test_the_validation_builds_reuse_the_docker_layers_from_the_actions_cache(job: str) -> None:
    """A imagem é construída em dois jobs de validação do mesmo commit. O cache de camadas
    (`type=gha`, num escopo só) deixa a base, o apt e as dependências, que mudam pouco, para a
    primeira construção. Um cache que falha não pode reprovar o job (`ignore-error`)."""
    steps = steps_of(job)
    (buildx,) = [s for s in steps if uses_of(s).startswith(BUILDX_ACTION)]
    (build,) = [s for s in steps if uses_of(s).startswith(BUILD_ACTION)]
    # O builder com cache só é usado pelo passo de build: o `docker build` solto de outro passo
    # continua no builder padrão, que enxerga as imagens locais.
    assert buildx["with"]["use"] is False
    assert steps.index(buildx) < steps.index(build)
    options = build["with"]
    assert options["builder"] == f"${{{{ steps.{buildx['id']}.outputs.name }}}}"
    assert options["context"] == "."
    assert options["load"] is True
    assert options["provenance"] is False
    assert options["cache-from"] == "type=gha,scope=leadkit"
    assert options["cache-to"] == "type=gha,mode=max,scope=leadkit,ignore-error=true"


def test_the_builds_do_not_upload_a_build_record_artifact() -> None:
    """O build-push-action sobe por padrão um artefato por build, que ocupa o armazenamento do
    repositório privado."""
    assert load("deploy-image.yml")["env"]["DOCKER_BUILD_RECORD_UPLOAD"] == "false"


def test_the_swarm_rebuild_from_the_local_image_stays_on_the_default_builder() -> None:
    """O passo do `swarm` que reaproveita a tag para outra imagem (`LABEL ci.build=reaproveitada`)
    parte da imagem local `gerador-planos:ci`: só o builder padrão a enxerga, não o do cache."""
    plain = [s["run"] for s in steps_of("swarm") if "docker build" in str(s.get("run", ""))]
    assert len(plain) == 2  # Imagem histórica fixada e reconstrução local da candidata.
    local_rebuilds = [script for script in plain if "FROM gerador-planos:ci" in script]
    assert len(local_rebuilds) == 2
    assert any("LABEL ci.build=reaproveitada" in script for script in local_rebuilds)
    assert any("ci.continuation-return" in script for script in local_rebuilds)
    assert all("--builder" not in script for script in plain)
    assert [s for s in steps_of("image") if "docker build" in str(s.get("run", ""))] == []


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", ""])
def test_failed_or_absent_required_swarm_blocks_readiness(result: str) -> None:
    gate = load("deploy-image.yml")["jobs"]["validation"]
    assert run_gate(gate, {"changes": "success", "image": "success", "swarm": result}) != 0


def test_successful_or_inapplicable_swarm_concludes_the_packaging_gate() -> None:
    gate = load("deploy-image.yml")["jobs"]["validation"]
    assert run_gate(gate, {"changes": "success", "image": "success", "swarm": "success"}) == 0
    assert (
        run_gate(
            gate,
            {"changes": "success", "image": "success", "swarm": "skipped"},
            swarm_required=False,
        )
        == 0
    )
    assert (
        run_gate(
            gate,
            {"changes": "failure", "image": "success", "swarm": "skipped"},
            swarm_required=False,
        )
        != 0
    )


def test_inline_shell_and_python_commands_remain_syntactically_valid() -> None:
    for name in NAMES:
        for job in load(name)["jobs"].values():
            for step in job.get("steps", []):
                if "run" not in step:
                    continue
                script = re.sub(r"\$\{\{.*?\}\}", "checked-value", step["run"])
                result = subprocess.run(
                    ["bash", "-n"], input=script, text=True, capture_output=True, check=False
                )
                assert result.returncode == 0, (name, step["name"], result.stderr)
                for _, snippet in re.findall(
                    r"<<'(PY[A-Z_]*)'\n(.*?)\n\1(?:\n|$)", script, re.DOTALL
                ):
                    compile(snippet, step["name"], "exec")


def name_of_publisher(workflow: dict[str, Any]) -> str:
    writers = [
        name
        for name, job in workflow["jobs"].items()
        if "write" in job.get("permissions", {}).values()
    ]
    assert writers == ["publish"]
    assert workflow["jobs"]["publish"]["permissions"] == {"contents": "write", "packages": "write"}
    return writers[0]


def publisher_allowed(overrides: dict[str, Any]) -> bool:
    values = {
        "github.event_name": "push",
        "github.ref": "refs/heads/main",
        "github.repository": "LevyDeSales/leadkit",
        "github.event.repository.fork": False,
        "vars.LEADKIT_PUBLISH_ENABLED": "",
        "needs.app-tests.result": "success",
        "needs.packaging.result": "success",
        "needs.ci-ready.result": "success",
        **overrides,
    }
    condition = load("tests.yml")["jobs"]["publish"]["if"]
    for clause in condition.split("&&"):
        left, right = [part.strip() for part in clause.split("==")]
        expected = False if right == "false" else right.strip("'")
        if values.get(left) != expected:
            return False
    return True


def test_the_publisher_is_implemented_but_disabled_without_repository_opt_in() -> None:
    assert not publisher_allowed({})
    assert publisher_allowed({"vars.LEADKIT_PUBLISH_ENABLED": "true"})
    job = load("tests.yml")["jobs"]["publish"]
    assert job["needs"] == ["app-tests", "packaging", "ci-ready"]
    assert job["concurrency"]["cancel-in-progress"] is False
    names = [step["name"] for step in job["steps"]]
    assert (
        names.index("Build the image")
        < names.index("Create the release")
        < names.index("Push to GHCR")
    )
    script = next(step["run"] for step in job["steps"] if step["name"] == "Pick the version")
    assert "LEADKIT_RELEASE_BOOTSTRAP" in str(job) and "--allow-bootstrap" in script
    assert "git ls-remote" in script and "exit 1" not in script


@pytest.mark.parametrize(
    "overrides",
    [
        {"github.event_name": "pull_request"},
        {"github.ref": "refs/heads/feature"},
        {"github.repository": "example/leadkit"},
        {"github.event.repository.fork": True},
        *[
            {f"needs.{name}.result": result}
            for name in ("app-tests", "packaging", "ci-ready")
            for result in ("", "failure", "cancelled", "skipped")
        ],
    ],
)
def test_publisher_rejects_forks_prs_other_shas_and_missing_or_failed_checks(
    overrides: dict[str, Any],
) -> None:
    assert not publisher_allowed({"vars.LEADKIT_PUBLISH_ENABLED": "true", **overrides})


def test_swarm_rehearsal_accepts_the_real_guard_refusal(request, tmp_path: Path) -> None:
    """O ensaio precisa reconhecer a recusa real e ainda permitir rollback sem migrar.

    Atualização compatível2→2: Current/Previous e piso são coerentes com o binário.
    Só o transporte Docker é substituído: o deploy.sh e o Alembic/guard usam código real
    e PostgreSQL 16. Não há Swarm local; versões e disponibilidade ficam no ensaio remoto.
    """
    from uuid import uuid4

    from test_swarm_scripts import Server, Staging

    from app.models import DeploymentContract

    engine = request.getfixturevalue("rollout_engine")

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    server = Server(tmp_path, Staging(staging_dir))
    docker = server.bin_dir / "docker"
    docker.rename(server.bin_dir / "docker-stub")
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == run && "${@: -1}" == migrate ]]; then\n'
        '  echo "$*" >> "$STUB_CALLS"\n'
        '  exec "$GUARD_PYTHON" -m alembic -c "$GUARD_ROOT/app/alembic.ini" upgrade head\n'
        "fi\n"
        'if [[ "$1" == run && "$*" == *app.deployment_contract* ]]; then\n'
        '  echo "$*" >> "$STUB_CALLS"\n'
        '  exec "$GUARD_PYTHON" -m app.deployment_contract "${@: -1}"\n'
        "fi\n"
        'exec "$(dirname "$0")/docker-stub" "$@"\n'
    )
    docker.chmod(0o755)
    database_url = engine.url.render_as_string(hide_password=False)
    env = {
        "STUB_NETWORK": "1",
        "STUB_CONTRACT_FLOOR": "3",
        "STUB_TARGET_CONTRACT": "3",
        "GUARD_PYTHON": sys.executable,
        "GUARD_ROOT": str(ROOT),
        "DATABASE_URL": database_url,
        "PYTHONPATH": str(ROOT),
        "IMAGE": "gerador-planos:ci",
    }
    with engine.begin() as connection:
        original_contract = dict(
            connection.execute(sa.select(DeploymentContract.__table__)).mappings().one()
        )
        original = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        connection.exec_driver_sql("UPDATE alembic_version SET version_num = 'ci_futura'")
        connection.execute(
            DeploymentContract.__table__.update().values(
                minimum_contract=3, phase="verified", epoch=uuid4(), attested_at=sa.func.now()
            )
        )
    try:
        refused = server.run("deploy.sh", **env)
        assert refused.returncode != 0
        assert "revisão incompatível" in refused.stderr
        assert "deploy.sh: migrations com " in refused.stdout
        assert any(call.startswith("run ") and call.endswith(" migrate") for call in server.calls)
        assert any(call.startswith("run ") and call.endswith(" begin") for call in server.calls)
        assert not any(
            call.startswith(("stack deploy", "service scale", "service update"))
            for call in server.calls
        )
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "select minimum_contract || ':' || phase from deployment_contract where id=1"
                ).scalar_one()
                == "3:blocked"
            )
        log = tmp_path / "back-sem-rollback.log"
        log.write_text(refused.stdout + refused.stderr)
        rehearsal = next(
            step["run"]
            for step in steps_of("swarm")
            if step.get("name", "").startswith("Update, break and go back")
        )
        # Executa as asserções do próprio workflow contra a saída do deploy real.
        assertions = rehearsal.split('cat "$RUNNER_TEMP/back-sem-rollback.log"')[-1]
        assertions = assertions.split('test "$(versions)"')[0]
        checked = subprocess.run(
            ["bash", "-e", "-c", assertions],
            capture_output=True,
            text=True,
            env={**os.environ, "RUNNER_TEMP": str(tmp_path)},
            check=False,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert "revisão incompatível" in checked.stdout
        assert "a imagem não foi atualizada" in checked.stdout

        server.calls_file.write_text("")
        rollback = server.run("deploy.sh", ROLLBACK="1", **env)
        assert rollback.returncode == 0, rollback.stdout + rollback.stderr
        assert "sem migrations" in rollback.stdout
        assert any(call.startswith("stack deploy") for call in server.calls)
        assert not any(
            call.startswith("run ") and call.endswith(" migrate") for call in server.calls
        )
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == "ci_futura"
            )
    finally:
        with engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": original},
            )
            connection.execute(DeploymentContract.__table__.update().values(**original_contract))


@pytest.mark.parametrize(
    ("failure", "exit_code", "category"),
    [
        (
            "sqlalchemy.exc.OperationalError: password authentication failed",
            1,
            "database_authentication",
        ),
        ("psycopg.OperationalError: could not translate host name", 1, "database_dns"),
        ("psycopg.OperationalError: connection refused", 1, "database_unavailable"),
        ("ModuleNotFoundError: historical.module", 1, "module_missing"),
        ("RuntimeError: estrutura incompatível; objetos: private_table", 1, "schema_guard"),
        ("docker: daemon refused the request", 125, "docker_run"),
        ("unexpected historical failure", 1, "unclassified"),
        ("successful migration with private output", 0, None),
    ],
)
def test_historical_migration_failure_reports_only_safe_category_and_keeps_failure(
    tmp_path: Path, failure: str, exit_code: int, category: str | None
) -> None:
    """Executa o trecho real do workflow; somente o Docker externo é substituído."""
    import json

    rehearsal = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name")
        == "Rehearse historical bootstrap and drained adoption to contract three"
    )
    start = rehearsal.index('docker run --rm --network "${compat}_internal"')
    command = rehearsal[rehearsal.rfind("\n", 0, start) + 1 :]
    command = command[: command.index('test "$(psql_compat')]
    sentinel = "SYNTHETIC_PRIVATE_MIGRATION_CONTENT"
    script = (
        'docker() { printf "%s\\n" "$SYNTHETIC_FAILURE" >&2; return "$SYNTHETIC_EXIT"; }\n'
        + command
        + '\necho "migration failure was ignored"\n'
    )
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        env={
            **os.environ,
            "RUNNER_TEMP": str(tmp_path),
            "compat": "synthetic-compat",
            "old_image": "synthetic:historical",
            "SYNTHETIC_FAILURE": f"{failure}\n{sentinel} postgresql://private@example.test/test",
            "SYNTHETIC_EXIT": str(exit_code),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == exit_code
    if exit_code:
        assert json.loads(result.stdout) == {
            "stage": "historical_migration",
            "exit_code": exit_code,
            "category": category,
        }
    else:
        assert result.stdout == "migration failure was ignored\n"
    assert result.stderr == ""
    assert sentinel not in result.stdout + result.stderr
    assert "private" not in result.stdout + result.stderr


def test_historical_source_is_readable_by_image_user_before_build_without_opening_config(
    tmp_path: Path,
) -> None:
    """Usa tar real e o prefixo real do workflow sob umask077, sem Docker ou download."""
    import io
    import json
    import shlex
    import shutil
    import stat
    import tarfile

    archive = tmp_path / "public-source.tar.gz"
    with tarfile.open(archive, "w:gz") as package:
        for name in ("public-source", "public-source/app"):
            entry = tarfile.TarInfo(name)
            entry.type, entry.mode = tarfile.DIRTYPE, 0o755
            package.addfile(entry)
        entry = tarfile.TarInfo("public-source/app/alembic.ini")
        content = b"[alembic]\nscript_location = app/migrations\n"
        entry.size, entry.mode = len(content), 0o644
        package.addfile(entry, io.BytesIO(content))
    configuration = tmp_path / "private-config"
    configuration.mkdir(mode=0o700)
    env_file = configuration / "app.env"
    env_file.touch(mode=0o600)
    rehearsal = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name")
        == "Rehearse historical bootstrap and drained adoption to contract three"
    )
    prefix = rehearsal[: rehearsal.index('cp "$CONF/stack.yaml"')]
    probe = (
        "import json,stat,sys; from pathlib import Path; p=Path(sys.argv[1]); "
        "print(json.dumps({name:stat.S_IMODE((p/name).stat().st_mode) "
        "for name in ('app','app/alembic.ini')}))"
    )
    script = (
        'curl() { cp "$SYNTHETIC_ARCHIVE" "$RUNNER_TEMP/compat-old.tar.gz"; }\n'
        'tar() { "$TEST_TAR" "$@"; }\n'
        f'docker() {{ "$TEST_PYTHON" -c {shlex.quote(probe)} "$source"; }}\n' + prefix
    )
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        env={
            **os.environ,
            "RUNNER_TEMP": str(tmp_path),
            "SYNTHETIC_ARCHIVE": str(archive),
            "TEST_TAR": shutil.which("gtar") or shutil.which("tar"),
            "TEST_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"app": 0o755, "app/alembic.ini": 0o644}
    assert stat.S_IMODE(configuration.stat().st_mode) == 0o700
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "fault,reason,last_operation",
    [
        ({"STUB_OLD_RUNNING": "1"}, "old_process_alive", "container.inspect"),
        ({"STUB_REAP_RACE": "wait", "STUB_LATE_TASK": "1"}, "inventory_changed", "container.ls"),
    ],
)
def test_early_adoption_refusal_is_diagnosed_without_accepting_it_as_post_ddl_failure(
    tmp_path: Path,
    fault: dict[str, str],
    reason: str,
    last_operation: str,
) -> None:
    """O helper real recusa processo vivo; só Docker é falso e os marcadores continuam obrigatórios."""
    import json

    from test_swarm_scripts import Server, Staging

    staging = tmp_path / "staging"
    staging.mkdir()
    server = Server(tmp_path, Staging(staging))
    rehearsal = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name")
        == "Rehearse historical bootstrap and drained adoption to contract three"
    )
    wrapper_folder = server.folder / "bin"
    wrapper_folder.mkdir()
    wrapper = wrapper_folder / "docker"
    wrapper.write_text(rehearsal.split("cat > bin/docker <<'SH'\n", 1)[1].split("\nSH\n", 1)[0])
    wrapper.chmod(0o700)
    start = rehearsal.index('if env -u DOMAIN -u STACK PATH="$folder/bin:$PATH"')
    script = rehearsal[start : rehearsal.index("cat before-ddl", start)]
    launcher = server.folder / "rehearse.sh"
    launcher.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + script)
    launcher.chmod(0o700)
    result = server.run(
        "rehearse.sh",
        folder=str(server.folder),
        CI_REAL_DOCKER=str(server.bin_dir / "docker"),
        CI_COMPAT_FOLDER=str(server.folder),
        CI_COMPAT_STACK="gerador-planos",
        STUB_NETWORK="1",
        STUB_CONTRACT_FLOOR="0",
        **fault,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "stage": "first_adoption",
        "exit_code": 1,
        "reason": reason,
        "last_operation": last_operation,
        "before_ddl": False,
        "ddl_committed": False,
    }
    assert result.stderr == ""
    assert not (server.folder / "before-ddl").exists()
    assert not (server.folder / "ddl-committed-launcher-failed").exists()


@pytest.mark.parametrize("role", ["api", "worker", "SYNTHETIC_PRIVATE_ROLE"])
def test_bootstrap_err_trap_keeps_exit_cleanup_and_only_reports_safe_location(
    tmp_path: Path, role: str
) -> None:
    import json

    rehearsal = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name")
        == "Rehearse historical bootstrap and drained adoption to contract three"
    )
    prefix = rehearsal[: rehearsal.index('case "${CI_ADOPTION_BASELINE')]
    cleanup = rehearsal[rehearsal.index("cleanup_compat()") :]
    cleanup = cleanup[: cleanup.index("trap cleanup_compat EXIT") + len("trap cleanup_compat EXIT")]
    script = (
        prefix
        + 'docker() { case "$1 ${2:-}" in\n'
        + '  "service inspect") return 37 ;;\n'
        + '  "stack rm") : > "$CLEANUP_MARKER" ;;\n'
        + '  "network inspect") return 1 ;;\n'
        + "esac; }\n"
        + 'python3() { return 0; }; folder="$CLEANUP_FOLDER";\n'
        + cleanup
        + '\nbootstrap_role="$SYNTHETIC_ROLE"\n'
        + 'docker service inspect "$SYNTHETIC_PRIVATE_ARG"\n'
    )
    result = subprocess.run(
        ["bash", "-e", "-u", "-o", "pipefail", "-c", script],
        env={
            **os.environ,
            "compat": "synthetic-compat",
            "SYNTHETIC_ROLE": role,
            "SYNTHETIC_PRIVATE_ARG": "SYNTHETIC_PRIVATE_ARGUMENT",
            "CLEANUP_MARKER": str(tmp_path / "cleanup-ran"),
            "CLEANUP_FOLDER": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 37
    assert (tmp_path / "cleanup-ran").exists()
    assert json.loads(result.stdout) == {
        "stage": "bootstrap_error",
        "line": len(script.splitlines()),
        "exit_code": 37,
        "role": role if role in ("api", "worker") else "none",
    }
    assert result.stderr == ""
    assert "SYNTHETIC_PRIVATE" not in result.stdout


@pytest.mark.parametrize(
    ("arguments", "operation"),
    [
        (["image", "inspect"], "image.inspect"),
        (["image", "tag"], "image.tag"),
        *[
            (["service", action], f"service.{action}")
            for action in ("inspect", "ls", "ps", "scale", "update")
        ],
        (["inspect", "--type", "container"], "container.inspect"),
        (["inspect", "--type", "task"], "task.inspect"),
        (["container", "wait"], "container.wait"),
        (["network", "inspect"], "network.inspect"),
        (["node", "inspect"], "node.inspect"),
        (["info"], "info"),
        (["ps"], "container.ls"),
        (["stack", "deploy"], "stack.deploy"),
        *[
            (["run", "SYNTHETIC_PRIVATE_IMAGE", action], f"run.{action}")
            for action in ("capability", "status", "begin", "attest", "migrate")
        ],
        (["SYNTHETIC_PRIVATE_COMMAND"], "other"),
    ],
)
def test_docker_wrapper_records_only_closed_operation_and_preserves_failure(
    tmp_path: Path, arguments: list[str], operation: str
) -> None:
    rehearsal = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name")
        == "Rehearse historical bootstrap and drained adoption to contract three"
    )
    wrapper = tmp_path / "docker"
    wrapper.write_text(rehearsal.split("cat > bin/docker <<'SH'\n", 1)[1].split("\nSH\n", 1)[0])
    wrapper.chmod(0o700)
    real = tmp_path / "real-docker"
    real.write_text(
        '#!/usr/bin/env bash\nif [[ "${SYNTHETIC_MIGRATE:-0}" == 1 && "$1 ${2:-}" == "service inspect" ]]; then\n'
        "  echo 0; exit 0\nfi\nexit 37\n"
    )
    real.chmod(0o700)
    (tmp_path / "old-containers").touch()
    sent = arguments if operation.startswith("run.") else [*arguments, "SYNTHETIC_PRIVATE_ARGUMENT"]
    result = subprocess.run(
        [str(wrapper), *sent],
        env={
            **os.environ,
            "CI_REAL_DOCKER": str(real),
            "CI_COMPAT_FOLDER": str(tmp_path),
            "CI_COMPAT_STACK": "synthetic-compat",
            "SYNTHETIC_MIGRATE": str(int(operation == "run.migrate")),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 37
    assert (tmp_path / "last-docker-operation").read_text().strip() == operation
    assert result.stdout == result.stderr == ""
    assert not (tmp_path / "ddl-committed-launcher-failed").exists()


def test_bootstrap_evidence_never_publishes_private_helper_logs(tmp_path: Path) -> None:
    rehearsal = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name")
        == "Rehearse historical bootstrap and drained adoption to contract three"
    )
    first = rehearsal[rehearsal.index("test -s before-ddl") :]
    first = first[: first.index('test "$(psql_compat')]
    refused = rehearsal[rehearsal.index('test "$(versions)" = "$before"') :]
    refused = refused.split("# O rollback explícito", 1)[0]
    (tmp_path / "before-ddl").write_text("synthetic-api 1 0 0\n")
    (tmp_path / "ddl-committed-launcher-failed").touch()
    for name in ("first-adoption.log", "refuse-old.log"):
        (tmp_path / name).write_text("SYNTHETIC_PRIVATE_LOG_CONTENT\n")
    result = subprocess.run(
        [
            "bash",
            "-e",
            "-c",
            "versions() { echo synthetic; }; before=synthetic; psql_compat() { echo 3:blocked; };\n"
            + first
            + refused,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "synthetic-api 1 0 0\n"
    assert result.stderr == ""


@pytest.mark.parametrize("operation", ["service.inspect", "SYNTHETIC_PRIVATE_OPERATION"])
def test_first_adoption_report_validates_operation_before_emitting_it(
    tmp_path: Path, operation: str
) -> None:
    import json

    rehearsal = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name")
        == "Rehearse historical bootstrap and drained adoption to contract three"
    )
    parser = rehearsal.split("python3 - \"$adoption_exit\" <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    (tmp_path / "first-adoption.log").write_text(
        "Docker recusou uma operação; SYNTHETIC_PRIVATE_LOG_VALUE\n"
    )
    (tmp_path / "last-docker-operation").write_text(operation)
    result = subprocess.run(
        [sys.executable, "-", "1"],
        input=parser,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "stage": "first_adoption",
        "exit_code": 1,
        "reason": "docker_operation",
        "last_operation": operation if operation == "service.inspect" else "unknown",
        "before_ddl": False,
        "ddl_committed": False,
    }
    assert "SYNTHETIC_PRIVATE" not in result.stdout


def test_migration_wrapper_keeps_controlled_post_commit_failure(tmp_path: Path) -> None:
    rehearsal = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name")
        == "Rehearse historical bootstrap and drained adoption to contract three"
    )
    wrapper = tmp_path / "docker"
    wrapper.write_text(rehearsal.split("cat > bin/docker <<'SH'\n", 1)[1].split("\nSH\n", 1)[0])
    wrapper.chmod(0o700)
    real = tmp_path / "real-docker"
    real.write_text(
        '#!/usr/bin/env bash\nif [[ "$1 ${2:-}" == "service inspect" ]]; then echo 0; fi\nexit 0\n'
    )
    real.chmod(0o700)
    (tmp_path / "old-containers").touch()
    result = subprocess.run(
        [str(wrapper), "run", "SYNTHETIC_PRIVATE_IMAGE", "migrate"],
        env={
            **os.environ,
            "CI_REAL_DOCKER": str(real),
            "CI_COMPAT_FOLDER": str(tmp_path),
            "CI_COMPAT_STACK": "synthetic-compat",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 42
    assert (tmp_path / "before-ddl").stat().st_size > 0
    assert (tmp_path / "ddl-committed-launcher-failed").exists()
    assert (tmp_path / "last-docker-operation").read_text().strip() == "run.migrate"
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize(
    "proof",
    [
        "stopped",
        "destroyed",
        "task_removed",
        "running",
        "missing",
        "list_error",
        "event_error",
        "wrong_id",
        "history_rotated",
        "cached_wrong_id",
    ],
)
def test_bootstrap_proves_old_container_exit_without_requiring_retention(
    tmp_path: Path, proof: str
) -> None:
    from test_swarm_scripts import Server, Staging

    staging = tmp_path / "staging"
    staging.mkdir()
    server = Server(tmp_path, Staging(staging))
    rehearsal = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name")
        == "Rehearse historical bootstrap and drained adoption to contract three"
    )
    wrapper = server.folder / "wrapper-docker"
    wrapper.write_text(rehearsal.split("cat > bin/docker <<'SH'\n", 1)[1].split("\nSH\n", 1)[0])
    wrapper.chmod(0o700)
    baseline = rehearsal[
        rehearsal.index('python3 - "$folder" "$compat" <<\'PYBASE\'') : rehearsal.index(
            "export CI_REAL_DOCKER="
        )
    ]
    script = server.folder / "prove.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        + baseline
        + "\n"
        + (
            "true"
            if proof == "running"
            else "docker service scale gerador-planos_api=0 gerador-planos_worker=0"
        )
        + '\ncid=$(python3 -c \'import json; print(json.load(open("exit-baseline.json"))["roles"]["api"][0]["container_id"])\')\n./wrapper-docker ci-prove-stopped "$cid"\n'
        + (
            'python3 - <<\'PY\'\nimport json\nfrom pathlib import Path\np=Path("exit-baseline.json"); d=json.loads(p.read_text()); next(iter(d["proofs"].values()))["container_id"]="wrong"; p.write_text(json.dumps(d))\nPY\n'
            if proof == "cached_wrong_id"
            else ""
        )
        + './wrapper-docker ci-prove-stopped "$cid"\n'
    )
    script.chmod(0o700)
    faults = {"STUB_REAP": "container"}
    if proof in {"stopped", "running"}:
        faults = {}
    if proof == "task_removed":
        faults = {"STUB_REAP": "task"}
    if proof == "missing":
        faults["STUB_NO_DESTROY"] = "1"
    if proof == "list_error":
        faults["STUB_LOCAL_LIST_FAIL"] = "1"
    if proof == "event_error":
        faults["STUB_EVENTS_FAIL"] = "1"
    if proof == "wrong_id":
        faults["STUB_EVENT_FAULT"] = "id"
    if proof in {"history_rotated", "cached_wrong_id"}:
        faults["STUB_CONSUME_EVENTS"] = "1"
    result = server.run(
        "prove.sh",
        folder=str(server.folder),
        compat="gerador-planos",
        CI_REAL_DOCKER=str(server.bin_dir / "docker"),
        CI_COMPAT_FOLDER=str(server.folder),
        CI_COMPAT_STACK="gerador-planos",
        STUB_NETWORK="1",
        STUB_CONTRACT_FLOOR="0",
        **faults,
    )
    assert (result.returncode == 0) == (
        proof in {"stopped", "destroyed", "task_removed", "history_rotated"}
    )
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize(
    "fault,expected_status,expected_deploys",
    [
        ("normal", 0, 2),
        ("multiple", 0, 2),
        *[
            (name, 1, 1)
            for name in (
                "adoption_db",
                "adoption_db_restart",
                "adoption_image",
                "adoption_extra_image",
                "adoption_health",
                "adoption_stopped",
                "adoption_contract",
            )
        ],
        *[
            (name, 1, 2)
            for name in (
                "second_api",
                "second_worker",
                "second_db",
                "second_db_restart",
                "second_api_container",
                "second_api_restart",
            )
        ],
    ],
)
def test_manual_stack_adoption_precedes_strict_noop(
    tmp_path: Path, fault: str, expected_status: int, expected_deploys: int
) -> None:
    """Executa o bloco real; só o deploy externo e o daemon fornecem metadados sintéticos."""
    run_manual_stack_adoption(
        tmp_path, fault, expected_status=expected_status, expected_deploys=expected_deploys
    )


def run_manual_stack_adoption(
    tmp_path: Path, fault: str, *, expected_status: int, expected_deploys: int
) -> None:
    import json

    configuration = tmp_path / "conf"
    configuration.mkdir()
    (configuration / "app.env").write_text("SYNTHETIC_CONFIGURATION\n")
    state = tmp_path / "deploys.json"
    state.write_text("[]")
    binary = tmp_path / "bin"
    binary.mkdir()
    deploy = configuration / "deploy.sh"
    deploy.write_text(
        f"#!{sys.executable}\n"
        "import json, os\nfrom pathlib import Path\n"
        "p=Path(os.environ['NOOP_STATE']); calls=json.loads(p.read_text())\n"
        "calls.append(os.environ.get('IMAGE')); p.write_text(json.dumps(calls))\n"
    )
    deploy.chmod(0o700)
    docker = binary / "docker"
    docker.write_text(f"#!{sys.executable}\n" + MANUAL_ADOPTION_DOCKER)
    docker.chmod(0o700)
    script = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name") == "Run deploy.sh on the stack deployed by hand"
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "CONF": str(configuration),
            "PATH": f"{binary}:{os.environ['PATH']}",
            "NOOP_STATE": str(state),
            "NOOP_FAULT": fault,
        },
    )
    assert result.returncode == expected_status, result.stdout + result.stderr
    assert json.loads(state.read_text()) == ["gerador-planos:ci"] * expected_deploys
    assert (configuration / "app.env").read_text() == "SYNTHETIC_CONFIGURATION\n"
    assert "SYNTHETIC_PRIVATE" not in result.stdout + result.stderr


MANUAL_ADOPTION_DOCKER = r"""
import hashlib, json, os, sys
from pathlib import Path
args=sys.argv[1:]; phase=len(json.loads(Path(os.environ['NOOP_STATE']).read_text()))
fault=os.environ['NOOP_FAULT']; target='sha256:'+'a'*64

def active(role):
    count=2 if role=='worker' and fault in {'multiple','adoption_extra_image'} else 1
    rows=[]
    for slot in range(1,count+1):
        version='manual' if phase==0 and role!='db' else 'adopted'
        if role=='db': version='original'
        if phase>=1 and fault=='adoption_db' and role=='db': version='replaced'
        if phase>=2 and fault=='second_'+role: version='recreated'
        tid=hashlib.sha256(f'{role}-{slot}-{version}'.encode()).hexdigest()
        cid=hashlib.sha256(tid.encode()).hexdigest()
        if phase>=2 and role=='api' and fault=='second_api_container': cid=hashlib.sha256((cid+'changed').encode()).hexdigest()
        restarted=(role=='db' and ((fault=='adoption_db_restart' and phase>=1) or (fault=='second_db_restart' and phase>=2))) or (role=='api' and fault=='second_api_restart' and phase>=2)
        wrong_image=phase>=1 and ((fault=='adoption_image' and role=='api') or (fault=='adoption_extra_image' and role=='worker' and slot==2))
        unhealthy=phase>=1 and fault=='adoption_health' and role=='worker'
        stopped=phase>=1 and fault=='adoption_stopped' and role=='api'
        metadata={'id':cid,'image':'sha256:'+'b'*64 if wrong_image else target,
                  'running':not stopped,'health':'unhealthy' if unhealthy else 'healthy',
                  'started':'2026-10-21T00:00:00Z' if restarted else '2026-10-20T00:00:00Z',
                  'restarts':1 if restarted else 0}
        rows.append((tid,cid,metadata))
    return rows

if args[:2]==['image','inspect']: print(target)
elif args[:2]==['service','inspect']:
    role=args[-1].rsplit('_',1)[-1]; print(len(active(role)))
elif args[:2]==['service','ps']:
    services=[x for x in args if x.startswith('gerador-planos_')]
    if '-q' in args:
        print('\n'.join(row[0] for name in services for row in active(name.rsplit('_',1)[-1])))
    else:
        print('\n'.join(f'{name}.{slot} {row[0]}' for name in services for slot,row in enumerate(active(name.rsplit('_',1)[-1]),1)))
elif args[:2]==['inspect','--type']:
    kind=args[2]; identity=args[-1]
    row=next((row for role in ('db','api','worker') for row in active(role) if identity==row[0 if kind=='task' else 1]),None)
    if row is None: sys.stderr.write('SYNTHETIC_PRIVATE_DAEMON_ERROR'); raise SystemExit(1)
    print(row[1] if kind=='task' else json.dumps(row[2]))
elif args[0]=='exec': print('3:blocked' if fault=='adoption_contract' else '3:verified')
else:
    sys.stderr.write('SYNTHETIC_PRIVATE_DAEMON_ERROR'); raise SystemExit(2)
"""


@pytest.mark.parametrize(
    "fault,status,deploys",
    [
        ("normal", 0, 1),
        ("same_content", 1, 0),
        ("empty_state", 1, 1),
        ("wrong_image", 1, 1),
        ("no_replacement", 1, 1),
        ("unhealthy", 1, 1),
        ("no_tasks", 1, 1),
        ("db_replaced", 1, 1),
    ],
)
def test_rolling_rehearsal_uses_distinct_content_before_expecting_completed(
    tmp_path: Path, fault: str, status: int, deploys: int
) -> None:
    run_distinct_rolling_rehearsal(tmp_path, fault, status=status, deploys=deploys)


def run_distinct_rolling_rehearsal(
    tmp_path: Path, fault: str, *, status: int, deploys: int
) -> None:
    import json

    state = tmp_path / "rolling.json"
    state.write_text(
        json.dumps(
            {
                "images": {"gerador-planos:ci": "sha256:" + "a" * 64},
                "current": "sha256:" + "a" * 64,
                "updated": False,
                "deploys": 0,
            }
        )
    )
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text(f"#!{sys.executable}\n" + DISTINCT_ROLLING_DOCKER)
    docker.chmod(0o700)
    deploy = tmp_path / "deploy.sh"
    deploy.write_text(
        f"#!{sys.executable}\n"
        + """import json,os
from pathlib import Path
p=Path(os.environ['ROLLING_STATE']); state=json.loads(p.read_text())
target=state['images'][os.environ['IMAGE']]
state['updated']=target!=state['current']; state['current']=target; state['deploys']+=1
p.write_text(json.dumps(state))
"""
    )
    deploy.chmod(0o700)
    full = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name", "").startswith("Update, break and go back")
    )
    start = full.index('echo "::group::1.')
    helpers = full[full.index("state() {") : start]
    group = full[start : full.index('echo "::group::2.', start)]
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", 'cd "$CONF"\n' + helpers + group],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "CONF": str(tmp_path),
            "PATH": f"{binary}:{os.environ['PATH']}",
            "ROLLING_STATE": str(state),
            "ROLLING_FAULT": fault,
        },
    )
    assert result.returncode == status, result.stdout + result.stderr
    recorded = json.loads(state.read_text())
    assert recorded["deploys"] == deploys
    if status == 0:
        assert (
            recorded["images"]["gerador-planos:ci-novo"] != recorded["images"]["gerador-planos:ci"]
        )


DISTINCT_ROLLING_DOCKER = r"""
import hashlib,json,os,sys
from pathlib import Path
p=Path(os.environ['ROLLING_STATE']); state=json.loads(p.read_text()); a=sys.argv[1:]
fault=os.environ['ROLLING_FAULT']; original='sha256:'+'a'*64
def task(role):
    changed=state['updated'] and ((role in {'api','worker'} and fault!='no_replacement') or (role=='db' and fault=='db_replaced'))
    return role+('-new-task' if changed else '-task')
if a[0]=='tag': state['images'][a[-1]]=state['images'][a[-2]]
elif a[0]=='build':
    definition=sys.stdin.read()
    assert 'FROM gerador-planos:ci\n' in definition and 'LABEL ci.build=' in definition
    state['images']['gerador-planos:ci-novo']=original if fault=='same_content' else 'sha256:'+'b'*64
    print(state['images']['gerador-planos:ci-novo'])
elif a[:2]==['image','inspect']: print(state['images'][a[-1]])
elif a[:2]==['service','inspect']:
    fmt=a[a.index('--format')+1]
    print(('completed' if state['updated'] and fault!='empty_state' else '') if 'UpdateStatus' in fmt else 'leadkit-local:'+state['current'].split(':')[1])
elif a[:2]==['service','ps']:
    role=a[-1].rsplit('_',1)[-1]
    if not (state['updated'] and role=='worker' and fault=='no_tasks'): print(task(role))
elif a[:2]==['inspect','--type']:
    if a[2]=='task': print(hashlib.sha256(a[-1].encode()).hexdigest())
    elif 'Health.Status' in a[a.index('--format')+1]: print('unhealthy' if fault=='unhealthy' else 'healthy')
    else: print(original if fault=='wrong_image' and a[-1]==hashlib.sha256(task('worker').encode()).hexdigest() else state['current'])
else: raise SystemExit(2)
p.write_text(json.dumps(state))
"""


@pytest.mark.parametrize(
    "case,fault,expected",
    [
        (4, "valid", 0),
        (4, "missing_migration", 1),
        (4, "verified", 1),
        (4, "wrong_floor", 1),
        (4, "changed_service", 1),
        (5, "valid", 0),
        (5, "unexpected_migration", 1),
        (5, "uncontrolled", 1),
        (5, "changed_service", 1),
    ],
)
def test_preflight_and_migration_evidence_require_distinct_proofs(
    tmp_path: Path, case: int, fault: str, expected: int
) -> None:
    full = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name", "").startswith("Update, break and go back")
    )
    segment = full[
        full.index(f'echo "::group::{case}.') : full.index(f'echo "::group::{case + 1}.')
    ]
    filename = (
        "back-sem-rollback.log"
        if case == 4
        else re.findall(r'cat "\$RUNNER_TEMP/([^"\n]+)"', segment)[-1]
    )
    marker = "deploy.sh: migrations com gerador-planos:ci\n"
    if case == 4:
        content = (
            "" if fault == "missing_migration" else marker
        ) + "revisão incompatível; a imagem não foi atualizada.\n"
        assertions = segment.rsplit(f'cat "$RUNNER_TEMP/{filename}"\n', 1)[1].split(
            "ROLLBACK=1 IMAGE="
        )[0]
    else:
        content = (
            "a stack não foi mexida.\n"
            if fault == "uncontrolled"
            else "O banco não confirmou o contrato; a stack não foi mexida.\n"
        )
        if fault == "unexpected_migration":
            content += marker
        assertions = segment.rsplit(f'cat "$RUNNER_TEMP/{filename}"\n', 1)[1].split(
            'cp "$RUNNER_TEMP/app.env.ok"'
        )[0]
    (tmp_path / filename).write_text(content)
    prefix = 'versions() { echo "$OBSERVED_VERSION"; }; psql_db() { echo "$OBSERVED_CONTRACT"; }; before=original;\n'
    result = subprocess.run(
        ["bash", "-e", "-c", prefix + assertions],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "RUNNER_TEMP": str(tmp_path),
            "OBSERVED_VERSION": "changed" if fault == "changed_service" else "original",
            "OBSERVED_CONTRACT": "3:verified"
            if fault == "verified"
            else "0:blocked"
            if fault == "wrong_floor"
            else "3:blocked",
        },
    )
    assert result.returncode == expected, result.stdout + result.stderr


def test_preflight_unavailable_database_never_calls_begin_or_migrate(tmp_path: Path) -> None:
    import shutil

    from test_swarm_scripts import Server, Staging

    stage = tmp_path / "stage"
    stage.mkdir()
    server = Server(tmp_path, Staging(stage))
    if gnu_sed := shutil.which("gsed"):
        (server.bin_dir / "sed").symlink_to(gnu_sed)
    docker = server.bin_dir / "docker"
    docker.rename(server.bin_dir / "docker-stub")
    docker.write_text(
        '#!/usr/bin/env bash\nif [[ "$1" == run && "${@: -1}" == status ]]; then\n  echo "$*" >> "$STUB_CALLS"\n  exit 1\nfi\nexec "$(dirname "$0")/docker-stub" "$@"\n'
    )
    docker.chmod(0o700)
    server.write_app_env(
        "DATABASE_URL=postgresql+psycopg://synthetic@gerador-planos_db:5432/synthetic_test"
    )
    shutil.copyfile(server.folder / "app.env", tmp_path / "app.env.ok")
    full = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name", "").startswith("Update, break and go back")
    )
    segment = full[full.index('echo "::group::5.') : full.index('echo "::group::6.')]
    launcher = server.folder / "preflight.sh"
    launcher.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nversions() { echo synthetic-services; }\n"
        + segment
    )
    launcher.chmod(0o700)
    result = server.run(
        "preflight.sh",
        RUNNER_TEMP=str(tmp_path),
        STUB_NETWORK="1",
        STUB_CONTRACT_FLOOR="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "O banco não confirmou o contrato" in result.stdout
    assert not any(
        call.startswith("run ") and call.endswith((" begin", " migrate", " attest"))
        for call in server.calls
    )
    assert not any(
        call.startswith(("stack deploy", "service scale", "service update"))
        for call in server.calls
    )


@pytest.mark.parametrize(
    "fault,status",
    [
        ("delayed", 0),
        ("permanent", 1),
        ("list_error", 1),
        ("network_error", 1),
        ("network_pending", 1),
        ("service_pending", 1),
        ("service_error", 1),
    ],
)
def test_volume_redeploy_requires_confirmed_cleanup(
    tmp_path: Path, fault: str, status: int
) -> None:
    run_volume_redeploy_cleanup(tmp_path, fault, status=status)


def run_volume_redeploy_cleanup(tmp_path: Path, fault: str, *, status: int) -> None:
    import json

    from test_swarm_scripts import Server, Staging, rollout_state

    stage = tmp_path / "stage"
    stage.mkdir()
    server = Server(tmp_path, Staging(stage))
    docker = server.bin_dir / "docker"
    docker.rename(server.bin_dir / "docker-stub")
    docker.write_text(f"#!{sys.executable}\n" + VOLUME_CLEANUP_DOCKER)
    docker.chmod(0o700)
    for name, content in (("sleep", "exit 0"), ("seq", "printf '1\\n2\\n3\\n'")):
        path = server.bin_dir / name
        path.write_text("#!/usr/bin/env bash\n" + content + "\n")
        path.chmod(0o700)
    full = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name", "").startswith("Remove the stack, keep the volume")
    )
    script = full.split("for _ in $(seq 1 30)", 1)[0]
    launcher = server.folder / "redeploy.sh"
    launcher.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + script)
    launcher.chmod(0o700)
    result = server.run(
        "redeploy.sh",
        CONF=str(server.folder),
        STUB_NETWORK="1",
        STUB_CONTRACT_FLOOR="1",
        CLEANUP_FAULT=fault,
    )
    assert result.returncode == status, result.stdout + result.stderr
    expected_reason = {
        "delayed": "removed",
        "permanent": "containers_pending",
        "list_error": "containers_unavailable",
        "network_error": "networks_unavailable",
        "network_pending": "network_pending",
        "service_pending": "services_pending",
        "service_error": "services_unavailable",
    }[fault]
    events = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"stage":')
    ]
    assert events == [{"stage": "stack_removal", "reason": expected_reason}]
    assert "SYNTHETIC_PRIVATE" not in result.stdout + result.stderr
    state = rollout_state(server)
    if status == 0:
        assert not state["cleanup_pending"]
        assert state["cleanup_queries"] >= 2
        assert state["floor"] == 1
        assert state["phase"] == "verified"
        assert any(call.startswith("run ") and call.endswith(" migrate") for call in server.calls)
    else:
        assert not any(call.startswith("run ") for call in server.calls)
        assert not any(call.startswith("stack deploy") for call in server.calls)


VOLUME_CLEANUP_DOCKER = r"""
import json,os,subprocess,sys
from pathlib import Path
args=sys.argv[1:]; calls=Path(os.environ['STUB_CALLS']); state_path=Path(str(calls)+'.state')
real=Path(__file__).with_name('docker-stub'); fault=os.environ['CLEANUP_FAULT']
if not state_path.exists(): subprocess.run([str(real),'info'],check=True,capture_output=True)
state=json.loads(state_path.read_text()); handled=True; out=''; code=0
if args[:2]==['stack','rm']:
    state['cleanup_pending']={task['Status']['ContainerStatus']['ContainerID']:task['role'] for task in state['tasks'].values()}
    for container in state['containers'].values(): container['State']['Running']=False
    state['services']={}; state['tasks']={}; state['network']=False
elif args[0]=='ps' and any('com.docker.stack.namespace=' in a for a in args):
    if fault=='list_error': code=1
    if '-aq' in args:
        state['cleanup_queries']=state.get('cleanup_queries',0)+1
        if state['cleanup_queries']>=2 and fault!='permanent':
            for cid in state['cleanup_pending']: state['containers'].pop(cid,None)
            state['cleanup_pending']={}
        out='\n'.join(state['cleanup_pending'])
elif args[0]=='ps' and state.get('cleanup_pending'):
    role=args[-1].rsplit('_',1)[-1]
    out='\n'.join(cid for cid,r in state['cleanup_pending'].items() if r==role)
elif args[:2]==['network','ls']:
    if fault=='network_error': code=1
    out='traefik-public\n'+('gerador-planos_internal' if fault=='network_pending' else '')
elif args[:2]==['service','ls'] and any('com.docker.stack.namespace=' in a for a in args):
    if fault=='service_error': code=1
    if fault=='service_pending': out='gerador-planos_api'
elif args[:2]==['network','inspect'] and not state['network'] and 'gerador-planos_internal' in args:
    code=1
elif args[:2]==['volume','inspect']: out='gerador-planos_pgdata continua'
else: handled=False
if handled:
    with calls.open('a') as file: file.write(' '.join(args)+'\n')
    state_path.write_text(json.dumps(state))
    if out: print(out)
    if code: sys.stderr.write('SYNTHETIC_PRIVATE_DAEMON_ERROR')
    raise SystemExit(code)
os.execv(str(real),[str(real),*args])
"""


def test_post_ddl_rehearsal_accepts_only_the_composed_schema_and_blocked_floor(tmp_path):
    """Executa as verificações SQL reais do workflow contra a resposta do schema composto."""
    rehearsal = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name")
        == "Rehearse historical bootstrap and drained adoption to contract three"
    )
    assertions = rehearsal.split("test -s before-ddl\n", 1)[1].split(
        "for role in api worker; do", 1
    )[0]
    (tmp_path / "before-ddl").write_text("synthetic exit proof\n")
    (tmp_path / "ddl-committed-launcher-failed").touch()
    script = (
        """psql_compat() {
      case "$1" in
        *version_num*) echo 0021_partial_capture ;;
        *) echo 3:blocked ;;
      esac
    }
"""
        + assertions
    )
    result = subprocess.run(
        ["bash", "-e", "-c", script], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_swarm_rehearses_both_initial_adoption_and_the_real_public_contract_one_baseline():
    swarm = load("deploy-image.yml")["jobs"]["swarm"]
    assert swarm["strategy"]["matrix"]["adoption_baseline"] == [
        "pre-projection",
        "contract-one",
        "contract-two",
    ]
    assert swarm["strategy"]["fail-fast"] is False
    rehearsal = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name")
        == "Rehearse historical bootstrap and drained adoption to contract three"
    )
    assert "365f8bf2dc94dbe8894020ed5198fe0142a36ab2" in rehearsal
    assert "ecc009f501d157e3ee246d916e8a13b0cb69e629" in rehearsal
    assert "old_schema=0018_journey_events" in rehearsal
    assert "old_schema=0016_plan_audiences" in rehearsal
    assert "old_schema=0015_capture_delivery" in rehearsal


def test_composed_image_probe_is_isolated_and_checks_real_http_capabilities():
    step = next(
        step
        for step in steps_of("swarm")
        if step.get("name") == "Write the isolated continuation and release probe"
    )
    script = step["run"]
    probe = script.split("<<'PYPROBE'\n", 1)[1].split("\nPYPROBE", 1)[0]
    compile(probe, "continuation-probe", "exec")
    assert 'sys.path.insert(0, "/app")' in probe
    assert 'Path("/app/app/__init__.py")' in probe
    assert "/integrations/v1/handoffs/resolve" in probe
    assert "/release" in probe and "client.get(pdf_path, headers=auth).status_code == 403" in probe
    assert 'pdf.content.startswith(b"%PDF")' in probe
    assert '"replay"] == (phase == "return")' in probe
    assert 'body["release_id"] == state["release_id"]' in probe
    assert 'phase == "blocked"' in probe and "assert_contract(session, required=3)" in probe
    assert 'validate_database(session.connection(), SIGNATURES) == "0021_partial_capture"' in probe


def test_composed_probe_creates_and_replays_both_capabilities_over_http(database_url, tmp_path):
    """Executa o probe real com PG próprio; só caminhos de container e porta ficam locais."""
    import json
    import socket
    from uuid import uuid4

    from test_deployment_contract import evidence

    from app import deployment_contract
    from app.db import make_engine, make_session_factory

    step = next(
        step
        for step in steps_of("swarm")
        if step.get("name") == "Write the isolated continuation and release probe"
    )
    probe = step["run"].split("<<'PYPROBE'\n", 1)[1].split("\nPYPROBE", 1)[0]
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    probe = probe.replace('"/app/app/__init__.py"', repr(str(ROOT / "app/__init__.py")))
    probe = probe.replace('"/app"', repr(str(ROOT)))
    probe = probe.replace('"/proof"', repr(str(tmp_path)))
    probe = probe.replace("18080", str(port))
    probe = probe.replace(
        "except Exception:\n",
        "except Exception as error:\n    import traceback\n    Path("
        + repr(str(tmp_path / "probe-diagnostic.json"))
        + ").write_text(json.dumps([{'line':frame.lineno, 'name':frame.name} for frame in traceback.extract_tb(error.__traceback__)]))\n",
    )
    script = tmp_path / "probe.py"
    script.write_text(probe)
    script.chmod(0o600)
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    with engine.connect() as connection:
        original = dict(
            connection.execute(sa.text("SELECT * FROM deployment_contract WHERE id=1"))
            .one()
            ._mapping
        )
    try:
        for phase in ("baseline", "blocked", "create", "return"):
            if phase == "create":
                # Só a fixture local: o manifesto sintético não atesta uma frota Docker.
                with sessions.begin() as session:
                    epoch = uuid4()
                    deployment_contract.begin_rollout(session, epoch=epoch)
                    deployment_contract.attest_rollout(
                        session, epoch=epoch, evidence=evidence(contract=3)
                    )
            result = subprocess.run(
                [sys.executable, str(script), phase],
                cwd=ROOT,
                env={**os.environ, "DATABASE_URL": database_url},
                capture_output=True,
                text=True,
                check=False,
                timeout=90,
            )
            # O probe já reduz falhas a cenário/resultado; nunca publicar stderr bruto.
            assert result.returncode == 0, result.stdout
            assert json.loads(result.stdout) == {"scenario": phase, "result": "passed", "checks": 1}
            assert not result.stderr
    finally:
        from app.models import DeploymentContract

        with engine.begin() as connection:
            connection.execute(
                sa.update(DeploymentContract).where(DeploymentContract.id == 1).values(**original)
            )
        engine.dispose()


@pytest.mark.parametrize(
    "operation,expected",
    [
        ("run.status", 0),
        ("run.begin", 1),
        ("image.tag", 1),
        ("service.scale", 1),
        ("run.migrate", 1),
        ("other", 1),
    ],
)
def test_return_one_refusal_audits_operations_before_any_mutation(tmp_path, operation, expected):
    rehearsal = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name")
        == "Rehearse historical bootstrap and drained adoption to contract three"
    )
    audit = rehearsal.split("<<'PYNOMUTATE'\n", 1)[1].split("\nPYNOMUTATE", 1)[0]
    trace = tmp_path / "trace"
    trace.write_text("image.inspect\nrun.capability\n" + operation + "\n")
    result = subprocess.run(
        [sys.executable, "-c", audit, str(trace)], capture_output=True, text=True, check=False
    )
    assert result.returncode == expected
    assert not result.stdout and not result.stderr


@pytest.mark.parametrize("proof_exit", [0, 37])
def test_always_cleanup_preserves_volumes_and_private_state_without_exit_proof(
    tmp_path, proof_exit
):
    import json

    step = next(
        step
        for step in steps_of("swarm")
        if step.get("name") == "Clean isolated adoption resources after proven exit"
    )
    assert step["if"] == "always()"
    folder = tmp_path / "swarm-compat"
    (folder / "proof").mkdir(parents=True)
    (folder / "rollout.py").touch()
    state = folder / "proof/state.json"
    state.write_text("SYNTHETIC_PRIVATE_STATE")
    volume = tmp_path / "volume"
    volume.touch()
    binary = tmp_path / "bin"
    binary.mkdir()
    python = binary / "python3"
    python.write_text(
        f"#!{sys.executable}\n"
        + """import os,subprocess,sys
code=sys.stdin.read()
if 'rollout.quiesce()' in code: raise SystemExit(int(os.environ['PROOF_EXIT']))
raise SystemExit(subprocess.run([sys.executable,*sys.argv[1:]],input=code,text=True).returncode)
"""
    )
    python.chmod(0o700)
    docker = binary / "docker"
    docker.write_text("""#!/usr/bin/env bash
case "$1 ${2:-}" in
  "network inspect") exit 1 ;;
  "volume ls") echo leadkit-compat_pgdata ;;
  "volume rm") rm "$SYNTHETIC_VOLUME" ;;
esac
""")
    docker.chmod(0o700)
    result = subprocess.run(
        ["bash", "-e", "-u", "-c", step["run"]],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{binary}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "PROOF_EXIT": str(proof_exit),
            "SYNTHETIC_VOLUME": str(volume),
        },
    )
    assert result.returncode == proof_exit
    assert state.exists() == volume.exists() == bool(proof_exit)
    if not proof_exit:
        assert json.loads(result.stdout) == {"stage": "cleanup", "result": "proved"}
    assert "SYNTHETIC_PRIVATE_STATE" not in result.stdout + result.stderr


@pytest.mark.parametrize("cleanup_kind", ["trap", "always"])
@pytest.mark.parametrize(
    "fault",
    ["delayed", "retired", "alive", "unknown", "foreign", "remove_refused", "volume_unknown"],
)
def test_adoption_cleanup_waits_for_database_exit_and_releases_only_observed_resources(
    tmp_path, cleanup_kind, fault
):
    """A rede já sumiu, mas o banco ainda segura o volume; só Docker/sleep são substituídos."""
    import json

    folder = tmp_path / "swarm-compat"
    (folder / "proof").mkdir(parents=True)
    (folder / "proof/state.json").write_text("SYNTHETIC_PRIVATE_STATE")
    (folder / "rollout.py").touch()
    volume = tmp_path / "volume"
    volume.touch()
    attached = tmp_path / "attached"
    attached.touch()
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "python3").write_text(
        f"#!{sys.executable}\nimport subprocess,sys\ncode=sys.stdin.read()\n"
        "if 'rollout.quiesce()' in code: raise SystemExit(0)\n"
        "raise SystemExit(subprocess.run([sys.executable,*sys.argv[1:]],input=code,text=True).returncode)\n"
    )
    (binary / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (binary / "docker").write_text(
        f"#!{sys.executable}\n"
        + """import os,sys
from pathlib import Path
args=sys.argv[1:]; root=Path(os.environ['RUNNER_TEMP']); fault=os.environ['FAULT']
attached=root/'attached'; volume=root/'volume'; polls=root/'polls'
if args[:2]==['network','inspect']: raise SystemExit(1)
if args[0]=='ps':
    if fault=='retired' and any(a.startswith('volume=') for a in args): raise SystemExit(0)
    if fault=='unknown': raise SystemExit(31)
    if attached.exists(): print('synthetic-db')
elif args[0]=='inspect':
    count=int(polls.read_text())+1 if polls.exists() else 1
    polls.write_text(str(count))
    if fault=='alive' or count==1: print('true:running:leadkit-compat')
    elif fault=='foreign': print('false:exited:other-stack')
    else: print('false:exited:leadkit-compat')
elif args[0]=='rm':
    if fault=='remove_refused': raise SystemExit(32)
    assert args==['rm','synthetic-db'] and int(polls.read_text())>=2
    attached.unlink()
elif args[:2]==['volume','ls']:
    if fault=='volume_unknown': raise SystemExit(33)
    if volume.exists(): print('leadkit-compat_pgdata')
elif args[:2]==['volume','rm']:
    if attached.exists() and fault!='retired': raise SystemExit(34)
    assert args==['volume','rm','leadkit-compat_pgdata']
    volume.unlink()
"""
    )
    for path in binary.iterdir():
        path.chmod(0o700)
    if cleanup_kind == "always":
        script = next(
            s["run"]
            for s in steps_of("swarm")
            if s.get("name") == "Clean isolated adoption resources after proven exit"
        )
    else:
        rehearsal = next(
            s["run"]
            for s in steps_of("swarm")
            if s.get("name")
            == "Rehearse historical bootstrap and drained adoption to contract three"
        )
        script = 'folder="$RUNNER_TEMP/swarm-compat"; compat=leadkit-compat\n'
        script += rehearsal[
            rehearsal.index("cleanup_compat()") : rehearsal.index("trap cleanup_compat EXIT")
        ]
        script += "\ncleanup_compat\n"
    result = subprocess.run(
        ["bash", "-e", "-u", "-c", script],
        env={
            **os.environ,
            "PATH": f"{binary}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "FAULT": fault,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    succeeds = fault in {"delayed", "retired"}
    assert (result.returncode == 0) == succeeds
    assert volume.exists() != succeeds
    if succeeds:
        assert not attached.exists()
        if cleanup_kind == "always":
            assert json.loads(result.stdout) == {"stage": "cleanup", "result": "proved"}
            assert not (folder / "proof/state.json").exists()
    else:
        assert (folder / "proof/state.json").exists()
    assert "SYNTHETIC_PRIVATE" not in result.stdout + result.stderr


@pytest.mark.parametrize("phase", ["SYNTHETIC_PRIVATE_ARGUMENT", "create"])
def test_temporary_probe_refuses_the_wrong_import_location_without_raw_errors(tmp_path, phase):
    import json

    step = next(
        step
        for step in steps_of("swarm")
        if step.get("name") == "Write the isolated continuation and release probe"
    )
    probe = step["run"].split("<<'PYPROBE'\n", 1)[1].split("\nPYPROBE", 1)[0]
    result = subprocess.run(
        [sys.executable, "-c", probe, phase], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "scenario": "create" if phase == "create" else "invalid",
        "result": "failed",
    }
    assert result.stderr == "" and "SYNTHETIC_PRIVATE_ARGUMENT" not in result.stdout


@pytest.mark.parametrize(
    "fault,status",
    [
        ("valid", 0),
        ("db_changed", 1),
        ("data_changed", 1),
        ("blocked", 1),
        ("wrong_previous", 1),
        ("unhealthy", 1),
        ("second_churn", 1),
        ("not_paused", 1),
        ("still_running", 1),
    ],
)
def test_rollback_recovery_rehearsal_allows_normalization_before_asserting_no_churn(
    tmp_path: Path,
    fault: str,
    status: int,
) -> None:
    import json

    state = tmp_path / "recovery.json"
    state.write_text(json.dumps({"deploys": 0}))
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text(f"#!{sys.executable}\n" + RECOVERY_DOCKER)
    docker.chmod(0o700)
    deploy = tmp_path / "deploy.sh"
    deploy.write_text(
        f"#!{sys.executable}\n"
        + """import json,os,subprocess
from pathlib import Path
p=Path(os.environ['RECOVERY_STATE']); state=json.loads(p.read_text())
if state['deploys']==0:
    subprocess.run(['docker','run','synthetic','migrate'], check=True)
state['deploys']+=1
p.write_text(json.dumps(state))
"""
    )
    deploy.chmod(0o700)
    (tmp_path / "app.env.ok").write_text("PUBLIC_BASE_URL=https://example.test\n")
    full = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name", "").startswith("Update, break and go back")
    )
    helpers = full[full.index("state() {") : full.index('echo "::group::1.')]
    group = full[full.index('echo "::group::3.') : full.index('echo "::group::4.')]
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", 'cd "$CONF"\n' + helpers + group],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "CONF": str(tmp_path),
            "RUNNER_TEMP": str(tmp_path),
            "PATH": f"{binary}:{os.environ['PATH']}",
            "RECOVERY_STATE": str(state),
            "RECOVERY_FAULT": fault,
        },
    )
    assert result.returncode == status, result.stdout + result.stderr
    if status == 0:
        assert json.loads(state.read_text())["deploys"] == 2


RECOVERY_DOCKER = r"""
import hashlib,json,os,sys
from pathlib import Path
s=json.loads(Path(os.environ['RECOVERY_STATE']).read_text()); a=sys.argv[1:]
identity='sha256:'+'a'*64; fault=os.environ['RECOVERY_FAULT']
if a[:2]==['service','ps']:
    for name in a:
        if name.startswith('gerador-planos_'):
            role=name.rsplit('_',1)[-1]
            tid=role+('-recovered'+str(s['deploys'] if fault=='second_churn' else 1) if s['deploys'] and (role!='db' or fault=='db_changed') else '-original')
            print(tid if '-q' in a else name+'.1 '+tid)
elif a[:2]==['image','inspect']: print('sha256:'+'b'*64 if a[-1]=='wrong-previous' else identity)
elif a[:2]==['service','inspect']:
    fmt=a[a.index('--format')+1]
    print('completed' if 'UpdateStatus' in fmt else ('1' if s['deploys'] or fault=='not_paused' else '0') if 'Replicas' in fmt else 'wrong-previous' if 'PreviousSpec' in fmt and fault=='wrong_previous' else 'leadkit-local:'+identity.split(':')[1])
elif a[:2]==['inspect','--type']:
    fmt=a[a.index('--format')+1]
    print(hashlib.sha256(a[-1].encode()).hexdigest() if a[2]=='task' else ('unhealthy' if fault=='unhealthy' else 'healthy') if 'Health.Status' in fmt else ('true:running' if fault=='still_running' else 'false:exited') if 'State.Running' in fmt else identity)
elif a[:1]==['ps']: print('db-container' if 'gerador-planos_db' in ' '.join(a) else 'retired-container')
elif a[:1]==['exec']:
    print(('changed-data' if s['deploys'] and fault=='data_changed' else 'stable-data') if 'md5(' in a[-1] else '3:blocked' if not s['deploys'] or fault=='blocked' else '3:verified')
elif a[:1]==['run']: pass
else: raise SystemExit(2)
"""


@pytest.mark.parametrize(
    "fault,status", [("valid", 0), ("http_error", 1), ("curl_error", 1), ("few", 1)]
)
def test_rolling_probe_requires_only_200_and_enough_samples_outside_recovery(
    tmp_path: Path, fault: str, status: int
) -> None:
    full = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name", "").startswith("Update, break and go back")
    )
    helpers = full[full.index('probe="$RUNNER_TEMP/probe.txt"') : full.index("state() {")]
    (tmp_path / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (tmp_path / "sleep").chmod(0o700)
    curl = tmp_path / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        + """import os
from pathlib import Path
folder=Path(os.environ['RUNNER_TEMP']); counter=folder/'samples'
n=int(counter.read_text())+1 if counter.exists() else 1
counter.write_text(str(n))
fault=os.environ['PROBE_FAULT']
if n >= (2 if fault=='few' else 55): (folder/'stop-probe').touch()
print('503' if n==1 and fault=='http_error' else '200')
raise SystemExit(1 if n==1 and fault=='curl_error' else 0)
"""
    )
    curl.chmod(0o700)
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", helpers + '\nwait "$probe_pid"\nstop_probe'],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "RUNNER_TEMP": str(tmp_path),
            "CURL": str(curl),
            "DOMAIN": "example.test",
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "PROBE_FAULT": fault,
        },
    )
    assert result.returncode == status, result.stdout + result.stderr
    # A janela de pausa é limitada ao grupo3, depois do rolling validado e antes do retorno.
    assert full.index('stop_probe\n\necho "::group::3.') < full.index('echo "::group::4.')
    assert full.index('echo "::group::4.') < full.index(
        "start_probe", full.index('echo "::group::4.')
    )
