"""A subida do MCP de admin (`app.admin_mcp.discovery`) e a configuração (`app.admin_mcp.config`).

Na subida o servidor lê o `openapi.json` da API viva e chama `get_current_api_key` para saber o papel da
chave: só as ferramentas que o papel permite são registradas. Aqui a API é a de mentira (`FakeApi`), e
cada falha da subida tem de dizer o que conferir sem repetir a chave. Sem rede e sem o SDK do MCP.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest
from admin_mcp_support import (
    ORIGIN,
    FakeApi,
    answer,
    error,
    make_config,
    new_key,
    openapi_document,
    operation_ids,
)

from app.admin_mcp.client import ApiClient
from app.admin_mcp.config import API_PREFIX, Config, ConfigError
from app.admin_mcp.discovery import Discovery, StartupError, discover


def start(api: FakeApi, *, lead_data: bool = False, key: str | None = None) -> Discovery:
    config = make_config(key or api.key, lead_data=lead_data)
    client = ApiClient(config, transport=api.transport)
    try:
        return asyncio.run(discover(client, config))
    finally:
        asyncio.run(client.aclose())


def failure(api: FakeApi, **options: Any) -> str:
    with pytest.raises(StartupError) as raised:
        start(api, **options)
    return str(raised.value)


# --- a subida ------------------------------------------------------------------------------------


def test_a_total_key_starts_with_every_tool_but_the_lead_pdf() -> None:
    api = FakeApi(new_key(), "total")

    found = start(api)

    assert found.role == "total" and found.skipped == {}
    assert {t.name for t in found.tools} == operation_ids(api.document) - {
        "download_job_pdf",
        "get_job_plan_projection",
        "render_job_participant_preview",
    }


def test_a_leitura_key_starts_with_only_the_leitura_tools() -> None:
    found = start(FakeApi(new_key(), "leitura"))

    assert found.role == "leitura" and len(found.tools) == 41
    assert {t.role for t in found.tools} == {"leitura"}


def test_the_lead_data_option_adds_the_lead_pdf_for_a_total_key_only() -> None:
    assert len(start(FakeApi(new_key(), "total"), lead_data=True).tools) == 89
    assert len(start(FakeApi(new_key(), "leitura"), lead_data=True).tools) == 41


def test_the_startup_reads_the_live_contract_and_then_asks_who_the_key_is() -> None:
    api = FakeApi(new_key())

    start(api)

    assert [(r.method, r.url.path) for r in api.requests] == [
        ("GET", f"{API_PREFIX}/openapi.json"),
        ("GET", f"{API_PREFIX}/me"),
    ]
    assert all(r.headers["authorization"] == f"Bearer {api.key}" for r in api.requests)


def test_the_tools_come_from_the_document_the_api_serves_not_from_a_copy() -> None:
    document = openapi_document()
    document["paths"]["/novidade"] = {
        "get": {
            "operationId": "list_novelties",
            "summary": "Listar as novidades",
            "x-required-role": "leitura",
            "responses": {"200": {"description": "ok"}},
        }
    }

    found = start(FakeApi(new_key(), "leitura", document=document))

    assert "list_novelties" in {t.name for t in found.tools}


def test_an_operation_the_generator_cannot_build_is_warned_and_the_rest_still_start(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # O `fileConfig` das migrations (outro teste do mesmo processo) desliga os loggers que já existem.
    monkeypatch.setattr(logging.getLogger("gerador_mcp"), "disabled", False)
    document = openapi_document()
    document["paths"]["/estranha"] = {
        "get": {
            "operationId": "strange",
            "summary": "Estranha",
            "x-required-role": "leitura",
            "parameters": [{"name": "X", "in": "header", "schema": {"type": "string"}}],
            "responses": {"200": {"description": "ok"}},
        }
    }

    with caplog.at_level(logging.WARNING, logger="gerador_mcp"):
        found = start(FakeApi(new_key(), "leitura", document=document))

    assert set(found.skipped) == {"strange"} and len(found.tools) == 41
    assert "Sem ferramenta para 'strange'" in caplog.text


def test_a_hostile_document_registers_nothing_outside_the_formats_and_logs_escaped_lines(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging.getLogger("gerador_mcp"), "disabled", False)
    evil = {
        "summary": "Fingir",
        "x-required-role": "leitura",
        "responses": {"200": {"description": "ok"}},
    }
    document = openapi_document()
    document["paths"]["/evil"] = {"get": {**evil, "operationId": "../../escaped"}}
    document["paths"]["/linha"] = {"get": {**evil, "operationId": "x\nWARNING falso: tudo certo"}}
    document["paths"]["/../x"] = {"get": {**evil, "operationId": "dotdot"}}
    document["paths"]["/q?a=1"] = {"get": {**evil, "operationId": "with_query"}}

    with caplog.at_level(logging.WARNING, logger="gerador_mcp"):
        found = start(FakeApi(new_key(), "leitura", document=document))

    names = {tool.name for tool in found.tools}
    assert len(found.tools) == 41
    assert names.isdisjoint({"../../escaped", "dotdot", "with_query"})
    assert not any("escaped" in name or "falso" in name for name in names)
    assert len(found.skipped) == 4
    # Uma linha de log por operação recusada, e nenhuma quebra de linha do documento solta no log.
    lines = [m for m in caplog.messages if m.startswith("Sem ferramenta para")]
    assert len(lines) == 4 and all("\n" not in line for line in lines)


# --- as falhas da subida -------------------------------------------------------------------------


def test_a_key_the_api_refuses_says_to_check_the_key_and_never_repeats_it() -> None:
    api = FakeApi(new_key())

    text = failure(api, key=new_key())  # a API conhece outra chave

    assert text.startswith("Não consegui começar. A API recusou (401 invalid_credentials)")
    assert "GERADOR_API_KEY" in text


def test_a_revoked_key_is_reported_by_the_api_message() -> None:
    api = FakeApi(new_key())
    api.when("GET", "/openapi.json", error(401, "key_revoked", "A chave foi revogada."))

    assert "401 key_revoked" in failure(api)


def test_an_address_without_the_api_says_to_check_the_url() -> None:
    api = FakeApi(new_key())
    api.when("GET", "/openapi.json", error(404, "not_found", "Não há operação neste endereço."))

    assert "GERADOR_API_URL" in failure(api)


def test_a_reply_that_is_not_json_is_not_the_api() -> None:
    api = FakeApi(new_key())
    api.when("GET", "/openapi.json", httpx.Response(200, text="<html>login</html>"))

    assert "não é JSON" in failure(api) and "GERADOR_API_URL" in failure(api)


def test_a_json_that_is_not_a_document_is_refused() -> None:
    api = FakeApi(new_key())
    api.when("GET", "/openapi.json", answer(200, ["não", "é", "um", "documento"]))

    assert "não é um documento OpenAPI" in failure(api)


def test_a_document_without_paths_is_refused() -> None:
    api = FakeApi(new_key(), document={"openapi": "3.1.0", "paths": {}})

    assert "não é o que o MCP espera" in failure(api)


@pytest.mark.parametrize("components", ["texto", ["lista"], {"schemas": "texto"}, {"schemas": [1]}])
def test_a_document_whose_components_are_not_objects_is_a_clean_start_error(
    components: Any,
) -> None:
    document = openapi_document()
    document["components"] = components

    text = failure(FakeApi(new_key(), document=document))

    assert "não é o que o MCP espera" in text and "components" in text


def test_a_document_without_the_identity_operation_is_refused() -> None:
    document = openapi_document()
    del document["paths"]["/me"]

    assert "get_current_api_key" in failure(FakeApi(new_key(), document=document))


def test_an_identity_answer_without_a_role_is_refused() -> None:
    api = FakeApi(new_key())
    api.when("GET", "/me", answer(200, {"name": "x"}))

    assert "papel da chave" in failure(api)


def test_a_failure_on_the_identity_call_is_reported() -> None:
    api = FakeApi(new_key())
    api.when("GET", "/me", error(429, "rate_limited", "Muitas chamadas."))

    assert "429 rate_limited" in failure(api)


def test_a_network_failure_at_the_start_does_not_leak_the_library_message() -> None:
    key = new_key()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"sem rota para {request.url} {key}")

    config = make_config(key)
    client = ApiClient(config, transport=httpx.MockTransport(boom))

    with pytest.raises(StartupError) as raised:
        asyncio.run(discover(client, config))
    asyncio.run(client.aclose())

    assert "ConnectError" in str(raised.value) and key not in str(raised.value)
    assert "sem rota" not in str(raised.value)


# --- a configuração ------------------------------------------------------------------------------


def env(**values: str) -> dict[str, str]:
    return {"GERADOR_API_URL": ORIGIN, "GERADOR_API_KEY": new_key(), **values}


def test_the_address_gets_the_api_prefix_once() -> None:
    for given in (ORIGIN, ORIGIN + "/", ORIGIN + API_PREFIX, ORIGIN + API_PREFIX + "/"):
        assert Config.from_env(env(GERADOR_API_URL=given)).base_url == ORIGIN + API_PREFIX
    assert (
        Config.from_env(env(GERADOR_API_URL=ORIGIN + "/painel")).base_url
        == ORIGIN + "/painel" + API_PREFIX
    )


def test_plain_http_is_for_the_local_machine_only() -> None:
    for local in ("http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"):
        assert Config.from_env(env(GERADOR_API_URL=local)).base_url.startswith(local)

    for remote in (
        "http://planos.exemplo.test",
        "http://10.0.0.5",
        "http://127.0.0.1.exemplo.test",
    ):
        with pytest.raises(ConfigError, match="https"):
            Config.from_env(env(GERADOR_API_URL=remote))


@pytest.mark.parametrize(
    "bad",
    [
        "planos.exemplo.test",  # sem esquema
        "ftp://planos.exemplo.test",
        "https:///sem-host",
        "https://usuario:senha@planos.exemplo.test",
        "https://planos.exemplo.test?x=1",
        "https://planos.exemplo.test#a",
    ],
)
def test_an_address_that_is_not_a_plain_url_is_refused_without_echoing_it(bad: str) -> None:
    with pytest.raises(ConfigError) as raised:
        Config.from_env(env(GERADOR_API_URL=bad))

    assert "GERADOR_API_URL" in str(raised.value) and bad not in str(raised.value)


def test_the_key_is_required_and_must_look_like_a_key() -> None:
    with pytest.raises(ConfigError, match="GERADOR_API_KEY"):
        Config.from_env({"GERADOR_API_URL": ORIGIN})
    for bad in ("tem espaço", "com\nquebra", "acentuação-ç"):
        with pytest.raises(ConfigError) as raised:
            Config.from_env(env(GERADOR_API_KEY=bad))
        assert bad not in str(raised.value)


def test_the_key_is_trimmed() -> None:
    key = new_key()

    assert Config.from_env(env(GERADOR_API_KEY=f"  {key}\n")).api_key == key


@pytest.mark.parametrize(
    ("raw", "expected"), [("", False), ("0", False), ("1", True), (" 1 ", True)]
)
def test_the_lead_data_option_is_one_or_zero(raw: str, expected: bool) -> None:
    assert Config.from_env(env(GERADOR_MCP_LEAD_DATA=raw)).lead_data is expected
    assert Config.from_env(env()).lead_data is False


@pytest.mark.parametrize("bad", ["true", "sim", "2", "yes", "on"])
def test_the_lead_data_option_does_not_guess(bad: str) -> None:
    with pytest.raises(ConfigError, match="GERADOR_MCP_LEAD_DATA"):
        Config.from_env(env(GERADOR_MCP_LEAD_DATA=bad))


def test_the_download_folder_is_optional(tmp_path: Any) -> None:
    assert Config.from_env(env()).download_dir is None
    chosen = Config.from_env(env(GERADOR_MCP_DOWNLOAD_DIR=str(tmp_path / "pdfs")))
    assert chosen.download_dir == tmp_path / "pdfs"


def test_the_upload_folder_has_no_default_because_the_model_picks_the_path(tmp_path: Any) -> None:
    assert Config.from_env(env()).upload_dir is None
    assert Config.from_env(env(GERADOR_MCP_UPLOAD_DIR="  ")).upload_dir is None
    chosen = Config.from_env(env(GERADOR_MCP_UPLOAD_DIR=str(tmp_path / "imagens")))
    assert chosen.upload_dir == tmp_path / "imagens"


@pytest.mark.parametrize(
    "bad",
    [
        "https://planos.exemplo.test:abc",
        "https://planos.exemplo.test:99999",
        "https://planos.exemplo.test:-1",
        "https://planos.exemplo.test:0",
        "https://planos.exemplo.test:80:90",
        "https://[::1",
        "https://[::1]:xx",
    ],
)
def test_a_bad_port_or_host_is_a_clean_config_error_and_not_a_traceback(bad: str) -> None:
    with pytest.raises(ConfigError) as raised:
        Config.from_env(env(GERADOR_API_URL=bad))

    assert "GERADOR_API_URL" in str(raised.value) and bad not in str(raised.value)


def test_the_disk_root_and_the_home_folder_are_not_upload_folders(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "casa"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (tmp_path / "atalho-da-casa").symlink_to(home, target_is_directory=True)
    refused = [
        "/",
        str(home),
        "~",
        str(home) + "/",
        str(home / "sub" / ".."),
        str(tmp_path / "atalho-da-casa"),
    ]

    for bad in refused:
        with pytest.raises(ConfigError, match="raiz do disco nem a sua pasta pessoal"):
            Config.from_env(env(GERADOR_MCP_UPLOAD_DIR=bad))

    # Uma pasta dentro da casa vale, e nem precisa existir ainda: quem confere é o uso.
    assert Config.from_env(env(GERADOR_MCP_UPLOAD_DIR=str(home / "imagens"))).upload_dir
    assert Config.from_env(env(GERADOR_MCP_UPLOAD_DIR="~/imagens")).upload_dir == home / "imagens"


def test_a_valid_port_stays_in_the_address() -> None:
    config = Config.from_env(env(GERADOR_API_URL="https://planos.exemplo.test:8443"))

    assert config.base_url == "https://planos.exemplo.test:8443" + API_PREFIX
