"""O catálogo do MCP de admin (`app.admin_mcp.catalog`), gerado do `docs/api/openapi.json`.

O teste de cobertura do MCP (ADR 0015, "O caminho até o MCP"): o catálogo gerado do arquivo tem
exatamente os `operation_id` dele, cada ferramenta com descrição e as dicas que as marcas dizem, e a
lista de exceções é vazia. Com o teste de cobertura do admin, a cadeia fecha: rota do admin, linha da
matriz, operação do OpenAPI, ferramenta do MCP. Também confere o filtro por papel, o dado da lead
desligado por padrão e o esquema de entrada de cada ferramenta.

Roda sem rede: o documento é o arquivo do repositório. Os valores esperados saem do JSON cru, e não
do gerador.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from admin_mcp_support import openapi_document, operation_ids, optional

from app.admin_mcp import catalog
from app.admin_mcp.catalog import CatalogError, ToolSpec
from app.admin_mcp.config import API_PREFIX

DOCUMENT = openapi_document()
OPERATIONS = {
    operation["operationId"]: (method, path, operation)
    for path, item in DOCUMENT["paths"].items()
    for method, operation in item.items()
    if method in catalog.HTTP_METHODS
}
LOADED = catalog.load(DOCUMENT)
TOOLS = LOADED.tools
LEITURA = {name for name, (_, _, op) in OPERATIONS.items() if op["x-required-role"] == "leitura"}


def names(tools: list[ToolSpec]) -> set[str]:
    return {tool.name for tool in tools}


def selected(role: str, lead_data: bool = False) -> dict[str, ToolSpec]:
    return {t.name: t for t in catalog.select(TOOLS.values(), role=role, lead_data=lead_data)}


# --- a cobertura ---------------------------------------------------------------------------------


def test_the_catalog_has_exactly_the_operation_ids_of_the_file() -> None:
    assert LOADED.skipped == {}
    assert set(TOOLS) == operation_ids(DOCUMENT)
    assert len(TOOLS) == len(OPERATIONS)


def test_the_list_of_exceptions_starts_empty() -> None:
    assert dict(catalog.EXCEPTIONS) == {}


def test_an_exception_drops_the_operation_from_the_catalog_and_needs_a_reason() -> None:
    loaded = catalog.load(DOCUMENT, exceptions={"list_forms": "só um teste do mecanismo"})

    assert set(loaded.tools) == operation_ids(DOCUMENT) - {"list_forms"}
    # A regra do arquivo: cada exceção do mundo real traria um motivo não vazio.
    assert all(reason.strip() for reason in catalog.EXCEPTIONS.values())


def test_every_tool_has_a_description_that_starts_with_the_summary() -> None:
    for name, (_, _, operation) in OPERATIONS.items():
        tool = TOOLS[name]
        assert tool.summary == operation["summary"].strip()
        assert tool.description.startswith(tool.summary), name
        assert operation["description"].strip() in tool.description, name


def test_the_hints_say_what_the_marks_say_for_every_operation() -> None:
    for name, (method, _, operation) in OPERATIONS.items():
        assert TOOLS[name].annotations == {
            "readOnlyHint": (method == "get" or operation["x-required-role"] == "leitura")
            and not operation.get("x-destructive", False)
            and not operation.get("x-external-effect", False),
            "destructiveHint": operation.get("x-destructive", False),
            "openWorldHint": operation.get("x-external-effect", False),
        }, name


@pytest.mark.parametrize(
    ("name", "read_only", "destructive", "open_world"),
    [
        ("list_forms", True, False, False),
        ("get_current_api_key", True, False, False),
        ("render_form_draft_pdf_preview", True, False, False),  # POST, mas só lê
        ("save_form_draft", False, False, False),
        ("revoke_api_key", False, True, False),
        ("remove_asset", False, True, False),
        ("publish_form_draft", False, False, True),
        ("create_api_key", False, False, True),
        ("remove_destination", False, True, True),
        ("rotate_destination_secret", False, True, True),
    ],
)
def test_the_hints_of_known_operations(
    name: str, read_only: bool, destructive: bool, open_world: bool
) -> None:
    assert TOOLS[name].annotations == {
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "openWorldHint": open_world,
    }


def test_the_api_prefix_is_the_server_the_contract_declares() -> None:
    assert DOCUMENT["servers"] == [{"url": API_PREFIX}]


# --- o filtro por papel --------------------------------------------------------------------------


def test_a_leitura_key_registers_only_the_leitura_tools() -> None:
    found = selected("leitura")

    assert set(found) == LEITURA
    assert len(found) == len(LEITURA)
    assert all(tool.role == "leitura" for tool in found.values())
    assert {"publish_form_draft", "upload_asset", "download_job_pdf"}.isdisjoint(found)


def test_a_total_key_registers_all_but_the_lead_pdf_while_the_option_is_off() -> None:
    found = selected("total")

    assert set(found) == operation_ids(DOCUMENT) - {
        "download_job_pdf",
        "get_job_plan_projection",
        "render_job_participant_preview",
    }
    assert len(found) == len(OPERATIONS) - 3


def test_the_lead_data_option_registers_the_lead_pdf_for_a_total_key() -> None:
    found = selected("total", lead_data=True)

    assert set(found) == operation_ids(DOCUMENT)
    assert len(found) == len(OPERATIONS)


def test_the_option_does_nothing_for_a_leitura_key() -> None:
    assert set(selected("leitura", lead_data=True)) == LEITURA
    assert "include_lead_data" not in selected("leitura", lead_data=True)["get_job"].locations


def test_an_unknown_role_is_refused() -> None:
    with pytest.raises(CatalogError):
        catalog.select(TOOLS.values(), role="admin", lead_data=False)


# --- o dado da lead ------------------------------------------------------------------------------


def test_the_catalog_keeps_the_lead_data_operations_whole() -> None:
    assert "include_lead_data" in TOOLS["list_jobs"].input_schema["properties"]
    assert "include_lead_data" in TOOLS["get_job"].input_schema["properties"]
    assert "q" in TOOLS["list_jobs"].input_schema["properties"]
    assert "download_job_pdf" in TOOLS


@pytest.mark.parametrize("name", ["list_jobs", "get_job"])
def test_without_the_option_include_lead_data_is_gone_and_fixed_false(name: str) -> None:
    tool = selected("total")[name]

    assert "include_lead_data" not in tool.input_schema["properties"]
    assert "include_lead_data" not in tool.locations
    assert dict(tool.fixed) == {"include_lead_data": catalog.Fixed("query", False)}
    assert "`GERADOR_MCP_LEAD_DATA`" in tool.description


def test_without_the_option_list_jobs_cannot_search_by_person() -> None:
    """O `q` busca por parte do nome ou do e-mail da lead com a chave `total`: com ele solto, o
    modelo sondaria quem é lead. Sem a opção, o argumento nem existe."""
    tool = selected("total")["list_jobs"]

    assert "q" not in tool.input_schema["properties"]
    assert "q" not in tool.locations
    assert set(tool.input_schema["properties"]) == {
        "status",
        "form",
        "form_version",
        "delivery",
        "sync",
        "created_from",
        "created_to",
        "include_total",
        "limit",
        "cursor",
    }
    assert selected("total", lead_data=True)["list_jobs"].locations["q"] == "query"


def test_for_a_leitura_key_the_probing_argument_is_gone_too() -> None:
    assert "q" not in selected("leitura")["list_jobs"].locations
    assert dict(selected("leitura")["get_job"].fixed) == {
        "include_lead_data": catalog.Fixed("query", False)
    }


def test_with_the_option_the_lead_data_argument_is_offered_and_nothing_is_fixed() -> None:
    tool = selected("total", lead_data=True)["get_job"]

    assert tool.locations["include_lead_data"] == "query"
    assert dict(tool.fixed) == {}


def test_every_lead_data_operation_is_either_flagged_or_withheld_without_the_option() -> None:
    """Uma operação `x-lead-data` nova, com ou sem `include_lead_data`, nunca vaza por omissão: ou a
    ferramenta fixa o argumento em falso, ou ela não é registrada."""
    marked = {name for name, (_, _, op) in OPERATIONS.items() if op.get("x-lead-data")}
    assert marked == {
        "list_jobs",
        "get_job",
        "download_job_pdf",
        "get_job_plan_projection",
        "render_job_participant_preview",
        "list_captures",
        "get_capture",
        "get_submission_journey",
        "get_submission_commercial_context",
        "list_submission_journey_events",
    }

    found = selected("total")
    for name in marked:
        if name in found:
            assert "include_lead_data" not in found[name].locations
            assert found[name].fixed["include_lead_data"].value is False
        else:
            declared = {p["name"] for p in OPERATIONS[name][2].get("parameters", [])}
            assert "include_lead_data" not in declared  # a resposta inteira é da lead


def test_the_probing_arguments_the_server_hides_exist_in_the_contract() -> None:
    """Um `q` renomeado na API deixaria de ser escondido sem ninguém ver: o teste acusa."""
    for name, probes in catalog.LEAD_PROBES.items():
        declared = {p["name"] for p in OPERATIONS[name][2]["parameters"]}
        assert set(probes) <= declared, name


# --- a confirmação -------------------------------------------------------------------------------


def test_the_marked_tools_are_exactly_the_destructive_or_external_operations() -> None:
    expected = {
        name
        for name, (_, _, op) in OPERATIONS.items()
        if op.get("x-destructive") or op.get("x-external-effect")
    }

    assert {name for name, tool in TOOLS.items() if tool.marked} == expected
    assert expected >= {
        "create_event_source",
        "update_event_source",
        "set_event_source_enabled",
        "create_event_credential",
        "rotate_event_credential",
        "revoke_event_credential",
        "erase_submission_journey",
    }


def test_every_marked_tool_offers_confirm_as_an_optional_boolean_and_says_what_it_does() -> None:
    for name, tool in TOOLS.items():
        if tool.marked:
            schema = tool.input_schema
            assert schema["properties"]["confirm"]["type"] == "boolean", name
            assert "confirm" not in schema["required"], name
            assert "confirm: true" in schema["properties"]["confirm"]["description"], name
            assert "confirm" in tool.description, name
        elif not tool.has_file:
            assert "confirm" not in tool.locations, name


def test_a_tool_with_a_file_always_asks_and_its_confirm_stays_in_the_mcp() -> None:
    """O caminho vem do modelo, e a imagem enviada é servida sem login: a pessoa confirma o envio
    mesmo que a API não marque a operação. O `confirm` é só do MCP (`local`)."""
    with_files = {name for name, tool in TOOLS.items() if tool.has_file}

    assert with_files == {"upload_asset", "replace_asset"}
    for name in with_files:
        tool = TOOLS[name]
        assert not tool.marked
        assert tool.locations["confirm"] == "local"
        assert tool.input_schema["properties"]["confirm"]["type"] == "boolean"
        assert "confirm" not in tool.input_schema["required"]
        assert tool.needs_confirmation({"name": "x", "file": "a.png"})
        assert "servida sem login" in " ".join(tool.effects)
        assert "GERADOR_MCP_UPLOAD_DIR" in tool.description
        # Nem as dicas mudam: o que a API não marca, o MCP não finge marcar.
        assert tool.annotations == {
            "readOnlyHint": False,
            "destructiveHint": False,
            "openWorldHint": False,
        }
    # O corpo que a API declara não tem `confirm` nessas duas: o do MCP não é campo dela.
    for name in with_files:
        model = DOCUMENT["components"]["schemas"][
            "UploadAssetRequest" if name == "upload_asset" else "ReplaceAssetRequest"
        ]
        assert "confirm" not in model["properties"]


def test_confirm_goes_in_the_query_of_a_delete_and_in_the_body_of_the_others() -> None:
    for tool in TOOLS.values():
        if tool.marked:
            assert tool.locations["confirm"] == ("query" if tool.method == "DELETE" else "body")


def test_the_dry_run_exemption_comes_from_the_contract() -> None:
    publish = TOOLS["publish_form_draft"]

    assert publish.confirm_unless == "dry_run"
    assert publish.needs_confirmation({"expected_version": "3"})
    assert publish.needs_confirmation({"dry_run": False})
    assert not publish.needs_confirmation({"dry_run": True})
    assert {name for name, tool in TOOLS.items() if tool.confirm_unless} == {
        "publish_form_draft",
        "start_form_draft_test",
    }
    start = TOOLS["start_form_draft_test"]
    assert start.confirm_unless == "replay_only"
    assert not start.needs_confirmation({"replay_only": True})
    assert start.needs_confirmation({"replay_only": False})
    # `dry_run` em outra operação marcada não dispensa nada, e `"true"` não é `true`.
    assert TOOLS["activate_form"].needs_confirmation({"dry_run": True})
    assert publish.needs_confirmation({"dry_run": "true"})


def test_an_unmarked_tool_never_needs_confirmation() -> None:
    assert not TOOLS["save_form_draft"].needs_confirmation({"expected_version": "1"})
    assert not TOOLS["list_forms"].needs_confirmation({})


def test_the_extra_confirmations_are_the_booleans_the_contract_names() -> None:
    """`confirm_contact` e `confirm_http` não vêm no erro `confirmation_required`: o servidor sabe
    qual reenviar pelo esquema da operação."""
    with_companions = {name: tool.companions for name, tool in TOOLS.items() if tool.companions}

    assert with_companions == {
        "import_form": ("confirm_contact",),
        "publish_form_draft": ("confirm_contact",),
        "save_destination": ("confirm_http",),
    }
    assert (
        "seen" in TOOLS["save_form_retention"].input_schema["properties"]
    )  # um valor, não uma caixa


# --- arquivos, respostas e esquemas --------------------------------------------------------------


@pytest.mark.parametrize("name", ["upload_asset", "replace_asset"])
def test_a_file_field_becomes_a_local_path(name: str) -> None:
    tool = TOOLS[name]
    schema = tool.input_schema["properties"]["file"]

    assert tool.body == "multipart"
    assert tool.locations["file"] == "file"
    assert schema["type"] == "string"
    assert "contentMediaType" not in schema
    assert "caminho de uma imagem dentro da pasta `GERADOR_MCP_UPLOAD_DIR`" in schema["description"]
    assert "file" in tool.input_schema["required"]
    assert "só se estiver dentro da pasta" in tool.description


def test_the_json_and_multipart_bodies_are_told_apart() -> None:
    assert {n: t.body for n, t in TOOLS.items() if t.body == "multipart"} == {
        "upload_asset": "multipart",
        "replace_asset": "multipart",
    }
    assert TOOLS["list_forms"].body is None
    assert TOOLS["activate_form"].body == "json"
    assert TOOLS["activate_form"].body_required
    assert TOOLS["render_form_draft_pdf_preview"].body == "json"
    assert not TOOLS["render_form_draft_pdf_preview"].body_required  # o corpo é opcional


def test_the_responses_that_are_not_json_are_the_downloads_and_the_previews() -> None:
    non_json = {name: tool.returns for name, tool in TOOLS.items() if tool.non_json}

    assert non_json == {
        "download_job_pdf": ("application/pdf",),
        "download_form_draft_test_pdf": ("application/pdf",),
        "render_form_draft_pdf_preview": ("application/pdf",),
        "render_form_draft_public_page": ("text/html",),
        "render_form_draft_result_preview": ("text/html",),
        "render_form_draft_completion_preview": ("text/html",),
        "render_job_participant_preview": ("text/html",),
    }
    for name in non_json:
        assert "salva num arquivo" in TOOLS[name].description


def test_the_input_schemas_are_valid_and_self_contained() -> None:
    jsonschema = optional("jsonschema")
    for name, tool in TOOLS.items():
        schema = tool.input_schema
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["type"] == "object" and schema["additionalProperties"] is False, name
        refs = set(_refs(schema))
        assert all(ref.startswith("#/$defs/") for ref in refs), name
        assert {ref.split("/")[-1] for ref in refs} <= set(schema.get("$defs", {})), name


def test_the_form_document_comes_with_its_closure_and_the_recursion_survives() -> None:
    schema = TOOLS["save_form_draft"].input_schema

    assert {"PdfFormDocument", "CaptureFormDocument"} <= schema["$defs"].keys()
    assert len(schema["$defs"]) >= 20  # o fecho do documento (29 hoje)
    assert {"#/$defs/PdfFormDocument", "#/$defs/CaptureFormDocument"} <= set(_refs(schema))
    assert TOOLS["import_form"].input_schema["$defs"].keys() >= {
        "PdfFormDocument",
        "CaptureFormDocument",
    }
    # O plano é recursivo (um nó guarda nós): o `$defs` o descreve sem expandir para sempre.
    assert "#/$defs/PlanNodeDoc" in set(_refs(schema["$defs"]["PlanNodeDoc"]))


def test_the_titles_are_dropped_but_a_property_named_title_stays() -> None:
    document = {
        "paths": {
            "/x": {
                "post": {
                    "operationId": "make_x",
                    "summary": "Fazer x",
                    "x-required-role": "total",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Body"}}
                        },
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
        "components": {
            "schemas": {
                "Body": {
                    "type": "object",
                    "title": "Body",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string", "title": "Title", "default": "x"},
                        "meta": {"default": {"title": "fica"}, "title": "Meta"},
                    },
                }
            }
        },
    }

    tool = catalog.load(document).tools["make_x"]
    properties = tool.input_schema["properties"]

    assert set(properties) == {"title", "meta"}
    assert properties["title"] == {"type": "string", "default": "x"}
    assert properties["meta"] == {"default": {"title": "fica"}}
    assert tool.input_schema["required"] == ["title"]


def test_the_schemas_accept_a_real_call_and_refuse_what_the_contract_refuses() -> None:
    jsonschema = optional("jsonschema")
    validator = jsonschema.Draft202012Validator

    publish = TOOLS["publish_form_draft"].input_schema
    validator(publish).validate({"slug": "demo", "expected_version": "3", "dry_run": True})
    for wrong in (
        {"slug": "demo"},  # falta expected_version
        {"slug": "demo", "expected_version": "3", "extra": 1},  # argumento que não existe
        {"slug": "demo", "expected_version": "3", "dry_run": "sim"},  # não é booleano
        {"slug": "demo", "expected_version": "3", "note": "x" * 301},  # passa do máximo
    ):
        assert not validator(publish).is_valid(wrong), wrong

    jobs = selected("total")["list_jobs"].input_schema
    assert validator(jobs).is_valid({"status": "failed", "limit": 10})
    assert not validator(jobs).is_valid({"include_lead_data": True})
    assert not validator(jobs).is_valid({"q": "maria"})


# --- o que o gerador não sabe montar -------------------------------------------------------------


def _document_with(operation: dict[str, Any], **components: Any) -> dict[str, Any]:
    base = {
        "summary": "Teste",
        "x-required-role": "leitura",
        "responses": {"200": {"description": "ok"}},
    }
    return {
        "paths": {"/t": {"get": {"operationId": "probe", **base, **operation}}},
        "components": {"schemas": components},
    }


@pytest.mark.parametrize(
    ("operation", "fragment"),
    [
        ({"parameters": [{"name": "X", "in": "header", "schema": {}}]}, "header"),
        ({"parameters": [{"$ref": "#/components/parameters/P"}]}, "$ref"),
        ({"x-required-role": "super"}, "x-required-role"),
        (
            {"requestBody": {"content": {"text/plain": {"schema": {}}}}},
            "só JSON e multipart",
        ),
        (
            {"requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}}},
            "modelo com nome",
        ),
        ({"x-destructive": True}, "sem o campo `confirm`"),
        ({"x-confirm-unless": "dry_run"}, "x-confirm-unless"),
    ],
)
def test_an_operation_the_generator_cannot_build_is_reported_with_the_reason(
    operation: dict[str, Any], fragment: str
) -> None:
    loaded = catalog.load(_document_with(operation))

    assert loaded.tools == {}
    assert fragment in loaded.skipped["probe"]


def test_a_skipped_operation_does_not_stop_the_others() -> None:
    document = _document_with({"x-required-role": "super"})
    good = copy.deepcopy(DOCUMENT["paths"]["/me"])
    document["paths"]["/me"] = good

    loaded = catalog.load(document)

    assert set(loaded.tools) == {"get_current_api_key"}
    assert set(loaded.skipped) == {"probe"}


def test_the_same_argument_in_the_path_and_the_body_is_refused() -> None:
    document = _document_with(
        {
            "parameters": [{"name": "name", "in": "query", "schema": {"type": "string"}}],
            "requestBody": {
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/B"}}}
            },
        },
        B={"type": "object", "properties": {"name": {"type": "string"}}},
    )

    assert "duas vezes" in catalog.load(document).skipped["probe"]


# --- o documento é de fora: o que ele escolhe não vira nome nem caminho sem conferência ---------------


@pytest.mark.parametrize(
    "operation_id",
    [
        "../../escaped",
        "..",
        "a/b",
        "Maiuscula",
        "1comeca_com_numero",
        "tem-hifen",
        "tem espaco",
        "quebra\nde_linha",
        "ok\n",
        "x" * 65,
        "",
        None,
        7,
        ["lista"],
    ],
)
def test_an_operation_id_outside_the_format_is_not_registered(operation_id: Any) -> None:
    """O `operation_id` vira o nome da ferramenta e o começo do nome do arquivo salvo."""
    document = _document_with({"operationId": operation_id})
    document["paths"]["/t"]["get"]["operationId"] = operation_id

    loaded = catalog.load(document)

    assert loaded.tools == {}
    assert list(loaded.skipped) == ["GET /t"]  # o rótulo não repete o nome hostil
    assert loaded.skipped["GET /t"]


@pytest.mark.parametrize(
    "path",
    [
        "/../x",
        "/a/../b",
        "/a/./b",
        "//x",
        "/",
        "",
        "x",
        "/a b",
        "/a?x=1",
        "/a#b",
        "/a%2Fb",
        "/a\nb",
        "/{Bad}",
        "/{a}{b}",
        "/x/{slug}/",
        "/ação",
    ],
)
def test_a_path_template_outside_the_format_is_not_registered(path: str) -> None:
    document = _document_with({})
    document["paths"] = {path: document["paths"]["/t"]}

    loaded = catalog.load(document)

    assert loaded.tools == {}
    assert len(loaded.skipped) == 1


def test_the_path_template_and_the_declared_path_parameters_must_agree() -> None:
    declared = {"name": "slug", "in": "path", "required": True, "schema": {"type": "string"}}
    ok = _document_with({"parameters": [declared]})
    ok["paths"] = {"/forms/{slug}": ok["paths"]["/t"]}
    undeclared = _document_with({})
    undeclared["paths"] = {"/forms/{slug}": undeclared["paths"]["/t"]}
    unused = _document_with({"parameters": [declared]})

    assert "probe" in catalog.load(ok).tools
    assert "não são os parâmetros" in catalog.load(undeclared).skipped["probe"]
    assert "não são os parâmetros" in catalog.load(unused).skipped["probe"]


@pytest.mark.parametrize("name", ["../x", "a b", "a-b", "1a", "x" * 65, "ação", ""])
def test_an_argument_name_outside_the_format_is_not_registered(name: str) -> None:
    document = _document_with(
        {"parameters": [{"name": name, "in": "query", "schema": {"type": "string"}}]}
    )

    loaded = catalog.load(document)

    assert loaded.tools == {} and "nome de um argumento" in loaded.skipped["probe"]


def test_entries_that_are_not_objects_are_skipped_instead_of_stopping_the_start() -> None:
    document = _document_with({})
    document["paths"]["/a"] = "texto"
    document["paths"]["/b"] = {"get": ["lista"]}
    document["paths"]["/c"] = {
        "get": {"operationId": "ok_c", "x-required-role": "leitura", "parameters": 5}
    }
    document["paths"]["/d"] = {"parameters": "nao-e-lista", "get": {"operationId": "ok_d"}}

    loaded = catalog.load(document)

    assert set(loaded.tools) == {"probe"}
    assert set(loaded.skipped) == {"/a", "GET /b", "ok_c", "ok_d"}


def test_every_real_operation_and_path_is_inside_the_formats() -> None:
    assert all(catalog.OPERATION_ID.fullmatch(name) for name in TOOLS)
    assert all(catalog.PATH_TEMPLATE.fullmatch(tool.path) for tool in TOOLS.values())


@pytest.mark.parametrize(
    "components",
    [
        "texto",
        ["lista"],
        5,
        None,
        True,
        {"schemas": "texto"},
        {"schemas": ["x"]},
        {"schemas": None},
        {"schemas": 5},
    ],
)
def test_components_that_are_not_objects_are_a_catalog_error_and_not_an_attribute_error(
    components: Any,
) -> None:
    document = _document_with({})
    document["components"] = components

    with pytest.raises(CatalogError, match="components"):
        catalog.load(document)


def test_missing_components_or_schemas_just_mean_there_are_none() -> None:
    bare = _document_with({})
    del bare["components"]
    no_schemas = _document_with({})
    no_schemas["components"] = {}

    assert set(catalog.load(bare).tools) == {"probe"}
    assert set(catalog.load(no_schemas).tools) == {"probe"}


def test_a_document_without_paths_is_not_the_contract() -> None:
    with pytest.raises(CatalogError):
        catalog.load({"openapi": "3.1.0"})


def _refs(node: Any) -> list[str]:
    if isinstance(node, dict):
        found = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        return found + [ref for value in node.values() for ref in _refs(value)]
    if isinstance(node, list):
        return [ref for value in node for ref in _refs(value)]
    return []


def test_read_only_nature_is_distinct_from_required_role_and_effect_marks() -> None:
    from dataclasses import replace

    personal_get = TOOLS["render_job_participant_preview"]
    assert personal_get.role == "total" and personal_get.read_only
    assert TOOLS["render_form_draft_pdf_preview"].read_only
    assert not TOOLS["save_form_draft"].read_only
    assert not replace(personal_get, destructive=True).read_only
    assert replace(personal_get, external_effect=True).read_only
