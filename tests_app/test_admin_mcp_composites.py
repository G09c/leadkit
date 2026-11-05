"""As ferramentas compostas do MCP de admin (`app.admin_mcp.compose`, `composites*`; issue #114), contra a
API em memória: o app de verdade no mesmo processo do teste (Postgres de teste, `httpx.ASGITransport`,
sem rede) e, para as falhas que o app não faz sozinho (um `422` na publicação, o Baserow que some no
meio), a API de mentira (`FakeApi`, um `httpx.MockTransport`).

O que a ADR 0015 pede delas e este arquivo prova:

- **só chamam ferramentas geradas**, pelo mesmo caminho do `ToolRunner`: um teste lista cada requisição
  que as cinco fazem e confere que é uma operação do catálogo que a composta declarou;
- **a confirmação das ações marcadas vale dentro delas**: nenhuma manda `confirm` por conta própria, a
  publicação e a ativação só acontecem depois do sim da pessoa a **aquela** ação (com a pergunta ou com a
  chamada seguinte), e o `confirm` do modelo não vale antes de o servidor ter mostrado a chamada;
- **o dado da lead segue `GERADOR_MCP_LEAD_DATA`**: sem ele, nenhuma pede `include_lead_data`, usa `q` ou
  baixa o PDF, e o texto de `diagnose_order` nunca traz nome, e-mail nem resposta;
- **só existem para a chave que pode usá-las** (uma `leitura` recebe as duas que só leem), e as
  ferramentas geradas seguem registradas ao lado;
- **falham dizendo a verdade**: o `409` e o `422` da API chegam com `errors`, `reasons` e a versão de
  agora, e uma composta que parou no meio diz o que já fez.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
import logging
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from admin_api_support import audited
from admin_mcp_live import Live
from admin_mcp_support import (
    ROOT,
    answer,
    error,
    openapi_document,
    operation_ids,
    optional,
)
from admin_mcp_world import Asker, World
from conftest import BASEROW_TABLE, BASEROW_TOKEN, BASEROW_URL, FakeBaserow, baserow_field
from cryptography.fernet import Fernet
from jobs_world import FAILURE_DETAIL, PEOPLE, build_jobs_world
from test_admin import deliver_to_the_webhook, new_lead_job
from test_admin_forms import CONTACT_PROMPT, DEFINITION, SLUG

from app.admin_mcp import catalog
from app.admin_mcp.client import ApiClient
from app.admin_mcp.compose import Steps, untouched, without_clashes
from app.admin_mcp.composites import BUILDERS, for_tools
from app.admin_mcp.config import API_PREFIX
from app.admin_mcp.confirmation import ConfirmationOutcome
from app.admin_mcp.results import ToolOutput
from app.admin_mcp.runner import NeverAsks, ToolRunner
from app.deps import Deps
from app.destinations import SecretCipher
from app.forms import drafts, stored

Make = Any  # a fixture `api_client`: api_client("leitura") -> o cliente de uma chave

DOCUMENT = openapi_document()
ROUTES = [
    (
        method.upper(),
        re.compile("^" + re.sub(r"\{[a-z_]+\}", "[^/]+", path) + "$"),
        operation["operationId"],
    )
    for path, item in DOCUMENT["paths"].items()
    for method, operation in item.items()
    if method in catalog.HTTP_METHODS
]

# O que o ticket (#114) lista: a composta e as operações que ela chama.
CALLS = {
    "attention_overview": {"list_jobs", "list_destinations", "list_forms", "get_form_integration"},
    "diagnose_order": {"get_job"},
    "edit_form_draft": {"get_form_draft", "patch_form_draft", "compare_form_versions"},
    "publish_form_with_review": {"review_form_draft_publication", "publish_form_draft"},
    "connect_baserow_integration": {
        "get_form_integration",
        "connect_form_integration",
        "save_form_integration_mapping",
        "activate_form_integration",
    },
}
READS = {"attention_overview", "diagnose_order"}
NO_LEAD_DATA = {"include_lead_data": "false"}  # o que o executor acrescenta a toda `list_jobs`
NOTE = "Introdução nova, escrita pelo MCP"
INTRO = [{"op": "replace", "path": "/intro", "value": "Uma introdução nova para o diagnóstico."}]
CONNECT = {
    "slug": SLUG,
    "base_url": BASEROW_URL,
    "table_id": BASEROW_TABLE,
    "token": BASEROW_TOKEN,
    "purpose": "internal",
}


def folder(tmp_path: Path, name: str) -> Path:
    """Uma pasta nova dentro de `tmp_path` (o `World` e o `Live` põem a deles dentro da que recebem)."""
    found = tmp_path / name
    found.mkdir()
    return found


def operation_of(request: httpx.Request) -> str:
    """A operação do catálogo que uma requisição à API é (pelo método e pelo caminho do contrato)."""
    path = request.url.raw_path.decode().split("?")[0].removeprefix(API_PREFIX)
    found = [
        name for method, pattern, name in ROUTES if method == request.method and pattern.match(path)
    ]
    assert len(found) == 1, (request.method, path)
    return found[0]


def confirmed(request: httpx.Request) -> bool:
    """A requisição leva `confirm`, `confirm_contact` ou `confirm_http` verdadeiro (no corpo ou na query)."""
    if request.content:
        sent = json.loads(request.content)
        if isinstance(sent, dict) and any(
            sent.get(n) is True for n in sent if n.startswith("confirm")
        ):
            return True
    return any(
        value == "true" for name, value in request.url.params.items() if name.startswith("confirm")
    )


# =================================================================================================
# o registro: quem recebe quais compostas
# =================================================================================================


def test_a_total_key_gets_the_five_composites_beside_the_generated_tools(tmp_path: Path) -> None:
    world = World(tmp_path)

    assert set(world.composites) == set(CALLS)
    assert len(world.tools) == 86 and set(CALLS).isdisjoint(world.tools)
    assert "render_job_participant_preview" not in world.tools
    assert len(world.tools) + len(world.composites) == 91
    world.close()


def test_a_leitura_key_gets_only_the_two_composites_that_only_read(tmp_path: Path) -> None:
    reader = World(tmp_path, "leitura")

    assert set(reader.composites) == READS
    assert len(reader.tools) == 41 and len(reader.tools) + len(reader.composites) == 43
    assert all(reader.tools[call].read_only for c in reader.composites.values() for call in c.calls)
    reader.close()


def test_the_lead_data_option_changes_no_composite_but_adds_the_lead_pdf_beside_them(
    tmp_path: Path,
) -> None:
    on = World(tmp_path, lead_data=True)
    reader = World(folder(tmp_path, "leitura"), "leitura", lead_data=True)

    assert set(on.composites) == set(CALLS) and len(on.tools) + len(on.composites) == 94
    assert set(reader.composites) == READS  # a opção não faz nada para a chave `leitura`
    personal = {"download_job_pdf", "get_job_plan_projection", "render_job_participant_preview"}
    assert personal <= set(on.tools)
    assert personal.isdisjoint(reader.tools)
    assert all(personal.isdisjoint(c.calls) for c in on.composites.values())
    on.close()
    reader.close()


def test_each_composite_declares_the_calls_of_the_ticket_and_they_are_catalog_operations() -> None:
    declared = {c.name: set(c.calls) for c in for_tools(catalog.load(DOCUMENT).tools)}

    assert declared == CALLS
    assert set().union(*CALLS.values()) <= operation_ids(DOCUMENT)
    assert [builder.__name__ for builder in BUILDERS] == list(CALLS)


@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_composite_is_registered_only_if_every_operation_it_calls_is(
    name: str, tmp_path: Path
) -> None:
    world = World(tmp_path)

    for call in CALLS[name]:
        without = {k: v for k, v in world.tools.items() if k != call}
        assert name not in {c.name for c in for_tools(without)}, (name, call)
    world.close()


def test_the_descriptions_say_the_effect_and_the_marked_actions_each_one_can_trigger(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)

    for composite in world.composites.values():
        text = composite.description
        assert text.startswith(composite.summary)
        for call in composite.calls:
            assert f"`{call}`" in text, (composite.name, call)
        marked = [world.tools[c] for c in composite.calls if world.tools[c].marked]
        if marked:
            assert "Ações marcadas que ela pode disparar" in text
            assert "nunca confirma por você" in text
            for spec in marked:
                assert f"`{spec.name}` ({spec.summary}:" in text
        else:
            assert "Só lê" in text or "Não dispara nenhuma ação marcada" in text
    publish = world.composites["publish_form_with_review"].description
    assert "muda o que a lead" in publish and "`publish: true`" in publish
    connect = world.composites["connect_baserow_integration"].description
    assert "sobrescreve a conexão" in connect and "`activate: true`" in connect
    assert "só sugere" in world.composites["diagnose_order"].description
    world.close()


def test_the_hints_come_from_the_marks_of_the_operations_the_composite_calls(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)

    assert {n: dict(c.annotations) for n, c in world.composites.items()} == {
        "attention_overview": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
        "diagnose_order": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        "edit_form_draft": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "openWorldHint": False,
        },
        "publish_form_with_review": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "openWorldHint": True,
        },
        "connect_baserow_integration": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "openWorldHint": True,
        },
    }
    world.close()


def test_the_input_schemas_are_closed_valid_and_take_the_text_of_the_contract(
    tmp_path: Path,
) -> None:
    jsonschema = optional("jsonschema")
    world = World(tmp_path)

    for composite in world.composites.values():
        schema = composite.input_schema
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False, composite.name
        assert set(schema["required"]) <= set(schema["properties"]), composite.name
    edit = world.composites["edit_form_draft"].input_schema
    # O texto do argumento é o do contrato: um só lugar para mantê-lo.
    assert (
        edit["properties"]["patch"]
        == world.tools["patch_form_draft"].input_schema["properties"]["patch"]
    )
    assert set(edit["$defs"]) == {"PatchOperation"}
    validator = jsonschema.Draft202012Validator(edit)
    validator.validate({"slug": SLUG, "patch": INTRO})
    assert not validator.is_valid({"slug": SLUG})  # falta o patch
    assert not validator.is_valid(
        {"slug": SLUG, "patch": INTRO, "confirm": True}
    )  # não tem confirm
    publish = jsonschema.Draft202012Validator(
        world.composites["publish_form_with_review"].input_schema
    )
    publish.validate({"slug": SLUG, "note": NOTE, "publish": True, "confirm": True})
    assert not publish.is_valid({"slug": SLUG, "publish": "sim"})
    world.close()


def test_the_lead_data_argument_exists_only_where_the_generated_tool_has_it(tmp_path: Path) -> None:
    off = World(tmp_path)
    on = World(folder(tmp_path, "on"), lead_data=True)
    reader = World(folder(tmp_path, "leitura"), "leitura", lead_data=True)

    assert "include_lead_data" not in off.composites["diagnose_order"].input_schema["properties"]
    assert "include_lead_data" in on.composites["diagnose_order"].input_schema["properties"]
    assert "include_lead_data" not in reader.composites["diagnose_order"].input_schema["properties"]
    for world in (off, on, reader):
        for composite in world.composites.values():
            assert "q" not in composite.input_schema["properties"], composite.name
            assert "download_job_pdf" not in composite.calls
    off.close()
    on.close()
    reader.close()


# --- o que o código das compostas pode fazer ----------------------------------------------------------

MODULES = sorted((ROOT / "app" / "admin_mcp").glob("composite*.py")) + [
    ROOT / "app" / "admin_mcp" / "compose.py"
]
ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "copy",
    "dataclasses",
    "logging",
    "typing",
    "urllib.parse",  # só para ler o servidor de um endereço: quem fala HTTP é o `ToolRunner`
    "app.admin_mcp.catalog",
    "app.admin_mcp.client",
    "app.admin_mcp.compose",
    "app.admin_mcp.confirmation",  # desfechos neutros, sem SDK ou I/O
    "app.admin_mcp.composites_baserow",
    "app.admin_mcp.composites_forms",
    "app.admin_mcp.composites_orders",
    "app.admin_mcp.results",
    "app.admin_mcp.runner",
}


def test_the_composites_import_nothing_that_talks_http_or_belongs_to_the_app() -> None:
    assert {p.name for p in MODULES} >= {
        "compose.py",
        "composites.py",
        "composites_baserow.py",
        "composites_forms.py",
        "composites_orders.py",
    }
    for path in MODULES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name in ALLOWED_IMPORTS, f"{path.name} importa {name}"
    # `client` só para o `InputError`: quem monta e envia requisição é o `ToolRunner`.
    source = (ROOT / "app" / "admin_mcp" / "compose.py").read_text(encoding="utf-8")
    assert "from app.admin_mcp.client import InputError" in source


def test_no_composite_writes_a_confirmation_literal() -> None:
    """O servidor decide o que vale; a composta só repassa o que o modelo mandou (`confirmations`)."""
    for path in MODULES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    named = isinstance(key, ast.Constant) and str(key.value).startswith("confirm")
                    assert not (named and not isinstance(value, ast.Name)), (path.name, node.lineno)
            if (
                isinstance(node, ast.keyword)
                and node.arg
                and node.arg.startswith("confirm")
                and node.arg != "confirmation"  # metadado de ToolOutput, não autorização HTTP
            ):
                raise AssertionError(f"{path.name}: argumento {node.arg}= escrito à mão")
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                assert not str(node.slice.value).startswith("confirm") or not isinstance(
                    node.ctx, ast.Store
                ), (path.name, node.lineno)


def test_a_composite_cannot_call_an_operation_it_did_not_declare(tmp_path: Path) -> None:
    world = World(tmp_path)
    composite = world.composites["diagnose_order"]
    steps = Steps(composite, world.runner, world.tools, Asker())

    with pytest.raises(Exception, match="não declarou a chamada a `reprocess_job`"):
        asyncio.run(
            steps.call("reprocess_job", {"public_id": "x", "reason": "x", "expected_attempt": 1})
        )
    assert world.api.calls() == []
    world.close()


# =================================================================================================
# contra o app de verdade
# =================================================================================================


@pytest.fixture
def forms(deps: Deps) -> None:
    """O formulário demo-definicao (versão 1), com os três legados que o banco já traz."""
    stored.import_definition(deps, DEFINITION, actor="testes")


@pytest.fixture
def baserow(deps: Deps) -> FakeBaserow:
    return FakeBaserow().install(deps)


@pytest.fixture
def live(api_client: Make, forms: None, baserow: FakeBaserow, tmp_path: Path) -> Iterator[Live]:
    found = Live(api_client("total"), tmp_path, pace=True)
    yield found
    assert all(found.key not in text for text in found.outputs)  # a chave não aparece em nada
    found.close()


def draft_token(deps: Deps, slug: str = SLUG) -> str:
    with deps.sessions() as session:
        found = drafts.load(session, slug)
    assert found is not None
    return found.token


def edit_draft(deps: Deps, ops: list[dict[str, Any]] = INTRO) -> None:
    """Outra pessoa edita o rascunho pela página (o serviço), não pelo MCP."""
    drafts.patch_document(deps, SLUG, ops, actor="operadora", expected=draft_token(deps))


def by_key(deps: Deps, action: str) -> list[Any]:
    """Os registros de auditoria de uma ação feita por uma chave de API (e não pelo mundo do teste)."""
    return [entry for entry in audited(deps, action) if entry.actor.startswith("chave:")]


def posts(live: Live, name: str) -> list[httpx.Request]:
    return [r for r in live.sent() if operation_of(r) == name]


# --- attention_overview --------------------------------------------------------------------------


def test_the_overview_lists_failed_orders_failed_syncs_and_what_to_check(
    deps: Deps, live: Live, tmp_path: Path
) -> None:
    jobs = build_jobs_world(deps)

    output = live.compose("attention_overview")

    assert not output.is_error and output.text.startswith("PEDE ATENÇÃO")
    assert f"`{jobs.carla.public_id}`" in output.text and "llm_unavailable" in output.text
    assert "Pedidos que falharam: 1" in output.text
    assert (
        f"`{jobs.beatriz.public_id}`" in output.text
        and "Sincronizações com a planilha" in output.text
    )
    assert "reforma-2027" in output.text  # o aviso dos dados
    assert "diagnose_order" in output.text
    assert "Em ordem:" in output.text and "todo formulário tem destino" in output.text
    # A entrega que falhou na tentativa 1 do `beatriz` já foi reprocessada: não pede atenção.
    assert "nenhuma entrega ao webhook falhou" in output.text
    assert {operation_of(r) for r in live.sent()} == CALLS["attention_overview"]


def test_the_overview_says_which_forms_have_no_destination_and_which_drafts_are_pending(
    deps: Deps, live: Live
) -> None:
    edit_draft(deps)

    output = live.compose("attention_overview")

    assert "Formulários sem destino (os planos deles não são entregues): 4" in output.text
    for slug in (SLUG, "demo", "mcc", "reforma-2027"):
        assert f"- {slug}" in output.text
    assert "Rascunhos ainda não publicados: 1" in output.text
    assert f"- {SLUG} (publicada: v1)" in output.text
    assert "save_destination" in output.text and "publish_form_with_review" in output.text


def test_the_overview_finds_a_failed_delivery_of_an_order_older_than_the_fifty_most_recent(
    deps: Deps, live: Live
) -> None:
    """A API filtra a entrega (`delivery=failed`): a visão geral não baixa mais os 50 pedidos mais
    recentes para procurar, e uma falha antiga não passa batido."""
    old = new_lead_job(deps, "Lead antiga", "antiga@example.test")
    deliver_to_the_webhook(deps, old.id, outcome="failed")
    for number in range(55):  # mais do que os 50 que a visão geral baixava
        new_lead_job(deps, f"Lead {number}", f"lead-{number}@example.test")

    output = live.compose("attention_overview")

    assert output.text.startswith("PEDE ATENÇÃO")
    assert "Entregas ao webhook que falharam: 1 pedido(s)" in output.text
    assert f"`{old.public_id}`" in output.text and "1 entrega(s) com falha" in output.text
    asked = [dict(r.url.params) for r in live.sent() if operation_of(r) == "list_jobs"]
    assert asked == [
        {"status": "failed", "limit": "10", "include_total": "true", **NO_LEAD_DATA},
        {"delivery": "failed", "limit": "10", "include_total": "true", **NO_LEAD_DATA},
    ]


def test_the_overview_names_the_secrets_that_do_not_open(deps: Deps, live: Live) -> None:
    build_jobs_world(deps)
    # A chave de hoje não abre o que foi guardado com a de antes.
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))

    output = live.compose("attention_overview")

    assert "Segredos que não abrem: 2" in output.text
    assert "destino «default»" in output.text and "token do Baserow de «demo»" in output.text
    assert BASEROW_TOKEN not in output.text


def test_a_leitura_key_gets_the_overview_and_the_diagnosis(
    api_client: Make, deps: Deps, forms: None, baserow: FakeBaserow, tmp_path: Path
) -> None:
    jobs = build_jobs_world(deps)
    reader = Live(api_client("leitura"), tmp_path, pace=True)

    overview = reader.compose("attention_overview")
    diagnosis = reader.compose("diagnose_order", {"public_id": jobs.carla.public_id})

    assert set(reader.composites) == READS
    assert overview.text.startswith("PEDE ATENÇÃO") and not diagnosis.is_error
    # O conserto é de `total`: a sugestão diz isso em vez de oferecer o que a chave não tem.
    assert "`reprocess_job` exige uma chave `total`" in diagnosis.text
    assert {r.method for r in reader.sent()} == {"GET"}
    reader.close()


def test_the_overview_never_asks_for_lead_data_with_or_without_the_option(
    api_client: Make, deps: Deps, forms: None, baserow: FakeBaserow, tmp_path: Path
) -> None:
    build_jobs_world(deps)
    for lead_data in (False, True):
        keyed = Live(
            api_client("total"), folder(tmp_path, str(lead_data)), lead_data=lead_data, pace=True
        )

        output = keyed.compose("attention_overview")

        assert not output.is_error
        for name, email in PEOPLE.values():
            assert name not in output.text and email not in output.text
        for request in keyed.sent():
            assert request.url.params.get("include_lead_data", "false") == "false"
            assert "q" not in request.url.params and not request.url.path.endswith("/pdf")
            assert request.method == "GET"
        keyed.close()


def test_a_section_that_cannot_be_read_is_named_and_the_others_still_come(
    deps: Deps, api_client: Make, forms: None, baserow: FakeBaserow, tmp_path: Path
) -> None:
    """O limite de uso acaba no meio: a visão geral diz o que não leu, em vez de fingir que está em dia."""
    build_jobs_world(deps)
    hurried = Live(api_client("total"), tmp_path)  # sem `pace`: o balde de 30 fichas não enche

    first = hurried.compose("attention_overview")  # 4 + 4 integrações de 5 fichas = 24
    second = hurried.compose("attention_overview")

    assert "NÃO CONSEGUI LER" not in first.text
    assert second.text.startswith("PEDE ATENÇÃO") and "Pedidos que falharam: 1" in second.text
    assert "NÃO CONSEGUI LER (a visão geral está incompleta)" in second.text
    assert "429 rate_limited" in second.text and "integração de" in second.text
    assert not second.is_error
    hurried.close()


# --- diagnose_order ------------------------------------------------------------------------------


def test_the_diagnosis_of_a_failed_order_says_why_and_suggests_reprocessing_without_doing_it(
    deps: Deps, live: Live
) -> None:
    jobs = build_jobs_world(deps)

    output = live.compose("diagnose_order", {"public_id": jobs.carla.public_id})

    assert not output.is_error
    assert "Situação: failed" in output.text and "`llm_unavailable`" in output.text
    assert "o provedor da IA não respondeu" in output.text
    assert f"`public_id: {jobs.carla.public_id}`, `expected_attempt: 1`" in output.text
    assert "`reprocess_job`; ação marcada: pede a confirmação da pessoa" in output.text
    assert [operation_of(r) for r in live.sent()] == ["get_job"]  # sugere; não reprocessa
    assert FAILURE_DETAIL not in output.text  # o texto livre é dado da lead


def test_the_diagnosis_of_a_completed_order_with_a_failed_sync_points_at_the_resend(
    deps: Deps, live: Live
) -> None:
    jobs = build_jobs_world(deps)

    output = live.compose("diagnose_order", {"public_id": jobs.beatriz.public_id})

    assert "tentativa 2" in output.text and "reprocess_requested (actor operadora)" in output.text
    assert "failed, 4 tentativa(s)" in output.text and "`resend_job_sync`" in output.text
    assert re.search(r"`sync_id: [0-9a-f-]{36}`", output.text)
    assert "ainda não saiu" in output.text  # a entrega pendente: esperar, não reenviar
    assert "1 entrega(s) ou sincronização(ões) de tentativas anteriores falharam" in output.text
    assert [operation_of(r) for r in live.sent()] == ["get_job"]


def test_the_diagnosis_of_a_queued_order_says_to_wait_and_a_good_one_says_nothing_to_do(
    deps: Deps, live: Live
) -> None:
    jobs = build_jobs_world(deps)

    queued = live.compose("diagnose_order", {"public_id": jobs.debora.public_id})
    fine = live.compose("diagnose_order", {"public_id": jobs.ana.public_id})

    assert "na fila: espere" in queued.text and "reprocess_job" not in queued.text
    assert "Nada a fazer" in fine.text and "resend_job" not in fine.text


def test_the_diagnosis_of_an_order_that_does_not_exist_is_the_api_refusal(live: Live) -> None:
    output = live.compose("diagnose_order", {"public_id": "nao-existe"})

    assert output.is_error and "404 not_found" in output.text


def test_a_path_with_a_slash_is_refused_by_the_runner_path_before_anything_is_sent(
    deps: Deps, live: Live
) -> None:
    jobs = build_jobs_world(deps)

    output = live.compose("diagnose_order", {"public_id": f"{jobs.ana.public_id}/pdf"})

    assert output.is_error and "`public_id` só pode ter" in output.text
    assert live.sent() == []  # nem o PDF de uma lead nem outra operação


def test_without_the_option_the_diagnosis_cannot_ask_for_lead_data_and_shows_no_person(
    deps: Deps, live: Live
) -> None:
    jobs = build_jobs_world(deps)

    refused = live.compose(
        "diagnose_order", {"public_id": jobs.carla.public_id, "include_lead_data": True}
    )
    texts = [live.compose("diagnose_order", {"public_id": job.public_id}).text for job in jobs.all]

    assert (
        refused.is_error
        and "Argumentos que esta ferramenta não tem: `include_lead_data`" in refused.text
    )
    for name, email in PEOPLE.values():
        assert all(name not in text and email not in text for text in texts)
    # O servidor fixa `include_lead_data=false`: o que sai é sempre falso, e a composta não o escolhe.
    assert all(r.url.params.get("include_lead_data", "false") == "false" for r in live.sent())


def test_with_the_option_the_diagnosis_shows_the_error_texts_and_still_no_person(
    api_client: Make, deps: Deps, forms: None, baserow: FakeBaserow, tmp_path: Path
) -> None:
    jobs = build_jobs_world(deps)
    on = Live(api_client("total"), tmp_path, lead_data=True, pace=True)

    plain = on.compose("diagnose_order", {"public_id": jobs.carla.public_id})
    with_notes = on.compose(
        "diagnose_order", {"public_id": jobs.carla.public_id, "include_lead_data": True}
    )

    assert FAILURE_DETAIL not in plain.text  # pedir é uma escolha de cada chamada
    assert "include_lead_data" not in on.sent()[0].url.params
    assert on.sent()[1].url.params["include_lead_data"] == "true"
    assert FAILURE_DETAIL in with_notes.text and "failure_detail" in with_notes.text
    for name, email in PEOPLE.values():
        assert name not in with_notes.text and email not in with_notes.text
    on.close()


# --- edit_form_draft -----------------------------------------------------------------------------


def test_editing_the_draft_saves_it_compares_it_with_the_published_version_and_publishes_nothing(
    deps: Deps, live: Live
) -> None:
    before = draft_token(deps)

    output = live.compose("edit_form_draft", {"slug": SLUG, "patch": INTRO})

    assert not output.is_error
    assert "Rascunho de «demo-definicao» gravado. NADA foi publicado." in output.text
    assert (
        before in output.text and draft_token(deps) in output.text and draft_token(deps) != before
    )
    assert "Pendente de publicação: sim" in output.text
    assert "Contra a versão publicada 1" in output.text and "Página pública" in output.text
    assert '+ "intro": "Uma introdução nova para o diagnóstico."' in output.text
    assert "publish_form_with_review" in output.text
    assert [operation_of(r) for r in live.sent()] == [
        "get_form_draft",
        "patch_form_draft",
        "compare_form_versions",
    ]
    assert len(audited(deps, "form.draft.save")) == 1 and audited(deps, "form.publish") == []
    assert not any(confirmed(r) for r in live.sent())
    assert len(output.text) < 3000  # o documento inteiro do rascunho não vai ao modelo


def test_a_patch_written_against_an_old_draft_is_a_conflict_that_says_the_current_version(
    deps: Deps, live: Live
) -> None:
    old = draft_token(deps)
    edit_draft(deps, [{"op": "replace", "path": "/intro", "value": "Outra pessoa escreveu isto."}])

    output = live.compose(
        "edit_form_draft", {"slug": SLUG, "patch": INTRO, "expected_version": old}
    )

    assert output.is_error and "409 version_conflict" in output.text
    assert f"A versão de agora é {draft_token(deps)}" in output.text
    assert "Nada foi gravado no rascunho" in output.text
    with deps.sessions() as session:
        document = drafts.load(session, SLUG).document  # type: ignore[union-attr]
    assert document["intro"] == "Outra pessoa escreveu isto."  # o patch velho não passou por cima
    assert "compare_form_versions" not in [operation_of(r) for r in live.sent()]


def test_a_patch_that_does_not_apply_is_a_422_that_names_the_operation_and_saves_nothing(
    deps: Deps, live: Live
) -> None:
    before = draft_token(deps)
    bad = [*INTRO, {"op": "replace", "path": "/nao/existe", "value": 1}]

    output = live.compose("edit_form_draft", {"slug": SLUG, "patch": bad})

    assert output.is_error and "422 validation_failed" in output.text
    assert (
        "patch.1" in output.text
        and "Nada foi gravado no rascunho (o patch é tudo ou nada)" in output.text
    )
    assert draft_token(deps) == before  # nem a operação que valia
    assert audited(deps, "form.draft.save") == []


def test_a_form_without_a_published_version_is_edited_and_the_comparison_is_skipped_honestly(
    deps: Deps, live: Live
) -> None:
    drafts.create(deps, title="Em rascunho", slug="em-rascunho", actor="testes")

    output = live.compose(
        "edit_form_draft",
        {"slug": "em-rascunho", "patch": [{"op": "replace", "path": "/title", "value": "Outro"}]},
    )

    assert not output.is_error and "gravado" in output.text
    assert "ainda não tem versão publicada: não há com o que comparar" in output.text
    assert "compare_form_versions" not in [operation_of(r) for r in live.sent()]


def test_editing_a_draft_that_does_not_exist_is_a_not_found(live: Live) -> None:
    output = live.compose("edit_form_draft", {"slug": "nao-existe", "patch": INTRO})

    assert output.is_error and "404 not_found" in output.text
    assert [operation_of(r) for r in live.sent()] == ["get_form_draft"]


# --- publish_form_with_review --------------------------------------------------------------------


def test_the_review_alone_simulates_shows_what_changes_and_never_publishes(
    deps: Deps, live: Live
) -> None:
    edit_draft(deps)
    asker = Asker()

    output = live.compose("publish_form_with_review", {"slug": SLUG, "note": NOTE}, asker)

    assert not output.is_error and asker.messages == []
    assert "Publicar criaria a versão 2 (hoje: 1)" in output.text
    assert "Seções que mudam: Página pública" in output.text
    assert (
        "Simulação (com o PDF de exemplo, sem gravar nada):\n- pronta: nada falta." in output.text
    )
    assert f"`expected_version: {draft_token(deps)}`" in output.text
    assert [operation_of(r) for r in live.sent()] == [
        "review_form_draft_publication",
        "publish_form_draft",
    ]
    (simulation,) = posts(live, "publish_form_draft")
    assert json.loads(simulation.content)["dry_run"] is True and not confirmed(simulation)
    assert audited(deps, "form.publish") == []


def test_publishing_asks_the_person_with_what_changes_and_only_the_yes_reaches_the_api(
    deps: Deps, live: Live
) -> None:
    edit_draft(deps)
    asker = Asker(True)

    output = live.compose(
        "publish_form_with_review", {"slug": SLUG, "note": NOTE, "publish": True}, asker
    )

    assert (
        not output.is_error and "PUBLICADO: «demo-definicao» agora está na versão 2" in output.text
    )
    (question,) = asker.messages
    assert "publicar o rascunho de «demo-definicao» como a versão 2" in question
    assert "Muda: Página pública" in question and f"Nota da publicação: {NOTE}" in question
    assert (
        "Confirma «Publicar o rascunho (ou simular a publicação)» (`publish_form_draft`)?"
        in question
    )
    first, final = posts(live, "publish_form_draft")
    assert json.loads(first.content)["dry_run"] is True and not confirmed(first)
    assert json.loads(final.content) == {
        "expected_version": draft_token(deps),
        "note": NOTE,
        "confirm": True,  # só depois do sim; a composta não o escolheu
    }
    (entry,) = audited(deps, "form.publish")
    assert entry.actor.startswith("chave:")
    with deps.sessions() as session:
        assert drafts.load(session, SLUG).pending is False  # type: ignore[union-attr]


def test_a_no_leaves_the_draft_unpublished_and_says_so(deps: Deps, live: Live) -> None:
    edit_draft(deps)

    output = live.compose(
        "publish_form_with_review", {"slug": SLUG, "note": NOTE, "publish": True}, Asker(False)
    )

    assert (
        output.is_error
        and "O cliente não confirmou: informou recusa (`decline`). A operação solicitada não foi executada."
        in output.text
    )
    assert "Nada foi publicado." in output.text
    assert audited(deps, "form.publish") == [] and not any(confirmed(r) for r in live.sent())


def test_without_a_way_to_ask_the_publication_waits_for_the_same_call_with_the_confirm(
    deps: Deps, live: Live
) -> None:
    edit_draft(deps)
    arguments = {"slug": SLUG, "note": NOTE, "publish": True}

    first = live.compose("publish_form_with_review", arguments)
    nothing_yet = audited(deps, "form.publish")
    second = live.compose("publish_form_with_review", {**arguments, "confirm": True})

    assert first.awaiting and not first.is_error
    # O que a pessoa confirma vem antes da descrição do efeito: a publicação, a nota e o que muda.
    assert first.text.startswith("Pedido de `publish_form_with_review`: publicar o rascunho")
    assert (
        f"Nota da publicação: {NOTE}" in first.text and "Publicar criaria a versão 2" in first.text
    )
    assert "CONFIRMAÇÃO NECESSÁRIA: nada foi executado." in first.text
    assert (
        "chame `publish_form_with_review` de novo, com os mesmos argumentos e `confirm: true`"
        in first.text
    )
    assert f"expected_version: {draft_token(deps)}" in first.text
    assert nothing_yet == [] and "PUBLICADO" in second.text
    assert len(audited(deps, "form.publish")) == 1


def test_repeating_a_shown_call_without_the_confirm_executes_nothing_for_the_three_marked_actions(
    deps: Deps, live: Live
) -> None:
    """Quem põe o `confirm` é o modelo, depois do sim da pessoa: a composta não o põe na repetição."""
    edit_draft(deps)
    publish = {"slug": SLUG, "note": NOTE, "publish": True}
    live.compose("publish_form_with_review", publish)
    again = live.compose("publish_form_with_review", publish)
    assert again.awaiting and audited(deps, "form.publish") == []

    live.compose("connect_baserow_integration", CONNECT)
    again = live.compose("connect_baserow_integration", CONNECT)
    assert again.awaiting and stored_integration(deps) is None

    live.compose("connect_baserow_integration", {**CONNECT, "confirm": True})  # a pessoa disse sim
    live.compose("connect_baserow_integration", {"slug": SLUG, "activate": True})
    again = live.compose("connect_baserow_integration", {"slug": SLUG, "activate": True})
    assert again.awaiting and stored_integration(deps).active is False
    # O único `confirm` que saiu foi o da conexão, que o modelo repetiu depois do sim da pessoa.
    assert [operation_of(r) for r in live.sent() if confirmed(r)] == ["connect_form_integration"]


def test_the_confirm_the_model_writes_does_not_publish_a_call_the_server_never_showed(
    deps: Deps, live: Live
) -> None:
    edit_draft(deps)

    output = live.compose(
        "publish_form_with_review", {"slug": SLUG, "note": NOTE, "publish": True, "confirm": True}
    )

    assert output.awaiting and "CONFIRMAÇÃO NECESSÁRIA" in output.text
    assert audited(deps, "form.publish") == [] and not any(confirmed(r) for r in live.sent())


def test_the_confirmation_covers_the_call_that_was_shown_and_no_other(
    deps: Deps, live: Live
) -> None:
    """A pessoa viu a publicação com a nota A; a chamada seguinte com a nota B é outra ação: pergunta de novo."""
    edit_draft(deps)
    shown = {"slug": SLUG, "note": "Nota que a pessoa viu", "publish": True}
    live.compose("publish_form_with_review", shown)

    other = live.compose(
        "publish_form_with_review", {**shown, "note": "Outra nota", "confirm": True}
    )
    nothing_yet = audited(deps, "form.publish")
    same = live.compose("publish_form_with_review", {**shown, "confirm": True})

    assert other.awaiting and nothing_yet == []
    assert not same.awaiting and "PUBLICADO" in same.text  # a mesma ação que foi mostrada


def test_a_draft_that_changed_since_the_one_the_person_saw_stops_before_simulating(
    deps: Deps, live: Live
) -> None:
    edit_draft(deps)
    seen = draft_token(deps)
    edit_draft(
        deps, [{"op": "replace", "path": "/intro", "value": "Mudou depois de a pessoa ver."}]
    )

    output = live.compose(
        "publish_form_with_review",
        {"slug": SLUG, "note": NOTE, "publish": True, "expected_version": seen, "confirm": True},
    )

    assert output.is_error and f"({seen} -> {draft_token(deps)})" in output.text
    assert "Nada foi simulado nem publicado." in output.text
    assert [operation_of(r) for r in live.sent()] == ["review_form_draft_publication"]
    assert audited(deps, "form.publish") == []


def test_a_draft_that_changes_between_the_simulation_and_the_yes_is_a_409_and_nothing_is_published(
    deps: Deps, live: Live
) -> None:
    edit_draft(deps)

    class Racing(Asker):
        async def ask(self, message: str) -> ConfirmationOutcome:
            edit_draft(
                deps, [{"op": "replace", "path": "/intro", "value": "Mudou enquanto a pessoa lia."}]
            )
            return await super().ask(message)

    output = live.compose(
        "publish_form_with_review", {"slug": SLUG, "note": NOTE, "publish": True}, Racing(True)
    )

    assert output.is_error and "409 version_conflict" in output.text
    assert (
        "Nada foi publicado." in output.text
        and f"A versão de agora é {draft_token(deps)}" in output.text
    )
    assert audited(deps, "form.publish") == []


def test_nothing_to_publish_is_a_refusal_when_publishing_and_a_note_when_only_reviewing(
    live: Live,
) -> None:
    only_review = live.compose("publish_form_with_review", {"slug": SLUG})
    publishing = live.compose(
        "publish_form_with_review", {"slug": SLUG, "note": NOTE, "publish": True}
    )

    assert not only_review.is_error and "Não há o que publicar" in only_review.text
    assert publishing.is_error and "Não há o que publicar" in publishing.text
    assert "publish_form_draft" not in [operation_of(r) for r in live.sent()]  # nem a simulação


def test_a_missing_note_is_reported_by_the_simulation_and_stops_before_the_confirmation(
    deps: Deps, live: Live
) -> None:
    edit_draft(deps)
    asker = Asker(True)

    output = live.compose("publish_form_with_review", {"slug": SLUG, "publish": True}, asker)

    assert output.is_error and "FALTA: Escreva uma nota do que mudou" in output.text
    assert "NÃO publiquei: falta o que está acima." in output.text
    assert asker.messages == [] and not any(confirmed(r) for r in live.sent())
    assert len(posts(live, "publish_form_draft")) == 1  # só a simulação


def test_what_is_missing_for_the_llm_is_listed_and_blocks_the_publication(
    deps: Deps, live: Live
) -> None:
    edit_draft(deps, [{"op": "replace", "path": "/instructions", "value": "Instruções novas."}])
    asker = Asker(True)

    output = live.compose(
        "publish_form_with_review", {"slug": SLUG, "note": NOTE, "publish": True}, asker
    )

    assert output.is_error
    assert "FALTA: Teste o rascunho com o LLM antes de publicar" in output.text
    assert "FALTA um teste com sucesso (`start_form_draft_test`)" in output.text
    assert "NÃO publiquei" in output.text and asker.messages == []
    assert audited(deps, "form.publish") == []


@pytest.fixture
def contact(deps: Deps, tmp_path: Path, api_client: Make, baserow: FakeBaserow) -> Iterator[Live]:
    """O formulário cujo prompt cita o nome e o e-mail da lead, com um rascunho para publicar."""
    stored.import_definition(deps, CONTACT_PROMPT, actor="testes", contact_confirmed=True)
    edit_draft(deps)
    found = Live(api_client("total"), tmp_path, pace=True)
    yield found
    found.close()


def test_a_prompt_that_cites_contact_data_gets_the_second_question_and_both_must_be_yes(
    deps: Deps, contact: Live
) -> None:
    asker = Asker(True, True)

    output = contact.compose(
        "publish_form_with_review", {"slug": SLUG, "note": NOTE, "publish": True}, asker
    )

    assert "PUBLICADO" in output.text and len(asker.messages) == 2
    assert "O prompt manda dados de contato da lead à IA" in asker.messages[0]
    assert "Confirma «Publicar o rascunho" in asker.messages[0]
    assert "o nome e o e-mail" in asker.messages[1]  # a segunda mostra o que o prompt manda
    assert "A API pede mais uma confirmação" in asker.messages[1]
    final = posts(contact, "publish_form_draft")[-1]
    assert json.loads(final.content)["confirm"] is True
    assert json.loads(final.content)["confirm_contact"] is True
    assert len(audited(deps, "form.publish")) == 1


def test_the_review_says_the_contact_confirmation_is_coming_before_asking_anything(
    contact: Live,
) -> None:
    output = contact.compose("publish_form_with_review", {"slug": SLUG, "note": NOTE})

    assert (
        "O prompt manda dados de contato da lead à IA em todo pedido: name, email." in output.text
    )
    assert "A publicação vai pedir mais uma confirmação" in output.text
    assert "e `confirm_contact: true` (só depois do sim dela à segunda pergunta)" in output.text
    assert not output.is_error and "NÃO publiquei" not in output.text


def test_a_no_to_the_contact_confirmation_publishes_nothing(deps: Deps, contact: Live) -> None:
    output = contact.compose(
        "publish_form_with_review",
        {"slug": SLUG, "note": NOTE, "publish": True},
        Asker(True, False),
    )

    assert output.is_error and "O cliente não confirmou" in output.text
    assert audited(deps, "form.publish") == []


def test_without_a_way_to_ask_the_contact_confirmation_is_a_third_call_with_its_own_flag(
    deps: Deps, contact: Live
) -> None:
    arguments = {"slug": SLUG, "note": NOTE, "publish": True}

    first = contact.compose("publish_form_with_review", arguments)
    second = contact.compose("publish_form_with_review", {**arguments, "confirm": True})
    nothing_yet = audited(deps, "form.publish")
    third = contact.compose(
        "publish_form_with_review", {**arguments, "confirm": True, "confirm_contact": True}
    )

    assert first.awaiting and second.awaiting and nothing_yet == []
    assert (
        "o nome e o e-mail" in second.text
        and "`confirm: true` e `confirm_contact: true`" in second.text
    )
    assert "chame `publish_form_with_review` de novo" in second.text
    assert "PUBLICADO" in third.text and len(audited(deps, "form.publish")) == 1


def test_the_confirm_contact_the_model_writes_before_the_server_showed_the_text_is_not_sent(
    deps: Deps, contact: Live
) -> None:
    output = contact.compose(
        "publish_form_with_review",
        {"slug": SLUG, "note": NOTE, "publish": True, "confirm": True, "confirm_contact": True},
    )

    assert output.awaiting and audited(deps, "form.publish") == []
    assert not any(confirmed(r) for r in contact.sent())


# --- connect_baserow_integration -----------------------------------------------------------------


def stored_integration(deps: Deps) -> Any:
    from app.integrations import config

    with deps.sessions() as session:
        return config.load(session, SLUG)


def test_connecting_asks_once_saves_the_suggested_mapping_shows_the_report_and_does_not_activate(
    deps: Deps, live: Live
) -> None:
    asker = Asker(True)

    output = live.compose("connect_baserow_integration", CONNECT, asker)

    assert not output.is_error
    (question,) = asker.messages
    assert (
        "conectar «demo-definicao» ao servidor https://baserow.example.test, tabela 7" in question
    )
    assert (
        "Hoje o formulário não tem integração." in question and "ativar é outro passo" in question
    )
    assert "Confirma «Conectar o formulário a uma tabela do Baserow»" in question
    assert BASEROW_TOKEN not in question and "Campos enviados: base_url" in question
    assert "Conectado à tabela 7 em baserow.example.test" in output.text
    assert "Mapeamento guardado: 6 campo(s)." in output.text
    assert (
        "Nome (#101) <- lead.name" in output.text
        and "Código do pedido (#104) <- job.public_id" in output.text
    )
    assert "Relatório do mapeamento contra a tabela de agora: LIMPO" in output.text
    assert "segue DESATIVADA" in output.text and "`activate: true`" in output.text
    saved = stored_integration(deps)
    assert saved.active is False and len(saved.entries) == 6
    assert [operation_of(r) for r in live.sent()] == [
        "get_form_integration",
        "connect_form_integration",
        "get_form_integration",
        "save_form_integration_mapping",
    ]
    assert BASEROW_TOKEN not in output.text
    assert (
        len(audited(deps, "integration.create")) == 1
        and audited(deps, "integration.activate") == []
    )


def test_the_integration_is_activated_only_in_a_second_call_after_the_report_and_a_yes(
    deps: Deps, live: Live
) -> None:
    live.compose("connect_baserow_integration", CONNECT, Asker(True))
    asker = Asker(True)

    output = live.compose("connect_baserow_integration", {"slug": SLUG, "activate": True}, asker)

    assert not output.is_error and output.text.startswith(
        "ATIVADA: a integração de «demo-definicao»"
    )
    (question,) = asker.messages
    assert "ativar a integração de «demo-definicao» com a tabela 7" in question
    assert "Relatório do mapeamento: 6 campo(s) mapeado(s), sem pendências" in question
    assert "Confirma «Ativar a integração de um formulário com o Baserow»" in question
    assert stored_integration(deps).active is True
    assert len(audited(deps, "integration.activate")) == 1
    again = live.compose(
        "connect_baserow_integration", {"slug": SLUG, "activate": True}, Asker(True)
    )
    assert not again.is_error and "já está ativa" in again.text


def test_connecting_and_activating_in_one_call_is_refused_before_anything_happens(
    deps: Deps, live: Live
) -> None:
    asker = Asker(True, True)

    output = live.compose("connect_baserow_integration", {**CONNECT, "activate": True}, asker)

    assert (
        output.is_error
        and "Ativar é um passo à parte" in output.text
        and "Nada foi feito." in output.text
    )
    assert live.sent() == [] and asker.messages == [] and stored_integration(deps) is None


def test_without_a_way_to_ask_nothing_is_connected_or_activated_until_the_shown_call_is_repeated(
    deps: Deps, live: Live
) -> None:
    first = live.compose("connect_baserow_integration", CONNECT)
    assert first.awaiting and stored_integration(deps) is None
    assert "CONFIRMAÇÃO NECESSÁRIA: nada foi executado." in first.text
    assert (
        "chame `connect_baserow_integration` de novo, com os mesmos argumentos e `confirm: true`"
        in first.text
    )
    assert BASEROW_TOKEN not in first.text
    assert [operation_of(r) for r in live.sent()] == ["get_form_integration"]  # nada foi gravado

    second = live.compose("connect_baserow_integration", {**CONNECT, "confirm": True})
    assert not second.awaiting and stored_integration(deps).active is False

    waiting = live.compose("connect_baserow_integration", {"slug": SLUG, "activate": True})
    assert waiting.awaiting and stored_integration(deps).active is False
    assert "Relatório do mapeamento contra a tabela de agora: LIMPO" in waiting.text
    done = live.compose(
        "connect_baserow_integration", {"slug": SLUG, "activate": True, "confirm": True}
    )
    assert done.text.startswith("ATIVADA") and stored_integration(deps).active is True
    assert [r for r in live.sent() if r.method == "PUT" or r.method == "POST"] != []


def test_the_confirm_the_model_writes_does_not_connect_or_activate_what_was_never_shown(
    deps: Deps, live: Live
) -> None:
    connecting = live.compose("connect_baserow_integration", {**CONNECT, "confirm": True})

    assert connecting.awaiting and stored_integration(deps) is None
    assert not any(confirmed(r) for r in live.sent())
    assert [operation_of(r) for r in live.sent()] == ["get_form_integration"]


def test_a_no_to_the_connection_changes_nothing(deps: Deps, live: Live) -> None:
    output = live.compose("connect_baserow_integration", CONNECT, Asker(False))

    assert (
        output.is_error
        and "O cliente não confirmou" in output.text
        and "Nada foi gravado." in output.text
    )
    assert stored_integration(deps) is None


def test_an_address_the_api_refuses_is_a_422_with_the_field_and_nothing_is_saved(
    deps: Deps, live: Live
) -> None:
    output = live.compose(
        "connect_baserow_integration",
        {**CONNECT, "base_url": "https://x.example.test/?a=1"},
        Asker(True),
    )

    assert output.is_error and "422 validation_failed" in output.text
    assert "- base_url: " in output.text and "Nada foi gravado." in output.text
    assert stored_integration(deps) is None and BASEROW_TOKEN not in output.text


def test_a_version_the_integration_has_left_behind_is_a_409_that_says_the_current_one(
    deps: Deps, live: Live
) -> None:
    live.compose("connect_baserow_integration", CONNECT, Asker(True))

    output = live.compose(
        "connect_baserow_integration", {**CONNECT, "expected_version": "0"}, Asker(True)
    )

    assert output.is_error and "409 version_conflict" in output.text
    assert f"A versão de agora é {stored_integration(deps).version}" in output.text
    assert "Nada foi gravado." in output.text


def test_a_baserow_that_does_not_answer_is_a_502_and_the_connection_is_not_saved(
    deps: Deps, live: Live, baserow: FakeBaserow
) -> None:
    baserow.fail("GET", 500, where="fields")

    output = live.compose("connect_baserow_integration", CONNECT, Asker(True))

    assert (
        output.is_error
        and "502 upstream_failed" in output.text
        and "Nada foi gravado." in output.text
    )
    assert stored_integration(deps) is None


def test_a_mapping_the_api_refuses_after_the_connection_says_what_was_already_done(
    deps: Deps, live: Live
) -> None:
    output = live.compose(
        "connect_baserow_integration",
        {**CONNECT, "sources": {"101": "lead.name"}},  # sem o campo do código do pedido
        Asker(True),
    )

    assert output.is_error and "Parei em «guardar o mapeamento»" in output.text
    assert "422 validation_failed" in output.text and "código do pedido" in output.text
    assert "Já feito:\n- Conectado à tabela 7" in output.text
    assert "Não feito:\n- guardar o mapeamento\n- mostrar o relatório" in output.text
    assert "O mapeamento não foi gravado. A conexão foi." in output.text
    saved = stored_integration(deps)
    assert saved is not None and saved.active is False and saved.entries == ()


def test_an_explicit_mapping_replaces_the_suggestion_and_a_kept_one_is_not_overwritten(
    deps: Deps, live: Live
) -> None:
    mapping = {"101": "lead.name", "104": "job.public_id"}
    explicit = live.compose(
        "connect_baserow_integration", {**CONNECT, "sources": mapping}, Asker(True)
    )
    rotated = live.compose(
        "connect_baserow_integration", CONNECT, Asker(True)
    )  # só o token de novo

    assert "Mapeamento (o que você mandou):" in explicit.text
    assert "Mapeamento guardado: 2 campo(s)." in explicit.text
    assert "Mapeamento mantido (2 campo(s))" in rotated.text
    assert len(stored_integration(deps).entries) == 2
    assert (
        posts(live, "save_form_integration_mapping")
        and len(posts(live, "save_form_integration_mapping")) == 1
    )


def test_rotating_the_token_of_an_active_integration_keeps_it_active_and_says_so(
    deps: Deps, live: Live
) -> None:
    live.compose("connect_baserow_integration", CONNECT, Asker(True))
    live.compose("connect_baserow_integration", {"slug": SLUG, "activate": True}, Asker(True))
    asker = Asker(True)

    output = live.compose("connect_baserow_integration", CONNECT, asker)

    (question,) = asker.messages
    assert "ativa, com 6 campo(s) mapeado(s)" in question
    assert "O token guardado será trocado pelo que veio. A integração continua ativa." in question
    assert "A integração está ATIVA e segue gravando" in output.text
    assert "DESATIVADA" not in output.text and "desativada" not in output.text.split("Feito:")[1]
    assert stored_integration(deps).active is True


def test_a_new_mapping_with_pendencies_says_the_active_integration_was_switched_off(
    deps: Deps, live: Live, baserow: FakeBaserow
) -> None:
    mapping = {"104": "job.public_id", "108": "answer:tamanho_equipe"}
    live.compose("connect_baserow_integration", {**CONNECT, "sources": mapping}, Asker(True))
    live.compose("connect_baserow_integration", {"slug": SLUG, "activate": True}, Asker(True))
    baserow.fields[7] = baserow_field(108, "Equipe", "single_select", options=("Só eu",))

    asker = Asker(True, True)

    output = live.compose(
        "connect_baserow_integration",
        {**CONNECT, "sources": {**mapping, "101": "lead.name"}},
        asker,
    )

    assert not output.is_error and "COM PENDÊNCIAS" in output.text
    assert "A integração ESTAVA ativa e foi DESATIVADA" in output.text
    assert asker.messages[0].startswith(
        "Pedido de `connect_baserow_integration`: trocar o mapeamento"
    )
    assert stored_integration(deps).active is False


def test_a_table_that_cannot_be_read_right_after_connecting_keeps_the_connection_and_says_so(
    deps: Deps, live: Live, baserow: FakeBaserow
) -> None:
    reads = []

    def fail_the_second_read(request: httpx.Request) -> None:
        reads.append(request)
        if (
            len(reads) == 2
        ):  # a primeira é a da conexão; a segunda, a da leitura que sugere o mapeamento
            baserow.fail("GET", 500, where="fields")

    baserow.before = fail_the_second_read

    output = live.compose("connect_baserow_integration", CONNECT, Asker(True))

    assert not output.is_error and "Conectado à tabela 7" in output.text
    assert "NÃO guardei o mapeamento: não consegui ler a tabela do Baserow agora" in output.text
    assert "a conexão já está gravada" in output.text
    assert "o nome dos campos da tabela não sugere" not in output.text
    saved = stored_integration(deps)
    assert saved is not None and saved.entries == () and saved.active is False


NEW_MAPPING = {"101": "lead.name", "104": "job.public_id"}
THE_SIX = (
    "Nome (#101) <- lead.name",
    "E-mail (#102) <- lead.email",
    "Telefone (#103) <- lead.phone",
    "Código do pedido (#104) <- job.public_id",
    "Respostas (#105) <- job.answers_json",
    "PDF (#106) <- job.pdf_url",
)
NO_TOKEN = {"slug": SLUG, "base_url": BASEROW_URL, "table_id": BASEROW_TABLE}
ASKS_FOR_THE_MAPPING = "Pedido de `connect_baserow_integration`: trocar o mapeamento"


def stored_map(deps: Deps) -> dict[str, str]:
    return {str(e.field_id): e.source for e in stored_integration(deps).entries}


@pytest.fixture
def activated(live: Live) -> Live:
    """A integração conectada (os seis campos que o nome sugere) e ativada."""
    live.compose("connect_baserow_integration", CONNECT, Asker(True))
    live.compose("connect_baserow_integration", {"slug": SLUG, "activate": True}, Asker(True))
    return live


def operations_since(live: Live, count: int) -> list[str]:
    return [operation_of(r) for r in live.sent()[count:]]


def test_the_activation_question_lists_every_mapped_field_with_its_source(
    deps: Deps, live: Live
) -> None:
    live.compose("connect_baserow_integration", CONNECT, Asker(True))
    asker = Asker(True)

    live.compose("connect_baserow_integration", {"slug": SLUG, "activate": True}, asker)

    (question,) = asker.messages
    assert "estes campos passam a ser preenchidos:" in question
    for line in THE_SIX:
        assert f"- {line}" in question
    assert "baserow.example.test" in question and "tabela 7" in question


def test_without_a_way_to_ask_the_connect_and_activate_texts_carry_what_the_question_would(
    deps: Deps, live: Live
) -> None:
    connect = live.compose("connect_baserow_integration", CONNECT)
    live.compose("connect_baserow_integration", {**CONNECT, "confirm": True})
    activate = live.compose("connect_baserow_integration", {"slug": SLUG, "activate": True})

    assert connect.awaiting and activate.awaiting
    for shown in (connect, activate):
        # O que a pessoa confirma vem antes da descrição do efeito que o servidor devolve.
        assert shown.text.index("baserow.example.test") < shown.text.index("CONFIRMAÇÃO NECESSÁRIA")
    assert (
        "conectar «demo-definicao» ao servidor https://baserow.example.test, tabela 7"
        in connect.text
    )
    assert "Hoje o formulário não tem integração." in connect.text
    assert BASEROW_TOKEN not in connect.text
    for line in THE_SIX:
        assert f"- {line}" in activate.text
    assert activate.text.index(THE_SIX[0]) < activate.text.index("CONFIRMAÇÃO NECESSÁRIA")


def test_the_save_of_the_mapping_is_unmarked_at_the_api_and_the_composite_does_not_trust_that(
    live: Live,
) -> None:
    spec = live.tools["save_form_integration_mapping"]

    # Documentado em `composites_baserow.py`, no README e no SECURITY: a gerada troca o mapeamento sem
    # `confirm`; a composta pede a confirmação própria quando a integração está ativa.
    assert not spec.marked and "confirm" not in spec.input_schema["properties"]


def test_sources_added_to_the_replay_of_a_connect_are_not_covered_by_its_confirmation(
    deps: Deps, live: Live
) -> None:
    first = live.compose("connect_baserow_integration", CONNECT)
    assert first.awaiting and "Sem `sources`:" in first.text

    sneaky = live.compose(
        "connect_baserow_integration", {**CONNECT, "sources": NEW_MAPPING, "confirm": True}
    )

    assert sneaky.awaiting and stored_integration(deps) is None  # o `sources` é outra ação
    assert "Em seguida guarda o mapeamento que você mandou (2 campo(s)):" in sneaky.text
    assert "- #101 <- lead.name" in sneaky.text and "- #104 <- job.public_id" in sneaky.text
    shown = live.compose(
        "connect_baserow_integration", {**CONNECT, "sources": NEW_MAPPING, "confirm": True}
    )
    assert not shown.awaiting and stored_map(deps) == NEW_MAPPING  # a que foi mostrada vale


def test_the_connect_question_shows_the_mapping_it_will_save_and_the_one_it_replaces(
    deps: Deps, live: Live
) -> None:
    live.compose("connect_baserow_integration", CONNECT, Asker(True))  # inativa, com os seis
    asker = Asker(False)

    live.compose("connect_baserow_integration", {**CONNECT, "sources": NEW_MAPPING}, asker)

    (question,) = asker.messages
    assert "Em seguida guarda o mapeamento que você mandou (2 campo(s)):" in question
    assert "- Nome (#101) <- lead.name" in question
    assert "Troca o mapeamento de hoje (6 campo(s)):" in question
    for line in THE_SIX:
        assert f"- {line}" in question
    assert len(stored_map(deps)) == 6


def test_replacing_the_mapping_of_an_active_integration_asks_its_own_confirmation(
    deps: Deps, activated: Live
) -> None:
    asker = Asker(True)
    seen = len(activated.sent())

    output = activated.compose(
        "connect_baserow_integration", {**NO_TOKEN, "sources": NEW_MAPPING}, asker
    )

    (question,) = asker.messages
    assert "trocar o mapeamento de «demo-definicao». A integração está ATIVA" in question
    assert "Hoje (6 campo(s)):" in question and "Passa a ser (2 campo(s)):" in question
    assert "- Código do pedido (#104) <- job.public_id" in question
    assert "(`save_form_integration_mapping`, que a API não marca)" in question
    assert operations_since(activated, seen) == [
        "get_form_integration",
        "save_form_integration_mapping",
    ]
    assert "Já conectada a https://baserow.example.test" in output.text
    assert "Mapeamento guardado: 2 campo(s)." in output.text and stored_map(deps) == NEW_MAPPING
    assert stored_integration(deps).active is True


def test_a_no_to_the_mapping_of_an_active_integration_leaves_it_as_it_was(
    deps: Deps, activated: Live
) -> None:
    seen = len(activated.sent())

    output = activated.compose(
        "connect_baserow_integration", {**NO_TOKEN, "sources": NEW_MAPPING}, Asker(False)
    )

    assert output.is_error and "O cliente não confirmou" in output.text
    assert "O mapeamento não foi trocado." in output.text
    assert operations_since(activated, seen) == ["get_form_integration"]
    assert len(stored_map(deps)) == 6 and stored_integration(deps).active is True


def test_the_yes_to_the_mapping_of_an_active_integration_is_not_a_yes_to_the_connection(
    deps: Deps, activated: Live
) -> None:
    asker = Asker(True, False)  # sim ao mapeamento (a primeira pergunta), não à conexão
    seen = len(activated.sent())

    output = activated.compose(
        "connect_baserow_integration", {**CONNECT, "sources": NEW_MAPPING}, asker
    )

    assert len(asker.messages) == 2 and output.is_error
    assert asker.messages[0].startswith(ASKS_FOR_THE_MAPPING)
    assert asker.messages[1].startswith("Pedido de `connect_baserow_integration`: conectar")
    assert (
        "Em seguida guarda" not in asker.messages[1]
    )  # o mapeamento já foi o da primeira pergunta
    assert operations_since(activated, seen) == [
        "get_form_integration",
        "save_form_integration_mapping",
    ]
    assert "Já feito:\n- Mapeamento guardado: 2 campo(s)." in output.text
    assert stored_map(deps) == NEW_MAPPING


def test_without_a_way_to_ask_the_mapping_of_an_active_integration_waits_for_the_shown_call(
    deps: Deps, activated: Live
) -> None:
    arguments = {**CONNECT, "sources": NEW_MAPPING}

    unseen = activated.compose("connect_baserow_integration", {**arguments, "confirm": True})
    assert unseen.awaiting and len(stored_map(deps)) == 6  # o `confirm` antes da hora não vale
    assert "CONFIRMAÇÃO NECESSÁRIA: nada foi executado." in unseen.text
    assert "Hoje (6 campo(s)):" in unseen.text and "Passa a ser (2 campo(s)):" in unseen.text
    assert "chame `connect_baserow_integration` de novo, com os mesmos argumentos" in unseen.text

    other = activated.compose(
        "connect_baserow_integration",
        {**arguments, "sources": {"104": "job.public_id"}, "confirm": True},
    )
    assert other.awaiting and len(stored_map(deps)) == 6  # outro `sources` é outra ação

    saved = activated.compose("connect_baserow_integration", {**arguments, "confirm": True})
    assert saved.awaiting and stored_map(deps) == NEW_MAPPING  # trocou; falta a conexão
    assert "- Mapeamento guardado: 2 campo(s)." in saved.text
    assert "conectar «demo-definicao» ao servidor" in saved.text

    done = activated.compose("connect_baserow_integration", {**arguments, "confirm": True})
    assert not done.awaiting and "Conectado à tabela 7" in done.text
    assert "O mapeamento guardado já é o que você mandou: nada a trocar." in done.text
    assert stored_map(deps) == NEW_MAPPING and stored_integration(deps).active is True


def test_the_mapping_saved_for_an_inactive_integration_needs_no_question_of_its_own(
    deps: Deps, live: Live
) -> None:
    live.compose("connect_baserow_integration", CONNECT, Asker(True))  # inativa
    asker = Asker()

    output = live.compose(
        "connect_baserow_integration", {**NO_TOKEN, "sources": NEW_MAPPING}, asker
    )

    assert asker.messages == [] and not output.is_error  # nada ativo muda: a API não marca
    assert stored_map(deps) == NEW_MAPPING and stored_integration(deps).active is False


def test_connecting_again_to_the_same_table_without_a_token_connects_nothing(
    deps: Deps, activated: Live
) -> None:
    asker = Asker(True)
    seen = len(activated.sent())

    output = activated.compose("connect_baserow_integration", NO_TOKEN, asker)

    assert asker.messages == [] and not output.is_error
    assert operations_since(activated, seen) == ["get_form_integration"]
    assert "Já conectada a https://baserow.example.test (tabela 7)" in output.text
    assert "Mapeamento mantido (6 campo(s))" in output.text
    assert "A integração está ATIVA" in output.text


def test_a_stored_token_that_does_not_open_must_be_sent_again_before_anything_else(
    deps: Deps, activated: Live
) -> None:
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))

    output = activated.compose("connect_baserow_integration", NO_TOKEN, Asker(True))

    assert output.is_error and "mande o `token` de novo" in output.text
    assert "Nada foi feito." in output.text


def test_the_replay_of_a_connect_that_follows_a_stored_mapping_does_not_announce_it_again(
    deps: Deps, activated: Live
) -> None:
    arguments = {**CONNECT, "sources": NEW_MAPPING}
    activated.compose("connect_baserow_integration", {**arguments, "confirm": True})  # pergunta
    activated.compose("connect_baserow_integration", {**arguments, "confirm": True})  # troca
    asker = Asker(False)

    activated.compose("connect_baserow_integration", arguments, asker)

    (question,) = asker.messages
    assert question.startswith("Pedido de `connect_baserow_integration`: conectar")
    assert "Em seguida guarda" not in question and "Troca o mapeamento" not in question


@pytest.mark.parametrize(
    "base_url",
    [
        "https://" + "a" * 254 + ".com",
        "baserow.example.test",
        "https://[::1",
        "https://example.test:abc",
        "https://exemplo\u202e.com",
        "https://exe mplo.com",
        "https://",
    ],
)
def test_an_address_whose_server_cannot_be_shown_whole_is_refused_before_anything_is_sent(
    live: Live, base_url: str
) -> None:
    asker = Asker(True)

    output = live.compose("connect_baserow_integration", {**CONNECT, "base_url": base_url}, asker)

    assert output.is_error and "não tem um servidor que eu possa mostrar por inteiro" in output.text
    assert "Nada foi feito." in output.text
    assert live.sent() == [] and asker.messages == []


def test_the_question_shows_the_whole_server_and_the_real_one_behind_a_userinfo(
    live: Live,
) -> None:
    long_host = ".".join(["a" * 60] * 4)  # 243 caracteres: cabe no limite, e passa do corte antigo
    tricky = "https://bom.example.test@mau.example.test"
    asker = Asker(False, False)

    live.compose(
        "connect_baserow_integration", {**CONNECT, "base_url": f"https://{long_host}"}, asker
    )
    live.compose("connect_baserow_integration", {**CONNECT, "base_url": tricky}, asker)

    long_question, tricky_question = asker.messages
    assert f"ao servidor https://{long_host}," in long_question and "…" not in long_question
    assert "ao servidor https://mau.example.test," in tricky_question
    assert "bom.example.test" not in tricky_question


def test_an_integration_with_report_pendencies_is_not_activated_and_the_person_is_not_asked(
    deps: Deps, live: Live, baserow: FakeBaserow
) -> None:
    live.compose(
        "connect_baserow_integration",
        {**CONNECT, "sources": {"104": "job.public_id", "108": "answer:tamanho_equipe"}},
        Asker(True),
    )
    baserow.fields[7] = baserow_field(108, "Equipe", "single_select", options=("Só eu",))
    asker = Asker(True)

    output = live.compose("connect_baserow_integration", {"slug": SLUG, "activate": True}, asker)

    assert output.is_error and "COM PENDÊNCIAS" in output.text and "NÃO ativei" in output.text
    assert asker.messages == [] and stored_integration(deps).active is False


def test_a_form_without_a_published_version_cannot_be_connected(deps: Deps, live: Live) -> None:
    drafts.create(deps, title="Em rascunho", slug="em-rascunho", actor="testes")
    asker = Asker(True)

    output = live.compose("connect_baserow_integration", {**CONNECT, "slug": "em-rascunho"}, asker)

    assert output.is_error and "ainda não tem versão publicada" in output.text
    assert asker.messages == [] and [operation_of(r) for r in live.sent()] == [
        "get_form_integration"
    ]


def test_connecting_needs_the_address_the_table_and_a_first_token(deps: Deps, live: Live) -> None:
    no_table = live.compose("connect_baserow_integration", {"slug": SLUG, "base_url": BASEROW_URL})
    no_token = live.compose(
        "connect_baserow_integration", {"slug": SLUG, "base_url": BASEROW_URL, "table_id": 7}
    )
    activate_alone = live.compose("connect_baserow_integration", {"slug": SLUG, "activate": True})

    assert "faltam `table_id`" in no_table.text and no_table.is_error
    assert "Falta o `token`" in no_token.text and no_token.is_error
    assert "não tem integração: conecte primeiro" in activate_alone.text and activate_alone.is_error


# --- as cinco juntas: o que elas chamam e o que elas nunca mandam --------------------------------


def run_everything(deps: Deps, live: Live, *, asker: Callable[[], Asker]) -> list[tuple[str, int]]:
    """Roda as cinco compostas em cenários que tocam todas as operações que elas chamam, e devolve o
    (nome, quantas requisições) de cada uma, na ordem."""
    jobs = build_jobs_world(deps)
    plan: list[tuple[str, dict[str, Any]]] = [
        ("attention_overview", {}),
        ("diagnose_order", {"public_id": jobs.carla.public_id}),
        ("edit_form_draft", {"slug": SLUG, "patch": INTRO}),
        ("publish_form_with_review", {"slug": SLUG, "note": NOTE}),
        ("publish_form_with_review", {"slug": SLUG, "note": NOTE, "publish": True}),
        ("connect_baserow_integration", CONNECT),
        ("connect_baserow_integration", {"slug": SLUG, "activate": True}),
    ]
    seen = []
    for name, arguments in plan:
        before = len(live.sent())
        live.compose(name, arguments, asker())
        made = {operation_of(r) for r in live.sent()[before:]}
        assert made <= CALLS[name], (name, made - CALLS[name])  # só o que declarou
        assert made <= operation_ids(DOCUMENT)  # e é do catálogo
        seen.append((name, len(live.sent()) - before))
    return seen


def test_every_call_the_composites_make_is_a_catalog_operation_they_declared(
    deps: Deps, live: Live
) -> None:
    made = run_everything(deps, live, asker=lambda: Asker(True, True))

    assert [name for name, _ in made] == [
        "attention_overview",
        "diagnose_order",
        "edit_form_draft",
        "publish_form_with_review",
        "publish_form_with_review",
        "connect_baserow_integration",
        "connect_baserow_integration",
    ]
    used = {operation_of(r) for r in live.sent()}
    assert used == set().union(*CALLS.values())  # os cenários tocaram toda operação declarada
    assert used <= operation_ids(DOCUMENT)
    # Nenhuma chamada fora do catálogo: tudo foi para o prefixo da API de admin, e nada para o Baserow.
    assert all(r.url.path.startswith(API_PREFIX) for r in live.sent())


def test_without_the_person_no_composite_ever_sends_a_confirmation(deps: Deps, live: Live) -> None:
    run_everything(deps, live, asker=lambda: Asker())  # o cliente que não sabe perguntar

    assert not any(confirmed(r) for r in live.sent())
    # As ações marcadas não aconteceram: a publicação, a conexão e a ativação ficaram para o sim.
    assert audited(deps, "form.publish") == []
    assert by_key(deps, "integration.create") == [] and by_key(deps, "integration.activate") == []
    assert {operation_of(r) for r in live.sent()}.isdisjoint(
        {"reprocess_job", "resend_job_delivery", "resend_job_sync", "download_job_pdf"}
    )


def test_a_person_who_says_no_to_everything_leaves_the_world_as_it_was(
    deps: Deps, live: Live
) -> None:
    run_everything(deps, live, asker=lambda: Asker(False, False, False))

    assert not any(confirmed(r) for r in live.sent())
    assert audited(deps, "form.publish") == [] and by_key(deps, "integration.create") == []


def test_with_the_lead_data_off_no_composite_asks_for_it_or_downloads_the_pdf(
    deps: Deps, live: Live
) -> None:
    run_everything(deps, live, asker=lambda: Asker(True, True))

    assert "download_job_pdf" not in live.tools
    for request in live.sent():
        assert request.url.params.get("include_lead_data", "false") == "false"
        assert "q" not in request.url.params
        assert operation_of(request) != "download_job_pdf"


# =================================================================================================
# a API de mentira: as falhas que o app não faz, e o que cada composta responde a elas
# =================================================================================================


def scripted(tmp_path: Path, role: str = "total") -> World:
    return World(tmp_path, role)


def jobs_page(*items: dict[str, Any], total: int | None = None) -> httpx.Response:
    return answer(200, {"items": list(items), "next_cursor": None, "total": total})


def form_item(
    slug: str, *, active: bool = True, version: int | None = 1, **more: Any
) -> dict[str, Any]:
    return {
        "slug": slug,
        "title": slug,
        "active": active,
        "published_version": version,
        "draft_pending": False,
        "version_compiles": True,
        "data_notices": [],
        "submissions": 0,
        **more,
    }


def review_body(**changes: Any) -> dict[str, Any]:
    return {
        "slug": SLUG,
        "number": 2,
        "current_version": 1,
        "first_publication": False,
        "nothing_to_publish": False,
        "version": "v-do-rascunho",
        "changes": [{"key": "pagina", "title": "Página pública", "changed": True, "lines": []}],
        "test": {"status": "not_needed", "llm_changes": [], "tested_at": None},
        "contact": [],
        "consent_version": None,
        "dropped_values": [],
        "stale_assets": [],
        "missing": [],
        "problems": [],
        **changes,
    }


def simulation_body(**review: Any) -> dict[str, Any]:
    inner = review_body(**review)
    return {
        "dry_run": True,
        "published": False,
        "ready": not inner["missing"] and not inner["problems"],
        "review": inner,
        "published_version": None,
        "draft": {"pending": True, "version": inner["version"]},
    }


def test_every_section_of_the_overview_survives_the_failure_of_the_others(tmp_path: Path) -> None:
    world = scripted(tmp_path)
    world.api.when("GET", "/jobs", error(422, "validation_failed", "O corpo não vale."))
    world.api.when("GET", "/destinations", error(500, "internal_error", "Falhou."))
    world.api.when(
        "GET",
        "/forms",
        answer(200, {"items": [form_item("a", draft_pending=True)], "next_cursor": None}),
    )
    world.api.when(
        "GET", "/forms/{slug}/integrations/baserow", error(404, "not_found", "Não existe.")
    )

    output = world.compose("attention_overview")

    assert not output.is_error  # a lista de formulários veio
    assert "Rascunhos ainda não publicados: 1" in output.text
    assert "NÃO CONSEGUI LER (a visão geral está incompleta):" in output.text
    assert "- pedidos que falharam: A API recusou (422 validation_failed)" in output.text
    assert (
        "- entregas ao webhook que falharam: A API recusou (422 validation_failed)" in output.text
    )
    assert "- destinos: A API recusou (500 internal_error)" in output.text
    assert "- integração de «a»: A API recusou (404 not_found)" in output.text
    world.close()


def test_the_overview_asks_the_api_for_the_failed_deliveries_and_counts_them_by_the_total(
    tmp_path: Path,
) -> None:
    world = scripted(tmp_path)
    delivery_item = {
        "public_id": "pedido-1",
        "form_slug": "a",
        "created_at": "2027-01-15T10:00:00Z",
        "deliveries": [{"destination": "webhook", "status": "failed"}],
    }

    def jobs(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("delivery") == "failed":
            return jobs_page(delivery_item, total=12)
        return jobs_page()  # nenhum pedido falhou

    world.api.when("GET", "/jobs", jobs)
    world.api.when(
        "GET",
        "/destinations",
        answer(200, {"items": [], "next_cursor": None, "unserved": [], "unreadable": []}),
    )
    world.api.when("GET", "/forms", answer(200, {"items": [], "next_cursor": None}))

    output = world.compose("attention_overview", {"limit": 3})

    assert "Entregas ao webhook que falharam: 12 pedido(s)" in output.text  # o total da API
    assert "- `pedido-1` · a · 1 entrega(s) com falha · 2027-01-15T10:00:00Z" in output.text
    assert "nenhum pedido falhou" in output.text  # a outra seção, lida à parte
    asked = [dict(r.url.params) for r in world.api.calls("GET", "/jobs")]
    assert {"delivery": "failed", "limit": "3", "include_total": "true", **NO_LEAD_DATA} in asked
    world.close()


def test_an_overview_that_read_nothing_is_a_failure(tmp_path: Path) -> None:
    world = scripted(tmp_path)
    for path in ("/jobs", "/destinations", "/forms"):
        world.api.when(
            "GET",
            path,
            error(
                429,
                "rate_limited",
                "Devagar.",
            ),
        )

    output = world.compose("attention_overview")

    assert output.is_error and output.text.startswith(
        "A visão geral falhou: não consegui ler nada."
    )
    assert output.text.count("429 rate_limited") == 4  # as quatro seções que dependiam das leituras
    world.close()


def test_the_overview_reads_a_few_integrations_per_call_and_names_the_forms_left_out(
    tmp_path: Path,
) -> None:
    world = scripted(tmp_path)
    slugs = [f"form-{n}" for n in range(7)]
    world.api.when("GET", "/jobs", jobs_page())
    world.api.when(
        "GET",
        "/destinations",
        answer(200, {"items": [], "next_cursor": None, "unserved": [], "unreadable": []}),
    )
    world.api.when(
        "GET", "/forms", answer(200, {"items": [form_item(s) for s in slugs], "next_cursor": None})
    )
    world.api.when(
        "GET",
        "/forms/{slug}/integrations/baserow",
        answer(200, {"connected": False, "connection": None}),
    )

    default = world.compose("attention_overview")
    chosen = world.compose("attention_overview", {"integration_slugs": ["form-6", "form-5"]})
    skipped = world.compose("attention_overview", {"integrations": False})

    reads = [r.url.path for r in world.api.calls("GET") if "integrations" in r.url.path]
    assert [p.split("/")[-3] for p in reads[:4]] == slugs[
        :4
    ]  # os quatro primeiros, o que cabe no balde
    assert "integração de «form-4», «form-5», «form-6» (leio 4 por chamada" in default.text
    assert [p.split("/")[-3] for p in reads[4:]] == ["form-6", "form-5"]
    assert "integração de «form-0»" in chosen.text
    assert len(reads) == 6 and "Não conferi, a seu pedido" in skipped.text
    world.close()


def test_an_overview_with_an_unread_section_does_not_say_everything_is_fine(tmp_path: Path) -> None:
    world = scripted(tmp_path)
    world.api.when("GET", "/jobs", error(500, "internal_error", "Falhou."))
    world.api.when(
        "GET",
        "/destinations",
        answer(200, {"items": [], "next_cursor": None, "unserved": [], "unreadable": []}),
    )
    world.api.when("GET", "/forms", answer(200, {"items": [], "next_cursor": None}))

    output = world.compose("attention_overview")

    assert not output.is_error
    assert output.text.startswith("Nada pede atenção no que consegui ler.")
    assert "Nada pede atenção agora." not in output.text
    assert "NÃO CONSEGUI LER" in output.text and "pedidos que falharam" in output.text
    world.close()


def test_the_overview_stops_asking_the_baserow_when_the_usage_limit_runs_out(
    tmp_path: Path,
) -> None:
    world = scripted(tmp_path)
    world.api.when("GET", "/jobs", jobs_page())
    world.api.when(
        "GET",
        "/destinations",
        answer(200, {"items": [], "next_cursor": None, "unserved": [], "unreadable": []}),
    )
    world.api.when(
        "GET",
        "/forms",
        answer(
            200, {"items": [form_item("a"), form_item("b"), form_item("c")], "next_cursor": None}
        ),
    )
    world.api.when(
        "GET", "/forms/{slug}/integrations/baserow", error(429, "rate_limited", "Devagar.")
    )

    output = world.compose("attention_overview")

    assert len([r for r in world.api.calls("GET") if "integrations" in r.url.path]) == 1
    assert "integração de «a»: A API recusou (429 rate_limited)" in output.text
    world.close()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "not_found"),
        (409, "state_conflict"),
        (422, "validation_failed"),
        (429, "rate_limited"),
        (500, "internal_error"),
    ],
)
def test_the_two_readers_say_every_refusal_of_the_api_and_never_pretend_to_be_up_to_date(
    tmp_path: Path, status: int, code: str
) -> None:
    world = scripted(tmp_path)
    world.api.when("GET", "/jobs/{public_id}", error(status, code, "A API recusou isto."))
    world.api.when("GET", "/jobs", error(status, code, "A API recusou isto."))
    world.api.when("GET", "/destinations", error(status, code, "A API recusou isto."))
    world.api.when("GET", "/forms", error(status, code, "A API recusou isto."))

    diagnosis = world.compose("diagnose_order", {"public_id": "x"})
    overview = world.compose("attention_overview")

    assert diagnosis.is_error and diagnosis.status == status
    assert f"A API recusou ({status} {code}): A API recusou isto." in diagnosis.text
    assert overview.is_error and overview.text.startswith("A visão geral falhou")  # nada foi lido
    assert "Nada pede atenção agora." not in overview.text
    assert overview.text.count(f"A API recusou ({status} {code})") == 4
    assert "NÃO CONSEGUI LER (a visão geral está incompleta):" in overview.text
    world.close()


def test_the_diagnosis_passes_the_api_refusal_through_with_its_status(tmp_path: Path) -> None:
    world = scripted(tmp_path)
    world.api.when("GET", "/jobs/{public_id}", error(404, "not_found", "O pedido não existe."))

    output = world.compose("diagnose_order", {"public_id": "x"})

    assert output.is_error and output.status == 404 and "404 not_found" in output.text
    world.close()


def test_a_422_with_reasons_and_errors_on_the_final_publication_is_shown_with_both_lists(
    tmp_path: Path,
) -> None:
    world = scripted(tmp_path)
    world.api.when("GET", "/forms/{slug}/draft/review", answer(200, review_body()))
    world.api.when(
        "POST",
        "/forms/{slug}/draft/publish",
        lambda request: (
            answer(200, simulation_body())
            if json.loads(request.content).get("dry_run")
            else error(
                422,
                "validation_failed",
                "A publicação não aconteceu.",
                reasons=["Falta o teste do que vai ao LLM."],
                errors=[{"field": "note", "message": "Escreva uma nota do que mudou."}],
            )
        ),
    )

    output = world.compose(
        "publish_form_with_review", {"slug": SLUG, "note": NOTE, "publish": True}, Asker(True)
    )

    assert output.is_error and output.status == 422
    assert "Parei em «publicar»." in output.text
    assert "A API recusou (422 validation_failed): A publicação não aconteceu." in output.text
    assert "- note: Escreva uma nota do que mudou." in output.text
    assert "O que falta para publicar:\n- Falta o teste do que vai ao LLM." in output.text
    assert "Nada foi publicado." in output.text
    world.close()


def test_a_409_on_the_simulation_says_nothing_was_written(tmp_path: Path) -> None:
    world = scripted(tmp_path)
    world.api.when("GET", "/forms/{slug}/draft/review", answer(200, review_body()))
    world.api.when(
        "POST",
        "/forms/{slug}/draft/publish",
        error(409, "version_conflict", "O rascunho mudou.", current_version="v-novo"),
    )

    output = world.compose("publish_form_with_review", {"slug": SLUG, "note": NOTE})

    assert output.is_error and "409 version_conflict" in output.text
    assert (
        "A versão de agora é v-novo" in output.text and "A simulação não grava nada." in output.text
    )
    world.close()


def test_a_write_that_times_out_is_not_called_unwritten(tmp_path: Path) -> None:
    """Uma falha de rede numa escrita não prova que nada foi gravado: o texto manda conferir."""
    world = scripted(tmp_path)
    world.api.when("GET", "/forms/{slug}/draft/review", answer(200, review_body()))

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("dry_run"):
            return answer(200, simulation_body())
        raise httpx.ReadTimeout("devagar", request=request)

    world.api.when("POST", "/forms/{slug}/draft/publish", handler)

    output = world.compose(
        "publish_form_with_review", {"slug": SLUG, "note": NOTE, "publish": True}, Asker(True)
    )

    assert output.is_error and "PODE ter sido executada" in output.text
    assert "Nada foi publicado." not in output.text
    assert "Não sei se este passo chegou a gravar" in output.text
    world.close()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (500, "internal_error"),
        (501, "internal_error"),
        (502, "bad_gateway"),  # só o `upstream_failed` do Baserow o contrato diz que não grava
        (503, "unavailable"),
        (504, "timeout"),
        (599, "internal_error"),
    ],
)
def test_a_5xx_on_a_write_is_never_reported_as_nothing_published_or_nothing_saved(
    tmp_path: Path, status: int, code: str
) -> None:
    world = scripted(tmp_path)
    world.api.when("GET", "/forms/{slug}/draft/review", answer(200, review_body()))

    def publish(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("dry_run"):
            return answer(200, simulation_body())
        return error(status, code, "O servidor falhou no meio.")

    world.api.when("POST", "/forms/{slug}/draft/publish", publish)
    world.api.when(
        "GET",
        "/forms/{slug}/integrations/baserow",
        answer(200, {"published": True, "connected": False, "connection": None, "mapping": []}),
    )
    world.api.when(
        "PUT",
        "/forms/{slug}/integrations/baserow/connection",
        error(status, code, "O servidor falhou no meio."),
    )

    publishing = world.compose(
        "publish_form_with_review", {"slug": SLUG, "note": NOTE, "publish": True}, Asker(True)
    )
    connecting = world.compose("connect_baserow_integration", CONNECT, Asker(True))

    for output, nothing in ((publishing, "Nada foi publicado."), (connecting, "Nada foi gravado.")):
        assert output.is_error and output.status == status
        assert nothing not in output.text
        assert "Não sei se este passo chegou a gravar" in output.text
        assert "confira o estado com a ferramenta de leitura antes de repetir" in output.text
    world.close()


@pytest.mark.parametrize(
    ("output", "nothing_written"),
    [
        (ToolOutput("x", True, status=400), True),
        (ToolOutput("x", True, status=404), True),
        (ToolOutput("x", True, status=409), True),
        (ToolOutput("x", True, status=422), True),
        (ToolOutput("x", True, status=429), True),
        (ToolOutput("x", True, status=499), True),
        (ToolOutput("x", True, status=502, data={"code": "upstream_failed"}), True),
        (ToolOutput("Recusa do cliente", True, confirmation=ConfirmationOutcome.DECLINED), True),
        (ToolOutput("x", True, status=500), False),
        (ToolOutput("x", True, status=500, data={"code": "upstream_failed"}), False),
        (ToolOutput("x", True, status=502, data={"code": "bad_gateway"}), False),
        (ToolOutput("x", True, status=502), False),
        (ToolOutput("x", True, status=503), False),
        (ToolOutput("x", True, status=599), False),
        (ToolOutput("A API não confirmou a operação", True), False),  # sem status: falha de rede
    ],
)
def test_only_a_4xx_the_baserow_502_and_the_no_of_the_person_prove_that_nothing_was_written(
    output: ToolOutput, nothing_written: bool
) -> None:
    assert untouched(output) is nothing_written


def test_the_publication_lists_the_blocking_reasons_and_the_problems_of_the_sample_pdf(
    tmp_path: Path,
) -> None:
    world = scripted(tmp_path)
    world.api.when("GET", "/forms/{slug}/draft/review", answer(200, review_body()))
    broken = simulation_body(
        missing=["Escreva uma nota do que mudou: ela fica no histórico, com a versão."],
        problems=[{"field": "pdf.html", "message": "O PDF de exemplo não saiu.", "line": 3}],
    )
    world.api.when("POST", "/forms/{slug}/draft/publish", answer(200, broken))

    output = world.compose("publish_form_with_review", {"slug": SLUG, "publish": True}, Asker(True))

    assert output.is_error and "FALTA: Escreva uma nota do que mudou" in output.text
    assert "pdf.html: O PDF de exemplo não saiu." in output.text
    assert len(world.api.calls("POST")) == 1  # só a simulação
    world.close()


def test_the_comparison_that_fails_after_the_patch_does_not_hide_that_the_draft_was_saved(
    tmp_path: Path,
) -> None:
    world = scripted(tmp_path)
    document = {"version": "v1", "current_version": 3, "pending": True, "document": {}}
    world.api.when("GET", "/forms/{slug}/draft", answer(200, document))
    world.api.when("PATCH", "/forms/{slug}/draft", answer(200, {**document, "version": "v2"}))
    world.api.when(
        "GET", "/forms/{slug}/comparison", error(404, "not_found", "Um dos lados não existe.")
    )

    output = world.compose("edit_form_draft", {"slug": SLUG, "patch": INTRO})

    assert not output.is_error  # o patch foi gravado: repetir seria o erro
    assert "Rascunho de «demo-definicao» gravado. NADA foi publicado." in output.text
    assert (
        "A comparação com a versão 3 FALHOU, mas o rascunho foi gravado (não repita o patch)"
        in output.text
    )
    patch = world.api.calls("PATCH")[0]
    assert json.loads(patch.content) == {"expected_version": "v1", "patch": INTRO}
    world.close()


def test_a_patch_uses_the_version_the_caller_saw_and_not_the_one_it_just_read(
    tmp_path: Path,
) -> None:
    world = scripted(tmp_path)
    document = {"version": "v-de-agora", "current_version": None, "pending": True, "document": {}}
    world.api.when("GET", "/forms/{slug}/draft", answer(200, document))
    world.api.when(
        "PATCH",
        "/forms/{slug}/draft",
        error(409, "version_conflict", "O rascunho mudou.", current_version="v-de-agora"),
    )

    output = world.compose(
        "edit_form_draft", {"slug": SLUG, "patch": INTRO, "expected_version": "v-que-vi"}
    )

    assert json.loads(world.api.calls("PATCH")[0].content)["expected_version"] == "v-que-vi"
    assert output.is_error and "A versão de agora é v-de-agora" in output.text
    world.close()


def test_a_mapping_that_fails_with_the_baserow_down_keeps_the_connection_and_says_so(
    tmp_path: Path,
) -> None:
    world = scripted(tmp_path)
    state = {
        "published": True,
        "connected": False,
        "connection": None,
        "mapping": [],
        "fields": None,
        "report": None,
    }
    connected = {"version": "1", "table_id": 7, "host": "baserow.example.test", "active": False}
    reads = iter(
        [
            state,
            {
                **state,
                "connected": True,
                "connection": connected,
                "fields": [{"id": 104, "suggested_source": "job.public_id", "unsupported": None}],
            },
        ]
    )
    world.api.when(
        "GET", "/forms/{slug}/integrations/baserow", lambda request: answer(200, next(reads))
    )
    world.api.when("PUT", "/forms/{slug}/integrations/baserow/connection", answer(200, connected))
    world.api.when(
        "PUT",
        "/forms/{slug}/integrations/baserow/mapping",
        error(502, "upstream_failed", "O Baserow não leu a tabela (HTTP 500)."),
    )

    output = world.compose("connect_baserow_integration", CONNECT, Asker(True))

    assert output.is_error and output.status == 502 and "502 upstream_failed" in output.text
    assert "Já feito:\n- Conectado à tabela 7 em baserow.example.test" in output.text
    assert "Não feito:\n- guardar o mapeamento\n- mostrar o relatório" in output.text
    # O `502 upstream_failed` é o que o contrato diz que não grava; um 500 qualquer não diria.
    assert "O mapeamento não foi gravado. A conexão foi." in output.text
    assert BASEROW_TOKEN not in output.text
    world.close()


def test_the_activation_refused_by_the_api_with_pendencies_is_a_422_that_changes_nothing(
    tmp_path: Path,
) -> None:
    world = scripted(tmp_path)
    clean = {"ok": True, "fields": [{"field_id": 104}], "problems": [], "unmatched_options": 0}
    state = {
        "published": True,
        "connected": True,
        "connection": {
            "version": "3",
            "table_id": 7,
            "host": "baserow.example.test",
            "active": False,
        },
        "report": clean,
    }
    world.api.when("GET", "/forms/{slug}/integrations/baserow", answer(200, state))
    world.api.when(
        "POST",
        "/forms/{slug}/integrations/baserow/activate",
        error(
            422,
            "validation_failed",
            "A integração tem pendências.",
            errors=[{"field": "mapping", "message": "O campo «Equipe» sumiu da tabela."}],
        ),
    )

    output = world.compose(
        "connect_baserow_integration", {"slug": SLUG, "activate": True}, Asker(True)
    )

    assert output.is_error and "Parei em «ativar a integração»." in output.text
    assert (
        "- mapping: O campo «Equipe» sumiu da tabela." in output.text
        and "Nada foi ativado." in output.text
    )
    body = json.loads(world.api.calls("POST")[0].content)
    assert body == {"expected_version": "3", "confirm": True}
    world.close()


def test_a_composite_never_raises_even_when_the_api_answers_something_unexpected(
    tmp_path: Path,
) -> None:
    world = scripted(tmp_path)
    world.api.when("GET", "/jobs/{public_id}", answer(200, ["isto", "não", "é", "um", "pedido"]))
    world.api.when("GET", "/forms/{slug}/draft/review", answer(200, {"version": None}))
    world.api.when("POST", "/forms/{slug}/draft/publish", answer(200, {}))

    odd = world.compose("diagnose_order", {"public_id": "x"})
    review = world.compose("publish_form_with_review", {"slug": SLUG})

    assert not odd.is_error and odd.text.startswith("Pedido `None`")
    assert not review.is_error and "Publicar criaria a versão None" in review.text
    world.close()


def test_a_bug_in_a_composite_comes_back_as_the_type_of_the_error_and_never_its_text(
    tmp_path: Path,
) -> None:
    world = scripted(tmp_path)
    secret = world.key

    async def boom(steps: Steps, arguments: dict[str, Any]) -> Any:
        raise ValueError(f"o texto da exceção, com {secret}")

    broken = dataclasses.replace(world.composites["diagnose_order"], run=boom)

    output = asyncio.run(broken.invoke(world.runner, world.tools, {"public_id": "x"}, Asker()))

    assert output.is_error and "Erro inesperado no servidor MCP (ValueError)" in output.text
    assert "o texto da exceção" not in output.text and secret not in output.text
    world.close()


def test_the_key_is_taken_out_of_everything_a_composite_returns(tmp_path: Path) -> None:
    world = scripted(tmp_path)
    leaked = f"a API repetiu a chave {world.key} no texto do erro"
    world.api.when("GET", "/forms/{slug}/draft", error(422, "validation_failed", leaked))

    output = world.compose("edit_form_draft", {"slug": SLUG, "patch": INTRO})

    assert output.is_error and world.key not in output.text and "[chave]" in output.text
    world.close()


def test_the_argument_checks_name_what_is_wrong_before_any_call(tmp_path: Path) -> None:
    world = scripted(tmp_path)

    unknown = world.compose("diagnose_order", {"public_id": "x", "quem": "eu"})
    missing = world.compose("edit_form_draft", {"slug": SLUG})
    odd_limit = world.compose("attention_overview", {"limit": 0})
    odd_flag = world.compose("publish_form_with_review", {"slug": SLUG, "publish": "sim"})

    assert unknown.text == "Argumentos que esta ferramenta não tem: `quem`."
    assert missing.text == "Faltam argumentos obrigatórios: `patch`."
    assert "`limit` é um número inteiro de 1 a 50" in odd_limit.text
    assert "`publish` é `true` ou `false`" in odd_flag.text
    assert all(o.is_error for o in (unknown, missing, odd_limit, odd_flag))
    assert world.api.calls() == []
    world.close()


# =================================================================================================
# pelo protocolo: o servidor MCP de verdade, em memória
# =================================================================================================


def protocol(world: World, scenario: Callable[[Any], Any], *, elicit: Any = None) -> Any:
    memory = optional("mcp.shared.memory")
    server_module = optional("app.admin_mcp.server")
    server = server_module.build_server(
        world.runner, world.tools.values(), world.composites.values()
    )

    async def main() -> Any:
        async with memory.create_connected_server_and_client_session(
            server, elicitation_callback=elicit
        ) as session:
            return await scenario(session)

    return asyncio.run(main())


def test_the_server_lists_the_composites_beside_the_generated_tools(tmp_path: Path) -> None:
    world = World(tmp_path)
    reader = World(folder(tmp_path, "leitura"), "leitura")

    everything = protocol(world, lambda session: session.list_tools()).tools
    read_only = protocol(reader, lambda session: session.list_tools()).tools

    names = {tool.name for tool in everything}
    assert len(everything) == 91 and set(CALLS) <= names
    assert names - set(CALLS) == operation_ids(DOCUMENT) - {
        "download_job_pdf",
        "get_job_plan_projection",
        "render_job_participant_preview",
    }  # as geradas seguem lá
    assert len(read_only) == 43 and {t.name for t in read_only} >= READS
    assert not (
        {"edit_form_draft", "publish_form_with_review", "connect_baserow_integration"}
        & {t.name for t in read_only}
    )
    by_name = {tool.name: tool for tool in everything}
    for name, composite in world.composites.items():
        tool = by_name[name]
        assert tool.description == composite.description and tool.title == composite.summary
        assert tool.inputSchema == composite.input_schema
        assert tool.annotations.readOnlyHint is composite.annotations["readOnlyHint"]
        assert tool.annotations.destructiveHint is composite.annotations["destructiveHint"]
        assert tool.annotations.openWorldHint is composite.annotations["openWorldHint"]
    world.close()
    reader.close()


@pytest.fixture
def mcp_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """O `fileConfig` do alembic (os testes de migration) desliga os loggers que já existem."""
    monkeypatch.setattr(logging.getLogger("gerador_mcp"), "disabled", False)


def clashing(world: World, name: str = "diagnose_order") -> dict[str, Any]:
    """As ferramentas da chave mais uma operação que a API (mais nova que o MCP) ganhou com o nome
    de uma composta: é o `get_job` com outro nome, e é ela que vale."""
    return {**world.tools, name: dataclasses.replace(world.tools["get_job"], name=name)}


def test_a_composite_named_like_a_live_generated_operation_is_skipped_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, mcp_log: None
) -> None:
    world = World(tmp_path)

    with caplog.at_level(logging.WARNING, logger="gerador_mcp"):
        found = {c.name for c in for_tools(clashing(world))}

    assert found == set(CALLS) - {"diagnose_order"}  # as outras quatro seguem
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "Composta 'diagnose_order' pulada" in warnings[0].getMessage()
    assert "a gerada vale" in warnings[0].getMessage()
    world.close()


def test_the_clash_warning_shows_a_hostile_name_with_repr_and_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, mcp_log: None
) -> None:
    world = World(tmp_path)
    composite = dataclasses.replace(world.composites["diagnose_order"], name="a\nINFO falso")
    tools = {**world.tools, "a\nINFO falso": world.tools["get_job"]}

    with caplog.at_level(logging.WARNING, logger="gerador_mcp"):
        kept = without_clashes(tools, [composite])

    assert kept == [] and "'a\\nINFO falso'" in caplog.text and "\nINFO falso" not in caplog.text
    world.close()


def test_without_a_clash_nothing_is_skipped_and_nothing_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, mcp_log: None
) -> None:
    world = World(tmp_path)

    with caplog.at_level(logging.WARNING, logger="gerador_mcp"):
        found = for_tools(world.tools)

    assert {c.name for c in found} == set(CALLS) and caplog.records == []
    world.close()


def test_the_server_prefers_the_generated_tool_on_a_name_clash_and_does_not_crash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, mcp_log: None
) -> None:
    server_module = optional("app.admin_mcp.server")
    world = World(tmp_path)
    tools = clashing(world)
    world.api.when(
        "GET", "/jobs/{public_id}", answer(200, {"public_id": "abc", "status": "completed"})
    )

    # Mesmo entregando a composta ao servidor, o nome repetido não derruba nem aparece duas vezes.
    server = server_module.build_server(world.runner, tools.values(), world.composites.values())
    memory = optional("mcp.shared.memory")

    async def main() -> tuple[Any, Any]:
        async with memory.create_connected_server_and_client_session(server) as session:
            listed = await session.list_tools()
            called = await session.call_tool("diagnose_order", {"public_id": "abc"})
            return listed, called

    with caplog.at_level(logging.WARNING, logger="gerador_mcp"):
        listed, called = asyncio.run(main())

    names = [tool.name for tool in listed.tools]
    assert names.count("diagnose_order") == 1 and len(names) == len(set(names))
    assert set(CALLS) - {"diagnose_order"} <= set(names)
    generated = next(tool for tool in listed.tools if tool.name == "diagnose_order")
    assert generated.description == tools["diagnose_order"].description  # a gerada, não a composta
    assert json.loads(called.content[0].text) == {"public_id": "abc", "status": "completed"}
    assert "Composta 'diagnose_order' pulada" in caplog.text
    world.close()


def test_the_sdk_validates_a_composite_call_against_its_schema(tmp_path: Path) -> None:
    world = World(tmp_path)

    result = protocol(
        world,
        lambda session: session.call_tool(
            "edit_form_draft", {"slug": SLUG, "patch": INTRO, "confirm": True}
        ),
    )

    assert result.isError and world.api.calls() == []
    world.close()


def test_over_the_protocol_the_person_is_asked_with_what_changes_and_only_the_yes_publishes(
    tmp_path: Path,
) -> None:
    types = optional("mcp.types")
    world = World(tmp_path)
    world.api.when("GET", "/forms/{slug}/draft/review", answer(200, review_body()))
    published = {
        **simulation_body(),
        "dry_run": False,
        "published": True,
        "published_version": {"number": 2, "note": NOTE, "published_by": "chave:teste"},
    }

    def publish(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        return answer(200, simulation_body() if sent.get("dry_run") else published)

    world.api.when("POST", "/forms/{slug}/draft/publish", publish)
    asked: list[Any] = []

    async def elicit(context: Any, params: Any) -> Any:
        asked.append(params)
        return types.ElicitResult(action="accept", content={"confirmar": True})

    result = protocol(
        world,
        lambda session: session.call_tool(
            "publish_form_with_review", {"slug": SLUG, "note": NOTE, "publish": True}
        ),
        elicit=elicit,
    )

    assert not result.isError and "PUBLICADO: «demo-definicao»" in result.content[0].text
    (question,) = asked
    assert (
        "Muda: Página pública" in question.message and "(`publish_form_draft`)" in question.message
    )
    sent = [json.loads(r.content) for r in world.api.calls("POST")]
    assert [bool(s.get("dry_run")) for s in sent] == [True, False]
    assert sent[1]["confirm"] is True and "confirm" not in sent[0]
    world.close()


def test_over_the_protocol_a_client_that_cannot_ask_gets_the_description_and_nothing_is_published(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    world.api.when("GET", "/forms/{slug}/draft/review", answer(200, review_body()))
    world.api.when("POST", "/forms/{slug}/draft/publish", answer(200, simulation_body()))

    result = protocol(
        world,
        lambda session: session.call_tool(
            "publish_form_with_review", {"slug": SLUG, "note": NOTE, "publish": True}
        ),
    )

    assert (
        not result.isError
        and "CONFIRMAÇÃO NECESSÁRIA: nada foi executado." in result.content[0].text
    )
    assert [bool(json.loads(r.content).get("dry_run")) for r in world.api.calls("POST")] == [True]
    world.close()


def test_concurrent_publication_does_not_claim_nothing_was_published(tmp_path: Path) -> None:
    """A composta conserva a incerteza enquanto a primeira publicação ainda espera resposta."""

    async def scenario() -> None:
        world = World(tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()
        publications: list[httpx.Request] = []

        async def reply(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return answer(200, review_body())
            if json.loads(request.content).get("dry_run"):
                return answer(200, simulation_body())
            publications.append(request)
            entered.set()
            await release.wait()
            return answer(200, {"published_version": {"number": 2}})

        client = ApiClient(world.config, transport=httpx.MockTransport(reply))
        runner = ToolRunner(client, world.config)
        composite = world.composites["publish_form_with_review"]
        arguments = {"slug": SLUG, "note": NOTE, "publish": True}
        tasks = []

        async def invoke(given: dict[str, Any]) -> ToolOutput:
            return await composite.invoke(runner, world.tools, given, NeverAsks())

        try:
            assert (await invoke(arguments)).awaiting
            confirmed = {**arguments, "confirm": True}
            tasks.append(asyncio.create_task(invoke(confirmed)))
            await asyncio.wait_for(entered.wait(), 2)
            tasks.append(asyncio.create_task(invoke(confirmed)))
            concurrent = await asyncio.wait_for(asyncio.shield(tasks[1]), 2)
            assert concurrent.is_error and not concurrent.awaiting
            assert "em andamento" in concurrent.text
            assert "Nada foi publicado" not in concurrent.text
            assert "não foi executada" not in concurrent.text
            assert "confira o estado" in concurrent.text
            assert not tasks[0].done() and len(publications) == 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.aclose()
            await world.client.aclose()

    asyncio.run(scenario())


def test_baserow_composite_forwards_explicit_purpose_and_shows_it_before_confirmation(
    deps: Deps, live: Live
) -> None:
    asker = Asker(True)
    output = live.compose("connect_baserow_integration", {**CONNECT, "purpose": "internal"}, asker)
    assert not output.is_error
    assert "Finalidade interna" in asker.messages[0]
    request = next(
        request for request in live.sent() if operation_of(request) == "connect_form_integration"
    )
    assert json.loads(request.content)["purpose"] == "internal"


def test_baserow_composite_never_infers_internal_for_a_new_receiver(live: Live) -> None:
    arguments = {**CONNECT}
    arguments.pop("purpose", None)
    asker = Asker(True)
    output = live.compose("connect_baserow_integration", arguments, asker)
    assert output.is_error and "purpose" in output.text
    assert asker.messages == []
    assert all(operation_of(request) != "connect_form_integration" for request in live.sent())


def test_baserow_composite_refuses_participant_before_any_confirmation_or_request(
    live: Live,
) -> None:
    asker = Asker(True)
    output = live.compose(
        "connect_baserow_integration", {**CONNECT, "purpose": "participant"}, asker
    )
    assert output.is_error and "Participante não é compatível" in output.text
    assert asker.messages == [] and live.sent() == []
