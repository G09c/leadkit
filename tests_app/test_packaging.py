"""Contrato do pacote e verificação do wheel instalado, fora da árvore de fontes.

O CI chama este arquivo com `python -I .../test_packaging.py --installed` num ambiente próprio.
Não usa PYTHONPATH, o checkout, banco ou serviços externos para resolver recursos do pacote.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path


def test_package_metadata_and_resource_patterns_cover_the_public_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert project["project"]["name"] == "leadkit"
    assert project["project"]["license"] == "MIT"
    assert "LICENSE" in project["project"]["license-files"]
    assert "THIRD_PARTY_NOTICES.md" in project["project"]["license-files"]
    patterns = project["tool"]["setuptools"]["package-data"]["app"]
    assert "migrations/**/*.json" in patterns
    assert "forms/**/*.json" in patterns
    assert "assets/**/*" in patterns
    assert "web/static/**/*" in patterns


def check_image_package() -> None:
    """Confere a imagem real, inclusive arquivos removidos e imports fora do checkout."""
    import app

    source = Path("/app/app")
    installed = Path(app.__file__).resolve().parent
    assert source.is_dir() and installed.is_relative_to(Path(sys.prefix)), installed
    distribution = importlib.metadata.distribution("leadkit")
    direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
    assert not direct_url.get("dir_info", {}).get("editable", False)

    def files(root: Path) -> dict[str, str]:
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
            and path.name != ".gitkeep"
            and path != root / ".env.example"
            and path != root / "README.md"
        }

    expected, actual = files(source), files(installed)
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    changed = sorted(
        name for name in expected.keys() & actual.keys() if expected[name] != actual[name]
    )
    print(
        json.dumps(
            {
                "source": str(source),
                "installed": str(installed),
                "source_files": len(expected),
                "installed_files": len(actual),
                "missing": missing,
                "extra": extra,
                "changed": changed,
            },
            sort_keys=True,
        )
    )
    assert expected and not missing and not extra and not changed, (
        "o pacote instalado difere da fonte"
    )


def check_installed_package() -> None:
    # O SDK valida o formato ao importar o app; esta credencial sintética não autoriza serviço algum.
    def b64(data: dict[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    claims = {
        "sub": "00000000-0000-0000-0000-000000000001",
        "server_url": "http://127.0.0.1:1",
        "grpc_broadcast_address": "127.0.0.1:1",
    }
    os.environ["HATCHET_CLIENT_TOKEN"] = f"{b64({'alg': 'none'})}.{b64(claims)}.offline"
    os.environ["HATCHET_CLIENT_TLS_STRATEGY"] = "none"
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    import app
    import app.admin_mcp.server
    import app.api.main
    from app.adapters.weasyprint_pdf import WeasyPrintRenderer
    from app.forms import starter
    from app.forms.definition import compile_definition

    root = Path(app.__file__).resolve().parent
    distribution = importlib.metadata.distribution("leadkit")
    assert importlib.metadata.version("mcp") == "1.30.0"
    assert root.is_relative_to(Path(sys.prefix)), root
    assert distribution.metadata["License-Expression"] == "MIT"
    for name in (
        "alembic.ini",
        "migrations/env.py",
        "migrations/compatibility.py",
        "migrations/contracts.py",
        "migrations/script.py.mako",
        "migrations/versions/0012_leadkit_baseline.py",
        "migrations/versions/0014_capture_receipts.py",
        "migrations/versions/0015_capture_delivery.py",
        "migrations/versions/0016_plan_audiences.py",
        "migrations/versions/0017_continuation_release.py",
        "migrations/versions/0018_journey_events.py",
        "migrations/versions/0019_persisted_webhook_v2.py",
        "migrations/versions/0020_draft_test_commands.py",
        "migrations/versions/0021_partial_capture.py",
        "forms/draft_test_commands.py",
        "persisted_webhook.py",
        "capture_deliveries.py",
        "migrations/versions/0013_artifact_availability.py",
        "forms/starter/definition.json",
        "forms/starter/pdf.html",
        "web/static/NOTICE.txt",
        "web/static/vendor/scalar-1.74.0.THIRD-PARTY.txt",
        "assets/fonts/LICENSE-SourceSansPro.txt",
        "assets/fonts/LICENSE-SourceSerifPro.txt",
    ):
        assert (root / name).is_file(), name
    assert not (root / "migrations/legacy_forms").exists()
    assert (
        ScriptDirectory.from_config(Config(str(root / "alembic.ini"))).get_current_head()
        == "0021_partial_capture"
    )
    assert len(list((root / "assets/fonts").glob("*.ttf"))) == 8
    assert list((root / "web/templates").rglob("*.html"))
    assert list((root / "admin/templates").rglob("*.html"))
    json.loads((root / "forms/starter/definition.json").read_text())
    document = starter.definition(slug="wheel-example", title="Exemplo instalado")
    spec = compile_definition(document)
    answers = spec.answers_model.model_validate(document["examples"]["answers"])
    pdf = WeasyPrintRenderer().render(
        spec,
        spec.example_plan,
        {
            "lead": {"name": spec.identity(answers).name},
            "form": {"slug": spec.slug, "title": spec.title},
            "scores": spec.score(answers).scores if spec.score else {},
            "generated_at": datetime.now(UTC),
        },
    )
    assert pdf.content.startswith(b"%PDF") and pdf.page_count > 0
    environment = dict(os.environ)
    for key in ("GERADOR_API_URL", "GERADOR_API_KEY", "PYTHONPATH"):
        environment.pop(key, None)
    mcp = subprocess.run(
        [sys.executable, "-I", "-m", "app.admin_mcp"],
        env=environment,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert mcp.returncode == 2 and not mcp.stdout
    print("installed wheel: runtime, migration resources, MCP and real PDF verified")


if __name__ == "__main__":
    if sys.argv[1:] == ["--installed"]:
        check_installed_package()
    elif sys.argv[1:] == ["--image"]:
        check_image_package()
    else:
        raise SystemExit("use --installed para conferir o wheel ou --image dentro da imagem")
