"""O MCP de admin contra a API de verdade: a sub-aplicação FastAPI do app, no mesmo processo do teste,
com o Postgres de teste, ligada ao cliente do MCP por um `httpx.ASGITransport` (sem rede).

É a prova de que o que o gerador monta e o que a API entende são a mesma coisa: o catálogo sai do
`openapi.json` que a API serve, o `confirm` vai onde a API o lê (na query de um `DELETE`, no corpo do
resto), o `multipart` do envio de imagem passa pela validação de verdade, o PDF e o HTML das prévias
voltam como arquivo, e o dado da lead de um mundo com pessoas fictícias não aparece sem a opção.

Só o teste importa o app (`app.admin_mcp` continua sem importá-lo). Não precisa do SDK do MCP.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from admin_api_support import audited
from admin_mcp_live import Live
from admin_mcp_support import openapi_document, optional
from admin_mcp_world import Asker
from jobs_world import PEOPLE, build_jobs_world
from pypdf import PdfReader
from test_admin_assets import picture
from test_admin_forms import CONTACT_PROMPT, DEFINITION, SLUG

from app.admin_mcp import catalog
from app.deps import Deps
from app.forms import stored

Make = Any  # a fixture `api_client`: api_client("leitura") -> o cliente de uma chave


@pytest.fixture
def world(deps: Deps) -> None:
    """O formulário demo-definicao, importado (versão 1), com os três legados que o banco já traz."""
    stored.import_definition(deps, DEFINITION, actor="testes")


@pytest.fixture
def live(api_client: Make, world: None, tmp_path: Path) -> Iterator[Live]:
    found = Live(api_client("total"), tmp_path)
    yield found
    # Em nenhuma saída de nenhuma chamada deste teste aparece a chave.
    assert all(found.key not in text for text in found.outputs)
    found.close()


# --- a subida contra a API de verdade ------------------------------------------------------------


def test_the_contract_the_live_api_serves_generates_the_same_tools_as_the_file(live: Live) -> None:
    expected = catalog.select(
        catalog.load(openapi_document()).tools.values(), role="total", lead_data=False
    )

    assert live.role == "total"
    assert live.tools == {tool.name: tool for tool in expected}
    assert len(live.tools) == 86
    retry = live.tools["retry_job_persisted_delivery"]
    assert retry.role == "total" and retry.external_effect
    assert retry.annotations["openWorldHint"] is True
    assert "confirm" in retry.input_schema["properties"]


def test_a_leitura_key_of_the_live_api_starts_with_the_read_tools(
    api_client: Make, world: None, tmp_path: Path
) -> None:
    reader = Live(api_client("leitura"), tmp_path)

    assert reader.role == "leitura" and len(reader.tools) == 41
    assert reader.ok("list_forms")["items"]
    reader.close()


def test_the_generated_schema_accepts_a_real_form_document(live: Live) -> None:
    jsonschema = optional("jsonschema")
    schema = live.tools["import_form"].input_schema
    document = json.loads(DEFINITION)

    jsonschema.Draft202012Validator(schema).validate({"definition": document, "confirm": True})

    document["steps"] = "nem uma lista"
    assert not jsonschema.Draft202012Validator(schema).is_valid({"definition": document})


# --- ler, confirmar e escrever -------------------------------------------------------------------


def test_reading_forms_and_one_form(live: Live) -> None:
    listed = live.ok("list_forms")
    form = live.ok("get_form", {"slug": SLUG})

    assert SLUG in {item["slug"] for item in listed["items"]}
    assert form["slug"] == SLUG and form["active"] is True and form["published_version"] == 1


def test_a_marked_operation_asks_and_the_yes_reaches_the_api_with_confirm(live: Live) -> None:
    asker = Asker(True)

    live.ok("deactivate_form", {"slug": SLUG}, asker)

    assert "deactivate_form" in asker.messages[0]
    assert live.ok("get_form", {"slug": SLUG})["active"] is False


def test_without_a_way_to_ask_the_operation_waits_for_the_next_call(live: Live) -> None:
    first = live.call("deactivate_form", {"slug": SLUG})
    still_active = live.ok("get_form", {"slug": SLUG})["active"]
    second = live.call("deactivate_form", {"slug": SLUG, "confirm": True})

    assert first.text.startswith("CONFIRMAÇÃO NECESSÁRIA") and still_active is True
    assert not second.is_error and live.ok("get_form", {"slug": SLUG})["active"] is False


def test_a_no_leaves_the_form_on_the_air(live: Live) -> None:
    output = live.call("deactivate_form", {"slug": SLUG}, Asker(False))

    assert output.is_error and live.ok("get_form", {"slug": SLUG})["active"] is True


def test_the_writes_are_audited_in_the_name_of_the_key(live: Live, deps: Deps) -> None:
    live.ok("deactivate_form", {"slug": SLUG}, Asker(True))

    (entry,) = audited(deps, "form.deactivate")
    assert entry.actor.startswith("chave:")
    assert live.key not in json.dumps(entry.details)


def test_the_dry_run_publication_runs_without_a_question_and_changes_nothing(
    live: Live, deps: Deps
) -> None:
    draft = live.ok("get_form_draft", {"slug": SLUG})
    asker = Asker()

    simulation = live.ok(
        "publish_form_draft",
        {"slug": SLUG, "expected_version": draft["version"], "dry_run": True},
        asker,
    )

    assert asker.messages == [] and isinstance(simulation, dict)
    assert audited(deps, "form.publish") == []


def test_an_api_validation_error_names_the_field_in_portuguese(live: Live) -> None:
    draft = live.ok("get_form_draft", {"slug": SLUG})
    document = draft["document"]
    document["steps"] = []

    output = live.call(
        "save_form_draft",
        {"slug": SLUG, "expected_version": draft["version"], "document": document},
    )

    assert output.is_error and output.text.startswith("A API recusou (422 validation_failed)")
    assert "- " in output.text


def test_a_stale_version_is_a_conflict_that_says_the_current_one(live: Live) -> None:
    draft = live.ok("get_form_draft", {"slug": SLUG})
    changed = {**draft["document"], "intro": "Uma introdução nova, escrita pelo MCP."}
    live.ok(
        "save_form_draft",
        {"slug": SLUG, "expected_version": draft["version"], "document": changed},
    )

    output = live.call(
        "save_form_draft",
        {"slug": SLUG, "expected_version": draft["version"], "document": changed},
    )

    assert output.is_error and "409 version_conflict" in output.text
    assert "A versão de agora é" in output.text


def test_a_missing_resource_is_a_not_found(live: Live) -> None:
    output = live.call("get_form", {"slug": "nao-existe"})

    assert output.is_error and "404 not_found" in output.text


def test_the_contact_confirmation_the_api_asks_for_is_put_to_the_person(
    live: Live, deps: Deps
) -> None:
    asker = Asker(True, True)

    imported = live.ok(
        "import_form", {"definition": json.loads(CONTACT_PROMPT) | {"slug": "com-contato"}}, asker
    )

    assert imported["slug"] == "com-contato"
    assert "o nome e o e-mail" in asker.messages[1]
    (entry,) = [e for e in audited(deps, "form.import") if e.actor.startswith("chave:")]
    assert entry.details["contact_in_prompt"] == ["name", "email"]


# --- imagens: multipart e DELETE com confirm na query --------------------------------------------


def test_an_image_goes_up_by_path_comes_back_and_leaves_with_the_confirm_in_the_query(
    live: Live,
) -> None:
    source = live.uploads / "foto-da-cliente.png"
    source.write_bytes(picture(size=(900, 300)))
    asker = Asker(True)

    uploaded = live.ok(
        "upload_asset", {"name": "logo-mcp", "scope": SLUG, "file": str(source)}, asker
    )
    seen = live.ok("get_asset", {"asset_id": uploaded["id"]})
    removed = live.ok(
        "remove_asset",
        {"asset_id": uploaded["id"], "expected_version": seen["version"]},
        Asker(True),
    )
    gone = live.call("get_asset", {"asset_id": uploaded["id"]})

    assert uploaded["reference"] == "asset:logo-mcp" and uploaded["form_slug"] == SLUG
    assert "foto-da-cliente" not in json.dumps(uploaded)
    assert removed and gone.is_error and "404 not_found" in gone.text
    # A pessoa viu o arquivo (o caminho resolvido e o tamanho) antes do envio.
    assert (
        f"Arquivo (file): {source.resolve()} ({source.stat().st_size} bytes)" in asker.messages[0]
    )


def test_a_file_that_is_not_an_image_never_reaches_the_api(live: Live) -> None:
    note = live.uploads / "anotacao.txt"
    note.write_text("não sou uma imagem", encoding="utf-8")

    output = live.call("upload_asset", {"name": "logo-mcp", "file": str(note)}, Asker(True))

    assert output.is_error and "não é uma imagem" in output.text
    assert live.ok("list_assets")["items"] == []


def test_an_image_outside_the_upload_folder_never_reaches_the_real_api(
    live: Live, tmp_path: Path
) -> None:
    """A imagem enviada é servida sem login (`/img/<sha256>`): o modelo não escolhe qualquer imagem do
    disco da operadora."""
    private = tmp_path / "particular" / "foto-da-familia.png"
    private.parent.mkdir()
    private.write_bytes(picture(size=(300, 100)))
    (live.uploads / "atalho.png").symlink_to(private)
    asker = Asker(True)

    outputs = [
        live.call("upload_asset", {"name": "logo-mcp", "file": given}, asker)
        for given in (str(private), "../particular/foto-da-familia.png", "atalho.png")
    ]

    assert all(o.is_error and "fora da pasta de envio" in o.text for o in outputs)
    assert live.sent() == [] and asker.messages == []
    assert live.ok("list_assets")["items"] == []


# --- o argumento de caminho: o app de verdade decodifica o `/` escapado --------------------------


def test_the_real_app_routes_an_escaped_slash_to_another_operation(
    api_client: Make, deps: Deps
) -> None:
    """Por que o MCP recusa o `/` em vez de escapá-lo: o servidor decodifica o `%2F` antes de rotear
    (o uvicorn faz o mesmo que o cliente de teste), e `get_job` com `<código>%2Fpdf` chega a
    `download_job_pdf`, o PDF de uma lead. Este teste mostra o caminho que o MCP fecha."""
    jobs = build_jobs_world(deps)

    smuggled = api_client("total").get(f"/admin/api/v1/jobs/{jobs.ana.public_id}%2Fpdf")

    assert smuggled.status_code == 200
    assert smuggled.headers["content-type"] == "application/pdf"
    assert smuggled.content.startswith(b"%PDF")


@pytest.mark.parametrize("suffix", ["/pdf", "%2Fpdf", "%2fpdf", "/../x", "?include_lead_data=true"])
def test_get_job_cannot_be_turned_into_the_lead_pdf_with_the_lead_data_off(
    api_client: Make, deps: Deps, tmp_path: Path, suffix: str
) -> None:
    jobs = build_jobs_world(deps)
    off = Live(api_client("total"), tmp_path)

    output = off.call("get_job", {"public_id": f"{jobs.ana.public_id}{suffix}"})

    assert "download_job_pdf" not in off.tools
    assert output.is_error and "`public_id` só pode ter" in output.text
    assert off.sent() == []
    assert not (tmp_path / "downloads").exists()  # nenhum PDF de lead foi salvo
    off.close()


def test_get_form_cannot_be_turned_into_get_form_draft(live: Live) -> None:
    for smuggled in (f"{SLUG}/draft", f"{SLUG}%2Fdraft", f"{SLUG}/draft/review"):
        output = live.call("get_form", {"slug": smuggled})
        assert output.is_error and "`slug` só pode ter" in output.text, smuggled

    assert live.sent() == []
    assert live.ok("get_form", {"slug": SLUG})["slug"] == SLUG  # o legítimo segue funcionando
    assert len(live.sent()) == 1


def test_no_tool_with_a_path_argument_sends_anything_but_one_safe_segment(
    api_client: Make, world: None, tmp_path: Path
) -> None:
    everything = Live(api_client("total"), tmp_path, lead_data=True)
    checked = 0

    for name, tool in everything.tools.items():
        in_the_path = [arg for arg, where in tool.locations.items() if where == "path"]
        if not in_the_path:
            continue
        for evil in ("x/draft", "x%2Fdraft", "..", "a b"):
            placeholders = {arg: "valido" for arg in tool.input_schema["required"]}
            output = everything.call(name, {**placeholders, in_the_path[0]: evil})
            assert output.is_error and "só pode ter" in output.text, (name, evil)
        checked += 1

    assert checked >= 30 and "download_job_pdf" in everything.tools
    assert everything.sent() == []
    everything.close()


# --- as prévias: arquivo, e não bytes ------------------------------------------------------------


def test_the_pdf_preview_comes_back_as_a_valid_pdf_file(live: Live, tmp_path: Path) -> None:
    output = live.call("render_form_draft_pdf_preview", {"slug": SLUG})

    assert not output.is_error
    saved = Path(output.text.split("caminho: ", 1)[1].splitlines()[0])
    assert saved.parent == tmp_path / "downloads" and saved.suffix == ".pdf"
    assert len(PdfReader(saved).pages) >= 1
    assert "%PDF" not in output.text


def test_the_public_page_preview_comes_back_as_an_html_file(live: Live) -> None:
    output = live.call("render_form_draft_public_page", {"slug": SLUG})

    saved = Path(output.text.split("caminho: ", 1)[1].splitlines()[0])
    assert saved.suffix == ".html" and "<html" in saved.read_text(encoding="utf-8").lower()
    assert "<html" not in output.text.lower()


# --- o dado da lead ------------------------------------------------------------------------------


def test_without_the_option_no_name_or_email_of_a_lead_reaches_the_model(
    api_client: Make, deps: Deps, tmp_path: Path
) -> None:
    jobs = build_jobs_world(deps)
    off = Live(api_client("total"), tmp_path)

    everything = [
        off.call("list_jobs", {"include_total": True}).text,
        off.call("list_jobs", {"status": "failed"}).text,
        off.call("get_job", {"public_id": jobs.ana.public_id}).text,
        off.call("get_job", {"public_id": jobs.beatriz.public_id}).text,
    ]
    refused = [
        off.call("list_jobs", {"q": "ana@example.test"}),
        off.call("list_jobs", {"include_lead_data": True}),
        off.call("get_job", {"public_id": jobs.ana.public_id, "include_lead_data": True}),
    ]

    text = "\n".join(everything)
    for name, email in PEOPLE.values():
        assert name not in text and email not in text
    assert jobs.ana.public_id in text
    assert all(output.is_error and "não tem" in output.text for output in refused)
    assert "download_job_pdf" not in off.tools
    off.close()


def test_with_the_option_the_lead_data_and_the_pdf_are_there_on_request(
    api_client: Make, deps: Deps, tmp_path: Path
) -> None:
    jobs = build_jobs_world(deps)
    on = Live(api_client("total"), tmp_path, lead_data=True)

    listed = on.call("list_jobs", {"q": "Ana Souza", "include_lead_data": True})
    pdf = on.call("download_job_pdf", {"public_id": jobs.ana.public_id})

    assert PEOPLE["ana"][1] in listed.text and PEOPLE["beatriz"][1] not in listed.text
    saved = Path(pdf.text.split("caminho: ", 1)[1].splitlines()[0])
    assert saved.suffix == ".pdf" and saved.read_bytes().startswith(b"%PDF")
    assert jobs.ana.public_id not in saved.name
    on.close()


def test_draft_test_replay_recovers_before_native_confirmation(api_client, world, tmp_path, deps):
    from app.admin_mcp.runner import ToolRunner

    sent = []

    async def trigger(test_id):
        sent.append(test_id)
        return "synthetic-run"

    live = Live(api_client("total", test_trigger=trigger), tmp_path)
    try:
        context = live.ok("get_form_draft_test_command_context", {"slug": SLUG})
        body = {
            "slug": SLUG,
            "command_id": context["command_id"],
            "expected_version": context["expected_version"],
        }
        ask = Asker(True)
        import httpx

        send_request = live.wire.handle_async_request

        async def lose_committed_response(request):
            response = await send_request(request)
            if (
                request.method == "POST"
                and request.url.path.endswith("/draft/tests")
                and not json.loads(request.content).get("replay_only")
            ):
                raise httpx.ReadTimeout("resposta sintética perdida após commit")
            return response

        live.wire.handle_async_request = lose_committed_response
        lost = live.call("start_form_draft_test", body, ask)
        assert lost.is_error
        assert len(ask.messages) == 1
        live.runner = ToolRunner(live.client, live.config)
        again_ask = Asker(False)
        recovered = live.ok("start_form_draft_test", body, again_ask)
        assert not again_ask.messages
        assert recovered["receipt"]["test_id"] == str(sent[0])
        assert len(sent) == 1
        conflict = live.call("start_form_draft_test", {**body, "answers": {}}, again_ask)
        assert conflict.is_error and conflict.data["code"] == "command_conflict"
        assert not again_ask.messages
    finally:
        live.close()


def test_committed_replay_is_readable_while_the_original_dispatch_waits(
    api_client, world, tmp_path
):
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()
    sent = []

    async def trigger(test_id):
        sent.append(test_id)
        entered.set()
        await release.wait()
        return "synthetic-run"

    live = Live(api_client("total", test_trigger=trigger), tmp_path)
    try:
        context = live.ok("get_form_draft_test_command_context", {"slug": SLUG})
        arguments = {"slug": SLUG, **{k: context[k] for k in ("command_id", "expected_version")}}
        refused = Asker(False)

        async def concurrent_replay():
            tool = live.tools["start_form_draft_test"]
            first = asyncio.create_task(live.runner.call(tool, arguments, Asker(True)))
            try:
                await asyncio.wait_for(entered.wait(), timeout=10)
                return await live.runner.call(tool, arguments, refused)
            finally:
                release.set()
                created = await first
                assert created.status == 200

        recovered = asyncio.run(concurrent_replay())
        assert recovered.status == 200
        assert recovered.data["receipt"]["test_id"] == str(sent[0])
        assert not refused.messages and len(sent) == 1
    finally:
        live.close()


@pytest.mark.parametrize("answer", [False, None])
def test_identified_draft_test_negative_or_pending_never_creates(
    api_client, world, tmp_path, answer
):
    sent = []

    async def trigger(test_id):
        sent.append(test_id)
        return "synthetic-run"

    live = Live(api_client("total", test_trigger=trigger), tmp_path)
    try:
        context = live.ok("get_form_draft_test_command_context", {"slug": SLUG})
        body = {
            "slug": SLUG,
            "command_id": context["command_id"],
            "expected_version": context["expected_version"],
            "confirm": True,
        }
        refused = live.call("start_form_draft_test", body, Asker(answer))
        assert refused.is_error or refused.awaiting
        assert not sent
        checked = live.call("start_form_draft_test", {**body, "replay_only": True}, Asker(True))
        assert checked.status == 404 and checked.data["code"] == "command_not_found"
        assert not sent
    finally:
        live.close()
