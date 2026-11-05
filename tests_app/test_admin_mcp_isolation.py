"""O MCP de admin é um programa à parte: não importa o app, não toca o banco, fala MCP por stdio de
verdade (`python -m app.admin_mcp`) e não deixa a chave nem uma linha de log em stdout ou stderr.

- **Isolamento:** um processo novo importa o pacote inteiro (o servidor e o ponto de entrada) e o teste
  confere `sys.modules`: nenhum módulo do app além de `app` e `app.admin_mcp.*`, e nenhuma das
  bibliotecas do banco e do servidor da API. Uma varredura do código (AST) confere o mesmo nas linhas de
  `import`, para um `import` dentro de função não passar.
- **Stdio de verdade:** um processo filho roda `python -m app.admin_mcp` contra uma API HTTP local (uma
  thread com `http.server`, só `127.0.0.1`), e o cliente do SDK lista as ferramentas e chama uma.

Precisa do extra `mcp`; sem ele o arquivo é pulado, e no CI reprova.
"""

from __future__ import annotations

import ast
import asyncio
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from typing import Any, Self

import pytest
from admin_mcp_support import ROOT, new_key, openapi_document, operation_ids, optional

stdio = optional("mcp.client.stdio", module=True)
session_module = optional("mcp.client.session", module=True)

PACKAGE = ROOT / "app" / "admin_mcp"
# O que o servidor da API usa e o MCP não tem por que carregar.
HEAVY = (
    "sqlalchemy",
    "alembic",
    "psycopg",
    "fastapi",
    "hatchet_sdk",
    "boto3",
    "botocore",
    "openai",
    "weasyprint",
    "argon2",
)


# Roda num processo novo: o que o teste do pytest já importou não conta.
IMPORT_THE_PACKAGE = """
import json
import sys

import app.admin_mcp.__main__
import app.admin_mcp.server

heavy = sys.argv[1:]
print(json.dumps({
    "app": sorted(m for m in sys.modules if m == "app" or m.startswith("app.")),
    "heavy": sorted(m for m in sys.modules if m.split(".")[0] in heavy),
}))
"""


def test_importing_the_whole_package_loads_no_module_of_the_app() -> None:
    done = subprocess.run(
        [sys.executable, "-c", IMPORT_THE_PACKAGE, *HEAVY],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    loaded = json.loads(done.stdout)
    assert [m for m in loaded["app"] if m != "app" and not m.startswith("app.admin_mcp")] == []
    assert "app.admin_mcp.server" in loaded["app"]
    assert loaded["heavy"] == []


def test_no_line_of_the_package_imports_the_app() -> None:
    sources = sorted(PACKAGE.glob("*.py"))
    assert {p.name for p in sources} >= {"catalog.py", "client.py", "runner.py", "server.py"}
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [("." * node.level) + (node.module or "")]
                assert node.level == 0, f"{path.name}: import relativo"
                if node.module == "app":
                    names = [f"app.{alias.name}" for alias in node.names]
            else:
                continue
            for name in names:
                assert name.split(".")[0] != "app" or name.startswith("app.admin_mcp"), (
                    f"{path.name} importa {name}"
                )
                assert name.split(".")[0] not in HEAVY, f"{path.name} importa {name}"


def test_only_the_server_module_imports_the_sdk() -> None:
    importing = [
        p.name
        for p in PACKAGE.glob("*.py")
        if any(
            (isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "mcp")
            or (isinstance(n, ast.Import) and any(a.name.split(".")[0] == "mcp" for a in n.names))
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
        )
    ]

    assert importing == ["server.py"]


def test_the_package_runs_as_a_program_and_stops_with_a_message_when_nothing_is_configured() -> (
    None
):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GERADOR_")}

    done = subprocess.run(
        [sys.executable, "-m", "app.admin_mcp"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 2 and done.stdout == ""
    assert "GERADOR_API_URL" in done.stderr


def test_a_bad_port_in_the_address_stops_the_program_cleanly_without_a_traceback() -> None:
    key = new_key()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GERADOR_")}

    done = subprocess.run(
        [sys.executable, "-m", "app.admin_mcp"],
        cwd=ROOT,
        env={**env, "GERADOR_API_URL": "https://planos.exemplo.test:porta", "GERADOR_API_KEY": key},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 2 and done.stdout == ""
    assert "GERADOR_API_URL" in done.stderr and "Traceback" not in done.stderr
    assert key not in done.stderr


# --- stdio de verdade ----------------------------------------------------------------------------


class LocalApi:
    """Uma API HTTP local (127.0.0.1, porta sorteada) que serve o contrato e o que o teste combina."""

    def __init__(self, key: str, role: str = "total") -> None:
        self.key, self.role = key, role
        self.document = json.dumps(openapi_document())
        self.seen: list[tuple[str, str, str | None]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.seen.append((self.command, self.path, self.headers.get("Authorization")))
                if self.headers.get("Authorization") != f"Bearer {outer.key}":
                    return self.reply(
                        401, {"code": "invalid_credentials", "detail": "Chave inválida."}
                    )
                prefix = "/admin/api/v1"
                if self.path == f"{prefix}/openapi.json":
                    return self.reply(200, outer.document, raw=True)
                if self.path == f"{prefix}/me":
                    return self.reply(200, {"key_id": "a1b2c3d4", "name": "x", "role": outer.role})
                if self.path == f"{prefix}/forms?limit=1":
                    return self.reply(200, {"items": [{"slug": "demo"}], "next_cursor": None})
                return self.reply(404, {"code": "not_found", "detail": "Não há operação aqui."})

            def reply(self, status: int, body: Any, *, raw: bool = False) -> None:
                data = (body if raw else json.dumps(body)).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def local_api() -> Iterator[LocalApi]:
    with LocalApi(new_key()) as api:
        yield api


def test_the_program_speaks_mcp_over_stdio_against_a_real_http_api(local_api: LocalApi) -> None:
    params = stdio.StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.admin_mcp"],
        cwd=str(ROOT),
        env={"GERADOR_API_URL": local_api.url, "GERADOR_API_KEY": local_api.key},
    )

    async def scenario(log: Any) -> tuple[Any, Any, Any]:
        async with (
            stdio.stdio_client(params, errlog=log) as (read, write),
            session_module.ClientSession(read, write) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            forms = await session.call_tool("list_forms", {"limit": 1})
            refused = await session.call_tool(
                "remove_asset", {"asset_id": "a", "expected_version": "1"}
            )
            return listed, forms, refused

    with tempfile.TemporaryFile("w+") as log:
        listed, forms, refused = asyncio.run(scenario(log))
        log.seek(0)
        stderr = log.read()

    composites = {
        "attention_overview",
        "diagnose_order",
        "edit_form_draft",
        "publish_form_with_review",
        "connect_baserow_integration",
    }
    generated = operation_ids(openapi_document()) - {
        "download_job_pdf",
        "get_job_plan_projection",
        "render_job_participant_preview",
    }
    assert {t.name for t in listed.tools} == generated | composites  # as geradas seguem ao lado
    assert not forms.isError and json.loads(forms.content[0].text)["items"] == [{"slug": "demo"}]
    assert refused.content[0].text.startswith("CONFIRMAÇÃO NECESSÁRIA")  # o stdio sem elicitation
    assert {auth for _, _, auth in local_api.seen} == {f"Bearer {local_api.key}"}
    assert [path for _, path, _ in local_api.seen] == [
        "/admin/api/v1/openapi.json",
        "/admin/api/v1/me",
        "/admin/api/v1/forms?limit=1",
    ]
    assert "86 ferramentas para a chave de papel total, mais 5 compostas" in stderr
    assert local_api.key not in stderr and local_api.key not in listed.model_dump_json()


def test_a_wrong_key_stops_the_program_with_exit_one_and_no_key_anywhere(
    local_api: LocalApi,
) -> None:
    wrong = new_key()

    done = subprocess.run(
        [sys.executable, "-m", "app.admin_mcp"],
        cwd=ROOT,
        env={
            **os.environ,
            "GERADOR_API_URL": local_api.url,
            "GERADOR_API_KEY": wrong,
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        input="",
    )

    assert done.returncode == 1 and done.stdout == ""
    assert "401 invalid_credentials" in done.stderr and "GERADOR_API_KEY" in done.stderr
    assert wrong not in done.stderr
