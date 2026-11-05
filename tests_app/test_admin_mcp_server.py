"""O servidor MCP de admin (`app.admin_mcp.server`) falando o protocolo de verdade, em memória.

O cliente e o servidor são os do SDK, ligados por streams em memória (`create_connected_server_and_client
_session`): o teste lista as ferramentas, chama uma, e responde à *elicitation* como um cliente faria.
A API é a de mentira (`FakeApi`, sem rede). Prova o que só aparece no protocolo: as dicas das ferramentas,
a pergunta à pessoa quando o cliente sabe perguntar (e o caminho da chamada seguinte quando não sabe),
a validação do esquema de entrada pelo SDK, o dado da lead desligado e a chave fora de tudo.

Precisa do extra `mcp` (`uv sync --extra mcp`); sem ele o arquivo é pulado, e no CI reprova.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from admin_mcp_support import answer, body_of, error, new_key, operation_ids, optional, pdf
from admin_mcp_world import World

from app.admin_mcp import __main__ as entry
from app.admin_mcp.confirmation import ConfirmationOutcome
from app.admin_mcp.discovery import StartupError
from app.admin_mcp.redaction import MASK, Redactor

# O SDK é do extra `mcp`: sem ele o arquivo é pulado (no CI, reprova), e estes nomes só existem depois.
types = optional("mcp.types", module=True)
memory = optional("mcp.shared.memory", module=True)
server_module = optional("app.admin_mcp.server", module=True)

Scenario = Callable[[Any], Awaitable[Any]]


def accept(value: bool = True) -> Any:
    return types.ElicitResult(action="accept", content={"confirmar": value})


def elicitor(*replies: Any) -> tuple[Callable[..., Awaitable[Any]], list[Any]]:
    """O cliente que sabe perguntar: devolve `replies` na ordem e guarda o que foi perguntado."""
    asked: list[Any] = []

    async def callback(context: Any, params: Any) -> Any:
        asked.append(params)
        return replies[len(asked) - 1]

    return callback, asked


def session_run(
    world: World, scenario: Scenario, *, elicit: Callable[..., Any] | None = None
) -> Any:
    server = server_module.build_server(world.runner, world.tools.values())

    async def main() -> Any:
        async with memory.create_connected_server_and_client_session(
            server, elicitation_callback=elicit
        ) as session:
            return await scenario(session)

    return asyncio.run(main())


def text_of(result: Any) -> str:
    (block,) = result.content
    assert block.type == "text"
    return str(block.text)


@pytest.fixture
def world(tmp_path: Path) -> Any:
    found = World(tmp_path)
    yield found
    found.close()


# --- o que o servidor oferece --------------------------------------------------------------------


def test_the_server_lists_one_tool_per_operation_the_key_allows(world: World) -> None:
    listed = session_run(world, lambda session: session.list_tools())

    names = {tool.name for tool in listed.tools}
    assert names == operation_ids(world.api.document) - {
        "download_job_pdf",
        "get_job_plan_projection",
        "render_job_participant_preview",
    }
    assert len(listed.tools) == 86
    for tool in listed.tools:
        spec = world.tools[tool.name]
        assert tool.description == spec.description and tool.title == spec.summary
        assert tool.inputSchema == spec.input_schema


def test_the_hints_reach_the_client_for_every_tool(world: World) -> None:
    listed = session_run(world, lambda session: session.list_tools())

    document = world.api.document
    operations = {
        operation["operationId"]: (method, operation)
        for item in document["paths"].values()
        for method, operation in item.items()
        if method in ("get", "put", "post", "delete", "patch")
    }
    for tool in listed.tools:
        method, operation = operations[tool.name]
        assert tool.annotations.readOnlyHint is (
            (method == "get" or operation["x-required-role"] == "leitura")
            and not operation.get("x-destructive", False)
        )
        assert tool.annotations.destructiveHint is bool(operation.get("x-destructive", False))
        assert tool.annotations.openWorldHint is bool(operation.get("x-external-effect", False))


def test_a_leitura_key_gets_only_the_leitura_tools(tmp_path: Path) -> None:
    reader = World(tmp_path, "leitura")

    listed = session_run(reader, lambda session: session.list_tools())

    assert len(listed.tools) == 41
    assert all(tool.annotations.readOnlyHint for tool in listed.tools)
    assert {"publish_form_draft", "upload_asset", "download_job_pdf"}.isdisjoint(
        tool.name for tool in listed.tools
    )
    reader.close()


def test_the_lead_data_is_off_by_default_and_on_with_the_option(
    world: World, tmp_path: Path
) -> None:
    off = {t.name: t for t in session_run(world, lambda s: s.list_tools()).tools}
    on_world = World(tmp_path, lead_data=True)
    on = {t.name: t for t in session_run(on_world, lambda s: s.list_tools()).tools}

    assert "download_job_pdf" not in off and "download_job_pdf" in on
    for name in ("list_jobs", "get_job"):
        assert "include_lead_data" not in off[name].inputSchema["properties"]
        assert "include_lead_data" in on[name].inputSchema["properties"]
    assert "q" not in off["list_jobs"].inputSchema["properties"]
    assert "q" in on["list_jobs"].inputSchema["properties"]
    on_world.close()


def test_the_server_tells_the_model_not_to_confirm_for_the_person() -> None:
    assert "nunca confirme por ela" in server_module.INSTRUCTIONS


def test_the_key_is_in_nothing_the_client_can_read(world: World) -> None:
    world.api.when("GET", "/forms", error(500, "internal_error", f"eco {world.key}"))

    async def scenario(session: Any) -> list[str]:
        listed = await session.list_tools()
        failed = await session.call_tool("list_forms", {})
        return [listed.model_dump_json(), failed.model_dump_json()]

    for text in session_run(world, scenario):
        assert world.key not in text and world.key.split("_", 2)[2] not in text
    assert MASK in text


# --- chamar uma ferramenta pelo protocolo --------------------------------------------------------


def test_a_read_goes_through_and_comes_back_as_text(world: World) -> None:
    world.api.when("GET", "/forms/{slug}", answer(200, {"slug": "demo"}))

    result = session_run(world, lambda s: s.call_tool("get_form", {"slug": "demo"}))

    assert not result.isError and json.loads(text_of(result)) == {"slug": "demo"}


def test_the_sdk_validates_the_input_against_the_generated_schema(world: World) -> None:
    async def scenario(session: Any) -> list[Any]:
        return [
            await session.call_tool("get_form", {}),  # falta o slug
            await session.call_tool("get_form", {"slug": "demo", "extra": 1}),
            await session.call_tool("list_forms", {"limit": "muitos"}),
            await session.call_tool("list_forms", {"limit": 500}),  # passa do máximo (200)
            await session.call_tool("get_job", {"public_id": "x", "include_lead_data": True}),
        ]

    results = session_run(world, scenario)

    assert all(r.isError for r in results)
    assert all("Input validation error" in text_of(r) for r in results)
    assert world.api.calls() == []


def test_a_tool_the_key_cannot_use_does_not_exist_for_it(tmp_path: Path) -> None:
    reader = World(tmp_path, "leitura")

    result = session_run(
        reader, lambda s: s.call_tool("publish_form_draft", {"slug": "a", "expected_version": "1"})
    )

    assert result.isError and "não existe neste servidor" in text_of(result)
    assert reader.api.calls() == []
    reader.close()


def test_an_api_error_comes_back_as_a_failed_call(world: World) -> None:
    world.api.when("GET", "/forms/{slug}", error(404, "not_found", "O formulário não existe."))

    result = session_run(world, lambda s: s.call_tool("get_form", {"slug": "nada"}))

    assert result.isError and "404 not_found" in text_of(result)


def test_a_pdf_comes_back_as_a_path_and_never_as_bytes(world: World, tmp_path: Path) -> None:
    world.api.when("POST", "/forms/{slug}/draft/pdf-preview", pdf(b"%PDF-1.7 " + b"x" * 5000))

    result = session_run(
        world, lambda s: s.call_tool("render_form_draft_pdf_preview", {"slug": "a"})
    )

    assert not result.isError and len(result.content) == 1
    assert str(tmp_path / "downloads") in text_of(result) and "x" * 100 not in text_of(result)


# --- a confirmação pelo protocolo ----------------------------------------------------------------


def test_a_client_that_can_ask_is_asked_and_the_yes_runs_the_operation(world: World) -> None:
    world.api.when("DELETE", "/assets/{asset_id}", answer(200, {"removed": True}))
    callback, asked = elicitor(accept(True))

    result = session_run(
        world,
        lambda s: s.call_tool("remove_asset", {"asset_id": "a1", "expected_version": "2"}),
        elicit=callback,
    )

    assert not result.isError and json.loads(text_of(result)) == {"removed": True}
    (params,) = asked
    assert "Confirma «" in params.message and "algo difícil de desfazer" in params.message
    assert "asset_id=a1" in params.message
    assert params.requestedSchema["required"] == ["confirmar"]
    assert params.requestedSchema["properties"]["confirmar"]["type"] == "boolean"
    (request,) = world.api.calls()
    assert dict(request.url.params) == {"expected_version": "2", "confirm": "true"}


@pytest.mark.parametrize(
    "reply",
    [
        types.ElicitResult(action="decline"),
        types.ElicitResult(action="cancel"),
        types.ElicitResult(action="accept", content={"confirmar": False}),
        types.ElicitResult(action="accept", content={}),
    ],
)
def test_anything_but_a_checked_yes_leaves_the_operation_undone(world: World, reply: Any) -> None:
    callback, asked = elicitor(reply)

    result = session_run(
        world,
        lambda s: s.call_tool("remove_asset", {"asset_id": "a1", "expected_version": "2"}),
        elicit=callback,
    )

    assert result.isError and "não confirmou" in text_of(result)
    assert len(asked) == 1 and world.api.calls() == []


def test_the_confirm_the_model_writes_does_not_skip_the_question(world: World) -> None:
    callback, asked = elicitor(types.ElicitResult(action="decline"))

    result = session_run(
        world,
        lambda s: s.call_tool(
            "remove_asset", {"asset_id": "a1", "expected_version": "2", "confirm": True}
        ),
        elicit=callback,
    )

    assert result.isError and len(asked) == 1 and world.api.calls() == []


def test_a_client_that_cannot_ask_gets_the_description_and_runs_on_the_next_call(
    world: World,
) -> None:
    world.api.when("DELETE", "/assets/{asset_id}", answer(200, {"removed": True}))
    arguments = {"asset_id": "a1", "expected_version": "2"}

    async def scenario(session: Any) -> tuple[Any, Any]:
        first = await session.call_tool("remove_asset", arguments)
        calls_after_first = len(world.api.calls())
        second = await session.call_tool("remove_asset", {**arguments, "confirm": True})
        return first, (calls_after_first, second)

    first, (calls_after_first, second) = session_run(world, scenario)

    assert not first.isError and text_of(first).startswith("CONFIRMAÇÃO NECESSÁRIA")
    assert calls_after_first == 0
    assert not second.isError and json.loads(text_of(second)) == {"removed": True}
    assert len(world.api.calls()) == 1


def test_the_dry_run_publication_needs_no_question_over_the_protocol(world: World) -> None:
    world.api.when("POST", "/forms/{slug}/draft/publish", answer(200, {"missing": []}))
    callback, asked = elicitor()

    result = session_run(
        world,
        lambda s: s.call_tool(
            "publish_form_draft", {"slug": "demo", "expected_version": "3", "dry_run": True}
        ),
        elicit=callback,
    )

    assert not result.isError and asked == []
    assert body_of(world.api.calls()[0]) == {"expected_version": "3", "dry_run": True}


def test_a_contact_refusal_is_asked_in_the_same_session(world: World) -> None:
    detail = "O prompt manda o nome e o e-mail da lead ao provedor do modelo."

    def reply(request: httpx.Request) -> httpx.Response:
        if body_of(request).get("confirm_contact"):
            return answer(200, {"version": 4})
        return error(422, "confirmation_required", detail)

    world.api.when("POST", "/forms/{slug}/draft/publish", reply)
    callback, asked = elicitor(accept(), accept())

    result = session_run(
        world,
        lambda s: s.call_tool(
            "publish_form_draft", {"slug": "demo", "expected_version": "3", "note": "Novo prompt"}
        ),
        elicit=callback,
    )

    assert not result.isError and json.loads(text_of(result)) == {"version": 4}
    assert detail in asked[1].message


# --- a pergunta: quem sabe perguntar -------------------------------------------------------------


@pytest.mark.parametrize(
    ("elicitation", "expected"),
    [
        (None, False),
        ("vazia", True),  # `elicitation: {}` vale como formulário (a regra de compatibilidade)
        ("formulario", True),
        ("so-url", False),
        ("os-dois", True),
    ],
)
def test_only_a_client_with_form_elicitation_can_be_asked(
    elicitation: str | None, expected: bool
) -> None:
    capability = {
        None: None,
        "vazia": types.ElicitationCapability(),
        "formulario": types.ElicitationCapability(form=types.FormElicitationCapability()),
        "so-url": types.ElicitationCapability(url=types.UrlElicitationCapability()),
        "os-dois": types.ElicitationCapability(
            form=types.FormElicitationCapability(), url=types.UrlElicitationCapability()
        ),
    }[elicitation]
    session = SimpleNamespace(
        client_params=SimpleNamespace(capabilities=SimpleNamespace(elicitation=capability))
    )

    assert server_module.SessionPrompter(session, 1).can_ask() is expected


def test_a_session_that_has_not_initialized_cannot_be_asked() -> None:
    assert server_module.SessionPrompter(SimpleNamespace(client_params=None), 1).can_ask() is False


def test_a_question_that_fails_is_not_a_yes() -> None:
    class Broken:
        client_params = SimpleNamespace(
            capabilities=SimpleNamespace(elicitation=types.ElicitationCapability())
        )

        async def elicit_form(self, **_: Any) -> None:
            raise RuntimeError("o cliente caiu")

    assert (
        asyncio.run(server_module.SessionPrompter(Broken(), 1).ask("Confirma?"))
        is ConfirmationOutcome.UNAVAILABLE
    )


# --- o ponto de entrada --------------------------------------------------------------------------


GOOD_ENV = {"GERADOR_API_URL": "https://planos.exemplo.test"}


def run_main(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> int:
    for name in ("GERADOR_API_URL", "GERADOR_API_KEY", "GERADOR_MCP_LEAD_DATA"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return entry.main()


def test_a_missing_variable_stops_the_start_with_a_message_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_main(monkeypatch, {"GERADOR_API_KEY": new_key()}) == 2

    captured = capsys.readouterr()
    assert "GERADOR_API_URL" in captured.err and captured.out == ""


def test_a_bad_configuration_never_prints_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = new_key()

    code = run_main(
        monkeypatch, {**GOOD_ENV, "GERADOR_API_KEY": key, "GERADOR_MCP_LEAD_DATA": "sim"}
    )

    captured = capsys.readouterr()
    assert code == 2 and "GERADOR_MCP_LEAD_DATA" in captured.err
    assert key not in captured.err and captured.out == ""


@pytest.mark.parametrize(
    "bad",
    [
        "https://planos.exemplo.test:abc",
        "https://planos.exemplo.test:99999",
        "https://[::1",
        "ftp://planos.exemplo.test",
    ],
)
def test_a_bad_address_exits_two_with_a_message_and_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    key = new_key()

    code = run_main(monkeypatch, {"GERADOR_API_URL": bad, "GERADOR_API_KEY": key})

    captured = capsys.readouterr()
    assert code == 2 and "GERADOR_API_URL" in captured.err
    assert "Traceback" not in captured.err and key not in captured.err and captured.out == ""


def test_an_upload_asks_the_person_with_the_resolved_path_and_the_size_over_the_protocol(
    world: World,
) -> None:
    picture = world.uploads / "logo.png"
    content = b"\x89PNG\r\n\x1a\n" + b"bytes sinteticos de uma imagem"
    picture.write_bytes(content)
    world.api.when("POST", "/assets", answer(201, {"id": "a1"}))
    callback, asked = elicitor(accept(True))

    result = session_run(
        world,
        lambda s: s.call_tool("upload_asset", {"name": "logo-demo", "file": str(picture)}),
        elicit=callback,
    )

    assert not result.isError and json.loads(text_of(result)) == {"id": "a1"}
    (params,) = asked
    assert f"Arquivo (file): {picture.resolve()} ({len(content)} bytes)" in params.message
    assert "servida sem login" in params.message


def test_the_upload_tools_offer_a_confirm_that_the_api_never_sees(world: World) -> None:
    listed = session_run(world, lambda session: session.list_tools())
    upload = next(tool for tool in listed.tools if tool.name == "upload_asset")

    assert upload.inputSchema["properties"]["confirm"]["type"] == "boolean"
    assert upload.annotations.destructiveHint is False  # as dicas seguem as marcas da API


def test_a_failed_start_exits_one_and_prints_the_message_without_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = new_key()

    def refuse(config: Any) -> None:
        raise StartupError(f"A API recusou {key}")

    monkeypatch.setattr(server_module, "run", refuse)

    assert run_main(monkeypatch, {**GOOD_ENV, "GERADOR_API_KEY": key}) == 1

    captured = capsys.readouterr()
    assert MASK in captured.err and key not in captured.err and captured.out == ""


def test_the_server_runs_with_the_configuration_of_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(server_module, "run", seen.append)
    key = new_key()

    assert (
        run_main(monkeypatch, {**GOOD_ENV, "GERADOR_API_KEY": key, "GERADOR_MCP_LEAD_DATA": "1"})
        == 0
    )

    (config,) = seen
    assert config.base_url == "https://planos.exemplo.test/admin/api/v1"
    assert config.api_key == key and config.lead_data is True


def test_ctrl_c_is_a_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(config: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(server_module, "run", interrupt)

    assert run_main(monkeypatch, {**GOOD_ENV, "GERADOR_API_KEY": new_key()}) == 0


def test_without_the_sdk_the_entry_point_says_which_extra_to_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delitem(sys.modules, "app.admin_mcp.server")
    monkeypatch.setitem(sys.modules, "mcp", None)

    code = run_main(monkeypatch, {**GOOD_ENV, "GERADOR_API_KEY": new_key()})

    assert code == 2 and "uv sync --locked --extra mcp" in capsys.readouterr().err


def test_the_log_filter_is_installed_on_the_stderr_handler(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = new_key()
    root, mine = logging.getLogger(), logging.getLogger("gerador_mcp")
    saved = (list(root.handlers), root.level, mine.level, mine.disabled)
    # O `fileConfig` das migrations (outro teste do mesmo processo) desliga os loggers que já existem.
    mine.disabled = False
    try:
        entry.configure_logging(Redactor(key))
        mine.warning("falhou com %s", key)
        captured = capsys.readouterr()
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        mine.setLevel(saved[2])
        mine.disabled = saved[3]

    assert key not in captured.err and MASK in captured.err and captured.out == ""
