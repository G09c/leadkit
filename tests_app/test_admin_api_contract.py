"""O contrato do OpenAPI da API de admin (ADR 0015, "O arquivo `openapi.json`", teste de contrato).

É o que garante, para toda operação que as fases de recurso acrescentarem, o que o Scalar mostra e o
MCP lê: `operation_id` único e em `snake_case`; `tags`, `summary` e `description` preenchidos; todo
corpo e toda resposta apontando para um modelo com nome; `x-required-role` em toda operação; os erros
declarados que a regra pede (com os cabeçalhos e o texto do `409` de cada operação); as marcas iguais
às da matriz; exemplo sem cara de segredo.

O contrato é conferido sobre o documento que a sub-aplicação gera (o mesmo que está em
`docs/api/openapi.json`) e, com um documento de brinquedo, o teste prova que cada regra reprova o que
cobra. No fim, o teste de eco: um valor sentinela em todo campo de todo corpo, parâmetro e caminho
não pode voltar em nenhuma resposta de erro.
"""

from __future__ import annotations

import copy
import re
import secrets
import uuid
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import httpx
import pytest
from admin_api_matrix import (
    MARKS,
    ROLES,
    differences,
    operations_of,
    read,
    snake_case,
)
from admin_api_support import API, ApiClient, add_routes, admin_api, built_openapi
from pydantic import BaseModel, field_validator

from app.admin.api.common import ApiInput
from app.admin.api.operations import OperationRouter

SCHEMAS = "#/components/schemas/"
ERROR = f"{SCHEMAS}ErrorResponse"
JSON_LIKE = ("application/json", "multipart/form-data", "application/x-www-form-urlencoded")
# O que um exemplo não pode parecer: uma chave de API (`gp_` e um segredo), um JWT, ou um texto
# comprido sem espaço que mistura as classes de caracteres de um segredo (base64 com minúsculas,
# maiúsculas e números; hexadecimal com números e letras). O GitGuardian do PR acusa valor com essa
# cara. Um UUID, um slug com hífen (`painel-de-vendas-2027`, sem maiúsculas) e uma URL não são segredo.
KEY_SHAPE = re.compile(r"gp_[0-9a-f]{8}_[A-Za-z0-9_-]{16,}")
JWT_SHAPE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
LONG_TOKEN = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
UUID_SHAPE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
LONG_HEX = re.compile(r"[0-9a-f]{32,}")
EXAMPLE_KEYS = ("example", "examples", "default", "const", "enum")
HEADERS_OF = {"401": "WWW-Authenticate", "429": "Retry-After", "503": "Retry-After", "405": "Allow"}
BINARY = {"type": "string", "format": "binary"}
TEXT = {"type": "string"}
# O bloco que guarda o dado da lead nas respostas, e o que só pode viver dentro dele: o dado pessoal
# (nome, e-mail, telefone), a pontuação e o consentimento (que a ADR 0015 também conta como pessoais),
# as respostas, o plano, a prioridade e a persona (que saem da pontuação) e os textos livres dos
# motivos (o motivo de um reprocessamento, o detalhe da falha, o último erro de uma entrega ou de uma
# sincronização: `reason`, `failure_detail`, `last_error`, `note` e `notes`), onde uma operadora pode
# ter escrito o nome de uma lead. É a camada do que se conhece; `LEAD_STATE`, abaixo, trava o resto.
LEAD_BLOCK = "lead_data"
# As únicas propriedades `token*` que uma resposta pode ter: dizem o estado do token do Baserow, nunca
# o token. O resto de `token*` só entra, num corpo, `writeOnly`.
TOKEN_STATE = frozenset({"token_set", "token_unreadable", "token_changed_at"})
# O segredo de um destino do webhook nasce no servidor e sai uma vez: as únicas propriedades `secret*`
# de uma resposta são as que dizem o estado dele (`SECRET_STATE`), e o próprio `secret` só nas
# operações `total` que o geram (`SECRET_ONCE`: a criação e a troca), na raiz da resposta delas. Nenhum
# corpo de entrada tem uma propriedade `secret*`.
SECRET_STATE = frozenset({"secret_unreadable", "secret_created_at"})
SECRET_ONCE = {
    "save_destination": "secret",
    "rotate_destination_secret": "secret",
    "create_integration_credential": "secret",
    "rotate_integration_credential": "secret",
    "create_event_credential": "secret",
    "rotate_event_credential": "secret",
}
LEAD_FIELDS = frozenset(
    {
        "name",
        "full_name",
        "email",
        "phone",
        "persona",
        "priority",
        "scores",
        "answers",
        "consent",
        "plan",
        "notes",
        "note",
        "reason",
        "failure_detail",
        "last_error",
    }
)
# Os campos de ESTADO de cada operação x-lead-data: tudo o que ela devolve fora do `lead_data`, como
# caminhos separados por ponto (`items.status`; `items` é a lista do envelope). A resposta fora do bloco
# é exatamente isto: um campo novo reprova até entrar aqui (e a revisão ver se é de estado, nada que
# diga quem é a lead), e um que sai da resposta reprova até sair daqui. Operação x-lead-data nova
# ganha a sua entrada; `download_job_pdf` devolve um arquivo, sem campo JSON.
_JOB_ITEM = (
    "attempt",
    "created_at",
    "deliveries",
    "deliveries.destination",
    "deliveries.status",
    "failure_code",
    "form_slug",
    "form_version",
    "public_id",
    "stage",
    "status",
    "syncs",
    "syncs.integration",
    "syncs.status",
)
_CAPTURE_ITEM = (
    "submission_id",
    "status",
    "form_slug",
    "form_version",
    "form_version_number",
    "created_at",
    "deliveries",
    "deliveries.delivery_id",
    "deliveries.submission_id",
    "deliveries.destination_id",
    "deliveries.kind",
    "deliveries.event",
    "deliveries.schema_version",
    "deliveries.config_version",
    "deliveries.current_config_version",
    "deliveries.status",
    "deliveries.attempts",
    "deliveries.dispatch_no",
    "deliveries.error_code",
    "deliveries.response_status",
    "deliveries.row_id",
    "deliveries.outcome",
    "deliveries.delivered_at",
)
_JOURNEY_EVENT_STATE = (
    "id",
    "origin",
    "source_id",
    "source_revision",
    "type",
    "entity_kind",
    "entity_revision",
    "occurred_at",
    "received_at",
    "state",
    "status",
    "starts_at",
    "ends_at",
    "timezone",
    "meeting_revision",
    "meeting_event_id",
)
LEAD_STATE: dict[str, frozenset[str]] = {
    "get_submission_commercial_context": frozenset(
        {
            "submission_id",
            "form_slug",
            "form_version",
            "form_version_number",
            "created_at",
            "historical_version_available",
            "generation",
            "job",
            "job.id",
            "job.public_id",
            "job.attempt",
            "job.status",
            "job.current_pdf",
            "job.current_pdf.attempt",
            "job.current_pdf.state",
            "job.current_pdf.available",
            "job.current_pdf.exists",
            "job.current_pdf.valid",
            "job.current_pdf.accessible",
            "job.current_pdf.verified_at",
            "job.release",
            "job.release.form_version_number",
            "job.release.policy",
            "job.release.state",
            "job.release.release_version",
            "job.release.release_id",
            "job.release.released_at",
            "job.release.attempt",
            "job.release.artifact_id",
        }
    ),
    "get_submission_journey": frozenset(
        {
            "submission_id",
            "reference_id",
            "reference_version",
            "journey_version",
            "job",
            "job.id",
            "job.attempt",
            "job.status",
            "local_facts",
            "local_facts.origin",
            "local_facts.type",
            "local_facts.state",
            "local_facts.occurred_at",
            "local_facts.attempt",
            "external_declared",
            *(f"external_declared.{field}" for field in _JOURNEY_EVENT_STATE),
        }
    ),
    "list_submission_journey_events": frozenset(
        {"items", "next_cursor", *(f"items.{field}" for field in _JOURNEY_EVENT_STATE)}
    ),
    "list_captures": frozenset(
        {"items", "next_cursor", *(f"items.{field}" for field in _CAPTURE_ITEM)}
    ),
    "get_capture": frozenset(_CAPTURE_ITEM),
    "list_jobs": frozenset(
        {"items", "next_cursor", "total", *(f"items.{field}" for field in _JOB_ITEM)}
    ),
    "get_job": frozenset(
        {
            "persisted_deliveries",
            *(
                f"persisted_deliveries.{field}"
                for field in (
                    "delivery_id",
                    "submission_id",
                    "destination_id",
                    "kind",
                    "event",
                    "schema_version",
                    "config_version",
                    "current_config_version",
                    "status",
                    "attempts",
                    "dispatch_no",
                    "error_code",
                    "response_status",
                    "row_id",
                    "outcome",
                    "delivered_at",
                )
            ),
            "release",
            "release.policy",
            "release.form_version_number",
            "release.state",
            "release.release_version",
            "release.release_id",
            "release.released_at",
            "release.attempt",
            "release.artifact_id",
            "deliveries.event",
            "deliveries.release_id",
            "deliveries.occurred_at",
            "deliveries.blocked_reason",
            "deliveries.audience",
            "deliveries.authorization_origin",
            "deliveries.config_version",
            "deliveries.current_config_version",
            "deliveries.dispatch_no",
            "current_pdf",
            "current_pdf.accessible",
            "current_pdf.attempt",
            "current_pdf.available",
            "current_pdf.exists",
            "current_pdf.state",
            "current_pdf.valid",
            "current_pdf.verified_at",
            "attempt",
            "completed_at",
            "created_at",
            "deliveries",
            "deliveries.attempt",
            "deliveries.attempts",
            "deliveries.delivered_at",
            "deliveries.destination",
            "deliveries.host",
            "deliveries.id",
            "deliveries.run_url",
            "deliveries.scope",
            "deliveries.status",
            "failure_code",
            "form_slug",
            "form_version",
            "link_expired",
            "link_expires_at",
            "pdfs",
            "pdfs.byte_size",
            "pdfs.created_at",
            "pdfs.purged_at",
            "pdfs.version",
            "public_id",
            "run_url",
            "stage",
            "status",
            "syncs",
            "syncs.attempt",
            "syncs.attempts",
            "syncs.created_at",
            "syncs.host",
            "syncs.id",
            "syncs.integration",
            "syncs.outcome",
            "syncs.row_id",
            "syncs.status",
            "syncs.synced_at",
            "syncs.table_id",
            "timeline",
            "timeline.attempt",
            "timeline.events",
            "timeline.events.actor",
            "timeline.events.at",
            "timeline.events.delivery_id",
            "timeline.events.failure_code",
            "timeline.events.model",
            "timeline.events.prompt_version",
            "timeline.events.stage",
            "timeline.events.type",
            "timeline.outcome",
        }
    ),
    "download_job_pdf": frozenset(),
    "render_job_participant_preview": frozenset(),  # HTML pessoal integral, sem estado JSON.
    "get_job_plan_projection": frozenset(
        {"public_id", "form_slug", "form_version", "form_version_number", "attempt", "audience"}
    ),
}


# --- as regras ----------------------------------------------------------------------------------


def _model(document: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """Por que o esquema não é um modelo com nome (ou `None`, se é)."""
    ref = str(schema.get("$ref", ""))
    name = ref.removeprefix(SCHEMAS)
    models = document.get("components", {}).get("schemas", {})
    if not ref.startswith(SCHEMAS) or name not in models:
        shape = schema.get("type") or "sem tipo"
        return f"não aponta para um modelo com nome (é {shape} solto)"
    model = models[name]
    opened = (
        model.get("type") == "object"
        and not model.get("properties")
        and model.get("additionalProperties") is not False
        and not any(key in model for key in ("allOf", "anyOf", "oneOf"))
    )
    return f"o modelo {name} é um objeto aberto (um dict solto)" if opened else None


def _fields(document: dict[str, Any], operation: dict[str, Any]) -> set[str]:
    """Os nomes que a operação recebe: parâmetros e as chaves do primeiro nível do corpo."""
    names = {parameter["name"] for parameter in operation.get("parameters", [])}
    for media in operation.get("requestBody", {}).get("content", {}).values():
        name = str(media.get("schema", {}).get("$ref", "")).removeprefix(SCHEMAS)
        names |= set(
            document.get("components", {}).get("schemas", {}).get(name, {}).get("properties", {})
        )
    return names


def _not_a_file_or_a_page(where: str, status: str, kind: str, media: dict[str, Any]) -> list[str]:
    """Uma resposta de sucesso que não é JSON é declarada pelo tipo, e não como um modelo: um arquivo
    (o PDF de um pedido) em binário e uma página (a prévia em HTML) como texto."""
    if kind.startswith("text/"):
        if media.get("schema") == TEXT:
            return []
        return [f'{where}: a resposta {status} ({kind}) não é texto (`{{"type": "string"}}`)']
    if media.get("schema") == BINARY:
        return []
    binary = '(`{"type": "string", "format": "binary"}`)'
    return [f"{where}: a resposta {status} ({kind}) não é binária {binary}"]


def _bodies_and_responses(
    where: str, document: dict[str, Any], operation: dict[str, Any]
) -> list[str]:
    found = []
    for kind, media in operation.get("requestBody", {}).get("content", {}).items():
        if kind in JSON_LIKE and (why := _model(document, media.get("schema", {}))):
            found.append(f"{where}: o corpo ({kind}) {why}")
    successes = [status for status in operation.get("responses", {}) if status.startswith("2")]
    if not successes:
        found.append(f"{where}: nenhuma resposta de sucesso (2xx) declarada")
    for status, response in operation.get("responses", {}).items():
        description = str(response.get("description", "")).strip()
        if not description or (status.startswith("2") and description == "Successful Response"):
            found.append(f"{where}: a resposta {status} não tem descrição (falta o `returns`)")
        for kind, media in response.get("content", {}).items():
            if kind in JSON_LIKE and (why := _model(document, media.get("schema", {}))):
                found.append(f"{where}: a resposta {status} ({kind}) {why}")
            elif kind not in JSON_LIKE and status.startswith("2"):
                found += _not_a_file_or_a_page(where, status, kind, media)
    return found


def _properties(
    document: dict[str, Any],
    schema: dict[str, Any],
    path: tuple[str, ...] = (),
    seen: frozenset[str] = frozenset(),
) -> Iterator[tuple[tuple[str, ...], dict[str, Any]]]:
    """O caminho (os nomes de propriedade, da raiz até cada uma) e o esquema de tudo o que um esquema
    deixa sair, seguindo `$ref`, `anyOf`/`allOf`/`oneOf` e os itens de uma lista."""
    ref = str(schema.get("$ref", ""))
    if ref.startswith(SCHEMAS):
        name = ref.removeprefix(SCHEMAS)
        if name in seen:  # um modelo que cita a si mesmo
            return
        seen = seen | {name}
        schema = document.get("components", {}).get("schemas", {}).get(name, {})
    for key in ("anyOf", "allOf", "oneOf"):
        for option in schema.get(key, []):
            yield from _properties(document, option, path, seen)
    if isinstance(schema.get("items"), dict):
        yield from _properties(document, schema["items"], path, seen)
    for name, inner in schema.get("properties", {}).items():
        yield (*path, name), inner
        yield from _properties(document, inner, (*path, name), seen)


def _property_paths(
    document: dict[str, Any],
    schema: dict[str, Any],
    path: tuple[str, ...] = (),
    seen: frozenset[str] = frozenset(),
) -> Iterator[tuple[str, ...]]:
    """Só os caminhos de `_properties`."""
    for found, _ in _properties(document, schema, path, seen):
        yield found


def _is_write_only(document: dict[str, Any], schema: dict[str, Any]) -> bool:
    """O esquema de uma propriedade é `writeOnly`: ele mesmo, ou toda opção que não é `null` de um
    `anyOf` (um `token` opcional é `string` ou `null`)."""
    ref = str(schema.get("$ref", ""))
    if ref.startswith(SCHEMAS):
        schema = (
            document.get("components", {}).get("schemas", {}).get(ref.removeprefix(SCHEMAS), {})
        )
    if schema.get("writeOnly") is True:
        return True
    options = [
        option
        for key in ("anyOf", "oneOf")
        for option in schema.get(key, [])
        if option.get("type") != "null"
    ]
    return bool(options) and all(_is_write_only(document, option) for option in options)


def _tokens(where: str, document: dict[str, Any], operation: dict[str, Any]) -> list[str]:
    """O token só entra (ADR 0015, "Segredos e dados sensíveis"): nenhuma resposta de sucesso tem uma
    propriedade `token*`, a não ser as que dizem o estado dele (`TOKEN_STATE`), e toda propriedade
    `token*` de um corpo é `writeOnly`."""
    found = []
    for status, response in operation.get("responses", {}).items():
        if not status.startswith("2"):
            continue
        for kind, media in response.get("content", {}).items():
            if kind not in JSON_LIKE:
                continue
            for path in _property_paths(document, media.get("schema", {})):
                if path[-1].startswith("token") and path[-1] not in TOKEN_STATE:
                    found.append(
                        f"{where}: a resposta {status} devolve a propriedade «{'.'.join(path)}»: "
                        "um token só entra, e a resposta diz o estado dele "
                        f"({', '.join(sorted(TOKEN_STATE))})"
                    )
    for kind, media in operation.get("requestBody", {}).get("content", {}).items():
        if kind not in JSON_LIKE:
            continue
        for path, schema in _properties(document, media.get("schema", {})):
            if path[-1].startswith("token") and not _is_write_only(document, schema):
                found.append(
                    f"{where}: o corpo ({kind}) tem a propriedade «{'.'.join(path)}» sem "
                    "`writeOnly`: um token só entra (`SecretStr`)"
                )
    return found


def _secrets(where: str, document: dict[str, Any], operation: dict[str, Any]) -> list[str]:
    """O segredo de um destino só sai uma vez (ADR 0015, "Segredos e dados sensíveis"): nenhuma
    resposta de sucesso tem uma propriedade `secret*`, a não ser as que dizem o estado dele
    (`SECRET_STATE`) e o próprio segredo, na raiz da resposta de uma operação `total` que o gera
    (`SECRET_ONCE`); e nenhum corpo de entrada tem uma propriedade `secret*`, porque o segredo nasce no
    servidor."""
    found = []
    once = SECRET_ONCE.get(str(operation.get("operationId")))
    for status, response in operation.get("responses", {}).items():
        if not status.startswith("2"):
            continue
        for kind, media in response.get("content", {}).items():
            if kind not in JSON_LIKE:
                continue
            for path in _property_paths(document, media.get("schema", {})):
                name = path[-1]
                if not name.startswith("secret") or name in SECRET_STATE:
                    continue
                if path == (once,) and operation.get("x-required-role") == "total":
                    continue
                found.append(
                    f"{where}: a resposta {status} devolve a propriedade «{'.'.join(path)}»: "
                    "o segredo só sai na resposta de quem o gera (a criação e a troca, de `total`), "
                    f"e as outras respostas dizem o estado dele ({', '.join(sorted(SECRET_STATE))})"
                )
    for kind, media in operation.get("requestBody", {}).get("content", {}).items():
        if kind not in JSON_LIKE:
            continue
        for path, _ in _properties(document, media.get("schema", {})):
            if path[-1].startswith("secret"):
                found.append(
                    f"{where}: o corpo ({kind}) tem a propriedade «{'.'.join(path)}»: o segredo "
                    "nasce no servidor, e nenhum corpo o recebe"
                )
    return found


def _lead_data(
    where: str,
    document: dict[str, Any],
    operation: dict[str, Any],
    state_fields: Mapping[str, frozenset[str]],
) -> list[str]:
    """A barreira entre o papel `leitura` e a lead (ADR 0015, "Segredos e dados sensíveis"), em duas
    camadas. Numa operação `x-lead-data`:

    1. nome, e-mail, telefone, respostas, pontuação, consentimento, plano e os textos livres dos
       motivos (`LEAD_FIELDS`) só podem estar dentro do bloco `lead_data`, que a operação devolve vazio
       a quem não pode vê-lo: é a lista do que se conhece;
    2. tudo o que ela devolve **fora** do bloco é exatamente o que `state_fields` (`LEAD_STATE`) trava
       para ela: um campo novo no estado reprova até alguém incluí-lo ali, e é a revisão que decide se
       ele é de estado ou da lead. Pega o que a lista da camada 1 não prevê.

    E toda operação que devolve o bloco leva a marca."""
    marked = operation.get("x-lead-data") is True
    operation_id = str(operation.get("operationId"))
    found, has_block, outside = [], False, set()
    for status, response in operation.get("responses", {}).items():
        if not status.startswith("2"):
            continue
        for kind, media in response.get("content", {}).items():
            if kind not in JSON_LIKE:
                continue
            for path in _property_paths(document, media.get("schema", {})):
                if LEAD_BLOCK in path[:-1]:
                    continue
                if path[-1] == LEAD_BLOCK:
                    has_block = True
                    continue
                outside.add(".".join(path))
                if marked and path[-1] in LEAD_FIELDS:
                    found.append(
                        f"{where}: o campo pessoal «{'.'.join(path)}» está fora do bloco "
                        f"`{LEAD_BLOCK}` (numa operação x-lead-data, os dados da lead só vão dentro "
                        "dele)"
                    )
    if has_block and not marked:
        found.append(f"{where}: devolve o bloco `{LEAD_BLOCK}`, mas não é marcada x-lead-data")
    if marked and operation_id not in state_fields:
        found.append(
            f"{where}: {operation_id} é x-lead-data e não tem os campos de estado travados em "
            "LEAD_STATE (tests_app/test_admin_api_contract.py)"
        )
    elif marked:
        locked = state_fields[operation_id]
        found += [
            f"{where}: o campo «{path}» de {operation_id} não está em LEAD_STATE "
            "(tests_app/test_admin_api_contract.py). Se é de estado, nada que diga quem é a lead, "
            f"inclua-o ali; se é da lead, vai dentro de `{LEAD_BLOCK}`"
            for path in sorted(outside - locked)
        ]
        found += [
            f"{where}: «{path}» está em LEAD_STATE, mas {operation_id} já não o devolve: tire-o de lá"
            for path in sorted(locked - outside)
        ]
    return found


def lock_problems(
    document: dict[str, Any], state_fields: Mapping[str, frozenset[str]]
) -> list[str]:
    """As entradas de `LEAD_STATE` que não valem: de uma operação que a API não tem, ou que não é
    `x-lead-data`. (A falta de uma entrada, para uma operação marcada, é de `contract_problems`.)"""
    operations = {op["operationId"]: op for _, _, op in operations_of(document)}
    found = []
    for operation_id in sorted(state_fields):
        if operation_id not in operations:
            found.append(f"LEAD_STATE lista {operation_id}, que a API não tem")
        elif operations[operation_id].get("x-lead-data") is not True:
            found.append(f"LEAD_STATE lista {operation_id}, que não é x-lead-data")
    return found


def _errors(
    where: str, path: str, operation: dict[str, Any], document: dict[str, Any]
) -> list[str]:
    found = []
    responses = operation.get("responses", {})
    role = operation.get("x-required-role")
    versioned = bool(
        {
            "expected_version",
            "expected_receiver_version",
            "expected_source_version",
            "expected_journey_version",
            "expected_handoff_version",
        }
        & set(_fields(document, operation))
    )
    takes_input = bool(operation.get("requestBody") or operation.get("parameters"))
    required = {"401": "toda operação", "429": "toda operação"}
    if role == "total":
        required["403"] = "as operações `total`"
    if "{" in path:
        required["404"] = "as que têm um recurso no caminho"
    if versioned:
        required["409"] = "as que pedem `expected_version`"
    if takes_input:
        required["422"] = "as que têm corpo ou parâmetro"
    for status, who in required.items():
        if status not in responses:
            found.append(f"{where}: falta declarar o erro {status} ({who})")
    if not takes_input and "422" in responses:
        found.append(
            f"{where}: declara 422, que não pode ocorrer (nada a validar: sem corpo nem parâmetro)"
        )
    for status, response in responses.items():
        if status[0] not in "45":
            continue
        schema = response.get("content", {}).get("application/json", {}).get("schema")
        if schema != {"$ref": ERROR}:
            found.append(f"{where}: a resposta {status} não usa o modelo ErrorResponse")
        header = HEADERS_OF.get(status)
        if header and header not in response.get("headers", {}):
            found.append(f"{where}: a resposta {status} não declara o cabeçalho {header}")
    if "409" in responses:
        text = str(responses["409"].get("description", ""))
        # #34: a origem opcional da simulação conflita com o estado do snapshot;
        # não é uma gravação otimista do recurso. As demais escritas mantêm version_conflict.
        snapshot = operation.get("operationId") in {
            "assemble_form_draft_prompt",
            "score_form_draft",
            "test_capture_form_draft",
            "start_form_draft_test",
        }
        if snapshot and "state_conflict" not in text:
            found.append(f"{where}: o 409 da simulação fala de `state_conflict`")
        if versioned and not snapshot and "version_conflict" not in text:
            found.append(f"{where}: o 409 de uma operação com versão fala de `version_conflict`")
        if not versioned and "version_conflict" in text:
            found.append(
                f"{where}: o 409 de uma operação sem versão não fala de `version_conflict`"
            )
        # O recibo conflita com o estado do vínculo histórico, sem receber versão corrente.
        # Exigir o código real e a categoria evita documentar state_conflict inexistente no GET.
        command_receipt = operation.get("operationId") == "get_form_draft_test_command"
        if command_receipt and ("command_conflict" not in text or "estado" not in text):
            found.append(
                f"{where}: o 409 do recibo descreve o estado do vínculo (`command_conflict`)"
            )
        if (
            operation.get("operationId") == "start_form_draft_test"
            and "command_conflict" not in text
        ):
            found.append(f"{where}: o 409 da partida também declara `command_conflict`")
        if not command_receipt and "state_conflict" not in text and "version_conflict" not in text:
            found.append(f"{where}: o texto do 409 diz qual é o conflito (version ou state)")
    return found


def _role_and_marks(where: str, document: dict[str, Any], operation: dict[str, Any]) -> list[str]:
    found = []
    if operation.get("x-required-role") not in ROLES:
        found.append(f"{where}: sem `x-required-role` (leitura ou total)")
    if not operation.get("security"):
        found.append(f"{where}: sem esquema de segurança (a referência a mostraria aberta)")
    for mark in MARKS.values():
        if mark in operation and operation[mark] is not True:
            found.append(f"{where}: {mark} só aparece quando vale (true)")
    marked = operation.get("x-destructive") or operation.get("x-external-effect")
    if marked and "confirm" not in _fields(document, operation):
        found.append(f"{where}: operação marcada que não declara o campo `confirm`")
    if "x-confirm-unless" in operation:
        found += _confirm_unless(where, document, operation, bool(marked))
    return found


def _body_property(
    document: dict[str, Any], operation: dict[str, Any], name: str
) -> dict[str, Any] | None:
    """O esquema de um campo do primeiro nível do corpo JSON da operação; `None` se não existe."""
    media = operation.get("requestBody", {}).get("content", {}).get("application/json", {})
    model = str(media.get("schema", {}).get("$ref", "")).removeprefix(SCHEMAS)
    schema = document.get("components", {}).get("schemas", {}).get(model, {})
    found = schema.get("properties", {}).get(name)
    return found if isinstance(found, dict) else None


def _confirm_unless(
    where: str, document: dict[str, Any], operation: dict[str, Any], marked: bool
) -> list[str]:
    """`x-confirm-unless` (a simulação que dispensa o `confirm`): só numa operação marcada, e o nome é
    de um campo booleano do corpo que começa falso, senão a dispensa não se vê ou vale sem pedir."""
    found = []
    if not marked:
        found.append(
            f"{where}: `x-confirm-unless` numa operação sem marca (não há o que dispensar)"
        )
    name = operation["x-confirm-unless"]
    field = _body_property(document, operation, name) if isinstance(name, str) else None
    if field is None or field.get("type") != "boolean" or field.get("default") is not False:
        found.append(
            f"{where}: `x-confirm-unless` aponta para «{name}», que não é um campo booleano do "
            "corpo que começa falso"
        )
    return found


def _strings(node: Any, in_properties: bool = False) -> list[str]:
    """Os textos de tudo o que o documento chama de exemplo, valor padrão ou valor fixo."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in EXAMPLE_KEYS and not in_properties:
                found += _every_string(value)
            else:
                found += _strings(value, in_properties=key == "properties")
    elif isinstance(node, list):
        for item in node:
            found += _strings(item)
    return found


def _every_string(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [text for value in node.values() for text in _every_string(value)]
    if isinstance(node, list):
        return [text for value in node for text in _every_string(value)]
    return []


def mixes_secret_classes(token: str) -> bool:
    """Se um texto comprido sem espaço mistura as classes de um segredo: minúsculas, maiúsculas e
    números juntos (base64), ou só hexadecimal com números e letras."""
    has = (
        any(c.islower() for c in token),
        any(c.isupper() for c in token),
        any(c.isdigit() for c in token),
    )
    hex_run = LONG_HEX.fullmatch(token) is not None
    return all(has) or (hex_run and has[2] and has[0])


def looks_like_a_secret(text: str) -> bool:
    """Se um texto de exemplo tem cara de segredo: uma chave de API, um JWT ou, numa palavra que não
    é uma URL, um texto comprido que mistura as classes de um segredo (um UUID não conta)."""
    if KEY_SHAPE.search(text) or JWT_SHAPE.search(text):
        return True
    for word in text.split():
        if word.startswith(("http://", "https://")):
            continue
        for token in LONG_TOKEN.findall(word):
            if UUID_SHAPE.fullmatch(token) is None and mixes_secret_classes(token):
                return True
    return False


def _examples(document: dict[str, Any]) -> list[str]:
    found = [
        f"exemplo com cara de segredo: «{text[:24]}...»"
        for text in _strings(document)
        if looks_like_a_secret(text)
    ]
    everything = _every_string(document)
    if any(KEY_SHAPE.search(text) for text in everything):
        found.append("o documento traz uma chave de API (`gp_<id>_<segredo>`) inteira")
    return found


def contract_problems(
    document: dict[str, Any], state_fields: Mapping[str, frozenset[str]] | None = None
) -> list[str]:
    """Tudo o que o contrato cobra do documento. `state_fields` são os campos de estado travados de
    cada operação x-lead-data (`LEAD_STATE`, o padrão); os documentos de brinquedo trazem os seus."""
    state_fields = LEAD_STATE if state_fields is None else state_fields
    found = []
    tags = {
        tag["name"]: str(tag.get("description") or "").strip() for tag in document.get("tags", [])
    }
    seen: set[str] = set()
    for method, path, operation in operations_of(document):
        where = f"{method} {path}"
        operation_id = operation.get("operationId")
        if not isinstance(operation_id, str) or not snake_case(operation_id):
            found.append(
                f"{where}: `operation_id` «{operation_id}» não é `snake_case` (verbo e recurso)"
            )
        elif operation_id in seen:
            found.append(f"{where}: `operation_id` {operation_id} repetido")
        seen.add(str(operation_id))
        for field in ("summary", "description"):
            if not str(operation.get(field) or "").strip():
                found.append(f"{where}: sem `{field}`")
        if not operation.get("tags"):
            found.append(f"{where}: sem `tags`")
        for tag in operation.get("tags", []):
            if not tags.get(tag):
                found.append(f"{where}: a tag «{tag}» não tem descrição em `tags` do OpenAPI")
        found += _role_and_marks(where, document, operation)
        found += _bodies_and_responses(where, document, operation)
        found += _lead_data(where, document, operation, state_fields)
        found += _tokens(where, document, operation)
        found += _secrets(where, document, operation)
        found += _errors(where, path, operation, document)
    return found + _examples(document)


def matrix_problems(document: dict[str, Any]) -> list[str]:
    """As marcas, o papel, o método e o caminho do OpenAPI contra a linha da matriz de cada operação
    (as operações sem linha são do teste de cobertura)."""
    matrix = read()
    rows = {row.operation_id: row for row in matrix.rows if row.operation_id and row.operation}
    return [
        f"{rows[operation['operationId']].label}: {operation['operationId']}: {difference}"
        for method, path, operation in operations_of(document)
        if operation["operationId"] in rows
        for difference in differences(rows[operation["operationId"]], method, path, operation)
    ]


# --- o contrato do documento real ---------------------------------------------------------------


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return built_openapi()


def test_the_openapi_of_the_admin_api_follows_the_contract(document: dict[str, Any]) -> None:
    found = contract_problems(document)

    assert not found, "\n".join(found)


def test_the_marks_and_roles_of_the_openapi_are_the_ones_of_the_matrix(
    document: dict[str, Any],
) -> None:
    found = matrix_problems(document)

    assert not found, "\n".join(found)


def test_the_contract_looks_at_the_operations_that_exist(document: dict[str, Any]) -> None:
    """Um teste que não olha nada passa sempre: ao menos as quatro operações das chaves estão lá (as
    fases de recurso acrescentam as delas)."""
    assert {
        "create_api_key",
        "get_current_api_key",
        "list_api_keys",
        "revoke_api_key",
    } <= {op["operationId"] for _, _, op in operations_of(document)}


def test_the_matrix_marks_are_charged_against_the_file(document: dict[str, Any]) -> None:
    changed = copy.deepcopy(document)
    changed["paths"]["/api-keys"]["post"]["x-destructive"] = True  # a matriz diz só `externo`
    changed["paths"]["/me"]["get"]["x-required-role"] = "total"

    found = "\n".join(matrix_problems(changed))

    assert "create_api_key: as marcas são ['x-external-effect'] na matriz" in found
    assert "get_current_api_key: o papel é «leitura» na matriz e «total» no OpenAPI" in found
    assert "docs/api/cobertura.md" in found


# --- o teste do teste: um documento de brinquedo ------------------------------------------------


def error(text: str = "Erro.", *headers: str) -> dict[str, Any]:
    """Uma resposta de erro do brinquedo: o modelo `ErrorResponse` e os cabeçalhos que ela declara."""
    response: dict[str, Any] = {
        "description": text,
        "content": {"application/json": {"schema": {"$ref": ERROR}}},
    }
    if headers:
        response["headers"] = {name: {"schema": {"type": "string"}} for name in headers}
    return response


def toy() -> dict[str, Any]:
    """Um documento mínimo que cumpre o contrato: uma lista, uma criação marcada e uma escrita
    versionada sobre um recurso do caminho."""
    common = {
        "401": error("Sem chave.", "WWW-Authenticate"),
        "429": error("Passou do limite.", "Retry-After"),
    }
    listing = {
        "operationId": "list_things",
        "summary": "Listar as coisas",
        "description": "Lista as coisas.",
        "tags": ["Coisas"],
        "x-required-role": "leitura",
        "security": [{"ApiKeyAuth": []}],
        "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
        "responses": {
            "200": {
                "description": "Uma página.",
                "content": {"application/json": {"schema": {"$ref": f"{SCHEMAS}ThingPage"}}},
            },
            **common,
            "422": error(),
        },
    }
    creation = {
        "operationId": "create_thing",
        "summary": "Criar uma coisa",
        "description": "Cria uma coisa.",
        "tags": ["Coisas"],
        "x-required-role": "total",
        "x-external-effect": True,
        "security": [{"ApiKeyAuth": []}],
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"$ref": f"{SCHEMAS}CreateThing"}}},
        },
        "responses": {
            "201": {
                "description": "A coisa.",
                "content": {"application/json": {"schema": {"$ref": f"{SCHEMAS}Thing"}}},
            },
            **common,
            "403": error(),
            "422": error(),
        },
    }
    rename = {
        "operationId": "rename_thing",
        "summary": "Renomear uma coisa",
        "description": "Renomeia a coisa.",
        "tags": ["Coisas"],
        "x-required-role": "total",
        "security": [{"ApiKeyAuth": []}],
        "parameters": [{"name": "thing_id", "in": "path", "required": True, "schema": {}}],
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"$ref": f"{SCHEMAS}RenameThing"}}},
        },
        "responses": {
            "200": {
                "description": "A coisa.",
                "content": {"application/json": {"schema": {"$ref": f"{SCHEMAS}Thing"}}},
            },
            **common,
            "403": error(),
            "404": error(),
            "409": error("A versão esperada mudou (`version_conflict`)."),
            "422": error(),
        },
    }
    thing = {"type": "object", "properties": {"name": {"type": "string"}}}
    return {
        "openapi": "3.1.0",
        "tags": [{"name": "Coisas", "description": "As coisas."}],
        "paths": {
            "/things": {"get": listing, "post": creation},
            "/things/{thing_id}": {"put": rename},
        },
        "components": {
            "schemas": {
                "Thing": thing,
                "ThingPage": {"type": "object", "properties": {"items": {"type": "array"}}},
                "CreateThing": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "confirm": {"type": "boolean"}},
                },
                "RenameThing": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "expected_version": {"type": "string"},
                    },
                },
                "ErrorResponse": {"type": "object", "properties": {"code": {"type": "string"}}},
            }
        },
    }


def operation_of(document: dict[str, Any], operation_id: str) -> dict[str, Any]:
    return next(op for _, _, op in operations_of(document) if op["operationId"] == operation_id)


def test_the_toy_document_follows_the_contract() -> None:
    assert contract_problems(toy()) == []


def drop(*keys: str) -> Callable[[dict[str, Any]], None]:
    def mutate(operation: dict[str, Any]) -> None:
        for key in keys:
            del operation[key]

    return mutate


def put(**changes: Any) -> Callable[[dict[str, Any]], None]:
    return lambda operation: operation.update(changes)


def response(
    status: str, change: Callable[[dict[str, Any]], None]
) -> Callable[[dict[str, Any]], None]:
    return lambda operation: change(operation["responses"][status])


def remove_response(status: str) -> Callable[[dict[str, Any]], None]:
    return lambda operation: operation["responses"].pop(status)


def loose_body(operation: dict[str, Any]) -> None:
    """O corpo `dict` solto: o esquema é um objeto qualquer, sem modelo."""
    operation["requestBody"]["content"]["application/json"]["schema"] = {
        "type": "object",
        "additionalProperties": True,
    }


def loose_response(operation: dict[str, Any]) -> None:
    operation["responses"]["201"]["content"]["application/json"]["schema"] = {"type": "object"}


def bare_list(operation: dict[str, Any]) -> None:
    operation["responses"]["200"]["content"]["application/json"]["schema"] = {
        "type": "array",
        "items": {"$ref": f"{SCHEMAS}Thing"},
    }


MUTATIONS = [
    # (operação, o que muda, o que a mensagem diz)
    ("list_things", drop("x-required-role"), "sem `x-required-role`"),
    ("list_things", put(**{"x-required-role": "root"}), "sem `x-required-role`"),
    ("list_things", drop("summary"), "sem `summary`"),
    ("list_things", put(description="  "), "sem `description`"),
    ("list_things", put(tags=[]), "sem `tags`"),
    ("list_things", put(tags=["Outra"]), "a tag «Outra» não tem descrição"),
    ("list_things", put(operationId="ListThings"), "não é `snake_case`"),
    ("list_things", put(operationId="things"), "não é `snake_case`"),
    ("list_things", drop("security"), "sem esquema de segurança"),
    ("list_things", put(**{"x-lead-data": "sim"}), "só aparece quando vale"),
    ("create_thing", loose_body, "o corpo (application/json) não aponta para um modelo com nome"),
    ("create_thing", loose_response, "a resposta 201 (application/json) não aponta"),
    ("list_things", bare_list, "a resposta 200 (application/json) não aponta"),
    ("create_thing", response("201", put(description="Successful Response")), "não tem descrição"),
    ("create_thing", remove_response("201"), "nenhuma resposta de sucesso"),
    ("list_things", remove_response("401"), "falta declarar o erro 401"),
    ("list_things", remove_response("429"), "falta declarar o erro 429"),
    ("create_thing", remove_response("403"), "falta declarar o erro 403 (as operações `total`)"),
    ("rename_thing", remove_response("404"), "falta declarar o erro 404"),
    ("rename_thing", remove_response("409"), "falta declarar o erro 409"),
    ("create_thing", remove_response("422"), "falta declarar o erro 422"),
    ("list_things", remove_response("422"), "falta declarar o erro 422"),
    ("list_things", response("401", put(content={})), "não usa o modelo ErrorResponse"),
    ("list_things", response("401", drop("headers")), "não declara o cabeçalho WWW-Authenticate"),
    ("list_things", response("429", drop("headers")), "não declara o cabeçalho Retry-After"),
    (
        "rename_thing",
        response("409", put(description="O recurso está num estado que não aceita.")),
        "fala de `version_conflict`",
    ),
    ("create_thing", put(responses={}), "nenhuma resposta de sucesso"),
]


def with_a_simulation(**changes: Any) -> dict[str, Any]:
    """O brinquedo com a criação que dispensa o `confirm` numa simulação (`dry_run`)."""
    document = toy()
    operation_of(document, "create_thing")["x-confirm-unless"] = "dry_run"
    schema = document["components"]["schemas"]["CreateThing"]["properties"]
    schema["dry_run"] = {"type": "boolean", "default": False, **changes}
    return document


def test_a_simulation_that_dispenses_the_confirm_follows_the_contract() -> None:
    assert contract_problems(with_a_simulation()) == []


@pytest.mark.parametrize(
    "changes",
    [{"type": "string"}, {"default": True}],
    ids=["not a boolean", "starts true"],
)
def test_the_field_that_dispenses_the_confirm_is_a_boolean_that_starts_false(
    changes: dict[str, Any],
) -> None:
    found = contract_problems(with_a_simulation(**changes))

    assert any("não é um campo booleano do corpo que começa falso" in problem for problem in found)


def test_the_dispensing_field_must_exist_in_the_body() -> None:
    document = with_a_simulation()
    del document["components"]["schemas"]["CreateThing"]["properties"]["dry_run"]

    found = contract_problems(document)

    assert any("aponta para «dry_run»" in problem for problem in found)


def test_an_operation_without_a_mark_cannot_dispense_a_confirmation() -> None:
    document = with_a_simulation()
    del operation_of(document, "create_thing")["x-external-effect"]

    found = contract_problems(document)

    assert any("`x-confirm-unless` numa operação sem marca" in problem for problem in found)


@pytest.mark.parametrize(("operation_id", "change", "message"), MUTATIONS)
def test_each_rule_of_the_contract_rejects_what_it_charges(
    operation_id: str, change: Callable[[dict[str, Any]], None], message: str
) -> None:
    document = toy()
    change(operation_of(document, operation_id))

    found = contract_problems(document)

    assert any(message in problem for problem in found), (message, found)


def with_a_lead_data_operation(*, leaks: str | None = None, marked: bool = True) -> dict[str, Any]:
    """O brinquedo com uma operação `read_thing`, que devolve o estado de uma coisa e o dado da lead no
    bloco `lead_data`. `leaks` põe um campo pessoal onde não pode (no estado); `marked=False` tira a
    marca x-lead-data."""
    document = toy()
    state: dict[str, Any] = {"status": {"type": "string"}}
    if leaks:
        state[leaks] = {"type": "string"}
    document["components"]["schemas"].update(
        {
            "ThingState": {"type": "object", "properties": state},
            "ThingLead": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "email": {"type": "string"}},
            },
            "ThingRead": {
                "type": "object",
                "properties": {
                    "state": {"$ref": f"{SCHEMAS}ThingState"},
                    "lead_data": {"anyOf": [{"$ref": f"{SCHEMAS}ThingLead"}, {"type": "null"}]},
                },
            },
            "ThingReadPage": {
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {"$ref": f"{SCHEMAS}ThingRead"}}
                },
            },
        }
    )
    read = copy.deepcopy(operation_of(document, "list_things"))
    read.update({"operationId": "read_thing", "summary": "Ler uma coisa"})
    if marked:
        read["x-lead-data"] = True
    read["responses"]["200"]["content"]["application/json"]["schema"] = {
        "$ref": f"{SCHEMAS}ThingReadPage"
    }
    document["paths"]["/things/read"] = {"get": read}
    return document


# Os campos de estado do brinquedo: tudo o que `read_thing` devolve fora de `lead_data`.
TOY_STATE = {"read_thing": frozenset({"items", "items.state", "items.state.status"})}


def toy_problems(document: dict[str, Any]) -> list[str]:
    return contract_problems(document, state_fields=TOY_STATE)


def test_the_personal_fields_inside_the_lead_data_block_follow_the_contract() -> None:
    assert toy_problems(with_a_lead_data_operation()) == []


@pytest.mark.parametrize(
    "field", ["name", "email", "phone", "answers", "scores", "consent", "plan"]
)
def test_a_personal_field_outside_the_lead_data_block_of_an_x_lead_data_operation_fails(
    field: str,
) -> None:
    found = "\n".join(toy_problems(with_a_lead_data_operation(leaks=field)))

    assert f"o campo pessoal «items.state.{field}» está fora do bloco `lead_data`" in found
    assert "GET /things/read" in found


@pytest.mark.parametrize(
    "field",
    [
        "persona",
        "priority",
        "notes",
        "note",
        "last_error",  # o último erro de uma entrega ou de uma sincronização
        "reason",  # o motivo de um reprocessamento
        "failure_detail",  # o detalhe da falha
        "full_name",
    ],
)
def test_what_comes_out_of_the_scoring_and_the_free_text_are_lead_data_too(field: str) -> None:
    found = toy_problems(with_a_lead_data_operation(leaks=field))

    assert any(f"«items.state.{field}» está fora do bloco `lead_data`" in p for p in found)


def test_an_operation_that_returns_the_lead_data_block_must_carry_the_x_lead_data_mark() -> None:
    found = toy_problems(with_a_lead_data_operation(marked=False))

    assert any("devolve o bloco `lead_data`, mas não é marcada x-lead-data" in p for p in found)


# --- o token só entra ---------------------------------------------------------------------------
# O token do Baserow (e qualquer outro `token*`) só vai numa requisição, e a requisição o declara
# `writeOnly`; nenhum modelo de resposta tem uma propriedade `token*`, a não ser as que dizem o estado
# dele (`TOKEN_STATE`). É a barreira do contrato: um campo `token` que alguém ponha num modelo de
# leitura reprova, qualquer que seja a operação.


def with_a_token(
    *, response_field: str | None = None, request_field: dict[str, Any] | None = None
) -> dict[str, Any]:
    """O brinquedo com uma propriedade `token` na resposta de `create_thing` (`response_field` é o
    nome) e/ou no corpo dele (`request_field` é o esquema da propriedade `token`)."""
    document = toy()
    schemas = document["components"]["schemas"]
    if response_field:
        schemas["Thing"]["properties"][response_field] = {"type": "string"}
    if request_field is not None:
        schemas["CreateThing"]["properties"]["token"] = request_field
    return document


WRITE_ONLY = {"type": "string", "writeOnly": True, "format": "password"}
WRITE_ONLY_OR_NULL = {"anyOf": [WRITE_ONLY, {"type": "null"}]}


@pytest.mark.parametrize("field", ["token", "token_value", "tokens", "token_sealed", "token_hash"])
def test_a_token_property_in_a_response_model_fails(field: str) -> None:
    found = "\n".join(contract_problems(with_a_token(response_field=field)))

    assert f"a resposta 201 devolve a propriedade «{field}»" in found
    assert "POST /things" in found


def test_a_token_property_nested_in_a_response_model_fails_too() -> None:
    document = with_a_token()
    document["components"]["schemas"]["ThingPage"]["properties"]["items"] = {
        "type": "array",
        "items": {"$ref": f"{SCHEMAS}Thing"},
    }
    document["components"]["schemas"]["Thing"]["properties"]["connection"] = {
        "type": "object",
        "properties": {"token": {"type": "string"}},
    }

    found = "\n".join(contract_problems(document))

    assert "a resposta 200 devolve a propriedade «items.connection.token»" in found


@pytest.mark.parametrize("field", ["token_set", "token_unreadable", "token_changed_at"])
def test_the_properties_that_only_say_the_state_of_the_token_are_fine_in_a_response(
    field: str,
) -> None:
    assert contract_problems(with_a_token(response_field=field)) == []


@pytest.mark.parametrize("request_field", [WRITE_ONLY, WRITE_ONLY_OR_NULL])
def test_a_write_only_token_in_a_request_is_fine(request_field: dict[str, Any]) -> None:
    assert contract_problems(with_a_token(request_field=request_field)) == []


@pytest.mark.parametrize(
    "request_field",
    [
        {"type": "string"},
        {"anyOf": [{"type": "string"}, {"type": "null"}]},
        {"type": "string", "writeOnly": False},
    ],
)
def test_a_token_in_a_request_that_is_not_write_only_fails(request_field: dict[str, Any]) -> None:
    found = "\n".join(contract_problems(with_a_token(request_field=request_field)))

    assert "o corpo (application/json) tem a propriedade «token» sem `writeOnly`" in found


def test_the_real_openapi_has_the_baserow_token_only_in_the_request_and_write_only(
    document: dict[str, Any],
) -> None:
    """A guarda olha o documento de verdade: o token do Baserow está no corpo de
    `connect_form_integration`, `writeOnly`, e as respostas só dizem o estado dele."""
    token = document["components"]["schemas"]["ConnectFormIntegrationRequest"]["properties"][
        "token"
    ]
    names = {
        path[-1]
        for _, _, operation in operations_of(document)
        for status, response in operation["responses"].items()
        if status.startswith("2")
        for media in response.get("content", {}).values()
        for path in _property_paths(document, media.get("schema", {}))
        if path[-1].startswith("token")
    }

    assert any(option.get("writeOnly") is True for option in token["anyOf"])
    assert names == TOKEN_STATE  # as respostas dizem só se há e se abre; o guarda olha o que existe


# --- o segredo sai uma vez ----------------------------------------------------------------------
# O segredo de um destino do webhook nasce no servidor e aparece só na resposta da criação e da troca
# (`SECRET_ONCE`). É a barreira do contrato: um campo `secret` que alguém ponha num modelo de leitura,
# ou num corpo de entrada, reprova, qualquer que seja a operação.


def with_a_secret(
    *,
    response_field: str | None = None,
    operation_id: str = "create_thing",
    role: str = "total",
    request_field: str | None = None,
) -> dict[str, Any]:
    """O brinquedo com uma propriedade `response_field` na resposta de `create_thing` (que pode se
    chamar `operation_id` e ter o papel `role`) e/ou `request_field` no corpo dele."""
    document = toy()
    schemas = document["components"]["schemas"]
    operation = document["paths"]["/things"]["post"]
    operation["operationId"] = operation_id
    operation["x-required-role"] = role
    if (
        response_field
    ):  # só a resposta desta operação ganha o campo: `rename_thing` segue com `Thing`
        schemas["CreatedThing"] = {
            "type": "object",
            "properties": {**schemas["Thing"]["properties"], response_field: {"type": "string"}},
        }
        operation["responses"]["201"]["content"]["application/json"]["schema"] = {
            "$ref": f"{SCHEMAS}CreatedThing"
        }
    if request_field:
        schemas["CreateThing"]["properties"][request_field] = {"type": "string"}
    return document


@pytest.mark.parametrize(
    "field", ["secret", "secret_value", "secrets", "secret_sealed", "secret_hash"]
)
def test_a_secret_property_in_the_response_of_an_ordinary_operation_fails(field: str) -> None:
    found = "\n".join(contract_problems(with_a_secret(response_field=field)))

    assert f"a resposta 201 devolve a propriedade «{field}»" in found
    assert "POST /things" in found


@pytest.mark.parametrize("operation_id", sorted(SECRET_ONCE))
def test_the_operations_that_make_a_secret_may_return_it_at_the_root_of_the_response(
    operation_id: str,
) -> None:
    assert (
        contract_problems(with_a_secret(response_field="secret", operation_id=operation_id)) == []
    )


@pytest.mark.parametrize("operation_id", sorted(SECRET_ONCE))
@pytest.mark.parametrize("field", ["secret_sealed", "secret_hash", "secret_value", "secrets"])
def test_those_operations_may_return_the_secret_and_nothing_else_called_secret(
    operation_id: str, field: str
) -> None:
    document = with_a_secret(response_field=field, operation_id=operation_id)

    found = "\n".join(contract_problems(document))

    assert f"a resposta 201 devolve a propriedade «{field}»" in found


def test_the_secret_nested_in_the_response_of_an_operation_that_makes_it_fails() -> None:
    document = with_a_secret(response_field="destination", operation_id="save_destination")
    document["components"]["schemas"]["CreatedThing"]["properties"]["destination"] = {
        "type": "object",
        "properties": {"secret": {"type": "string"}},
    }

    found = "\n".join(contract_problems(document))

    assert "a resposta 201 devolve a propriedade «destination.secret»" in found


def test_the_secret_in_a_read_operation_fails_even_if_it_has_the_name_of_one_that_makes_it() -> (
    None
):
    document = with_a_secret(
        response_field="secret", operation_id="save_destination", role="leitura"
    )

    found = "\n".join(contract_problems(document))

    assert "a resposta 201 devolve a propriedade «secret»" in found


@pytest.mark.parametrize("field", sorted(SECRET_STATE))
def test_the_properties_that_only_say_the_state_of_the_secret_are_fine_in_a_response(
    field: str,
) -> None:
    assert contract_problems(with_a_secret(response_field=field)) == []


@pytest.mark.parametrize("operation_id", ["create_thing", *sorted(SECRET_ONCE)])
@pytest.mark.parametrize("field", ["secret", "secret_value"])
def test_no_request_body_may_have_a_secret_property_not_even_the_operations_that_make_one(
    operation_id: str, field: str
) -> None:
    found = "\n".join(
        contract_problems(with_a_secret(request_field=field, operation_id=operation_id))
    )

    assert f"o corpo (application/json) tem a propriedade «{field}»" in found


def test_the_real_openapi_has_the_secret_only_in_the_two_responses_that_make_it(
    document: dict[str, Any],
) -> None:
    """A guarda olha o documento de verdade: `secret` está na raiz da resposta da criação e da troca
    do destino, as outras respostas dizem só o estado dele, e nenhum corpo o recebe."""
    roots: dict[str, set[str]] = {}  # a operação, e as propriedades `secret*` na raiz da resposta
    names: set[str] = set()  # o último nome de cada propriedade `secret*`, em qualquer fundura
    for _, _, operation in operations_of(document):
        for status, response in operation["responses"].items():
            if not status.startswith("2"):
                continue
            for media in response.get("content", {}).values():
                for path in _property_paths(document, media.get("schema", {})):
                    if path[-1].startswith("secret"):
                        names.add(path[-1])
                        if len(path) == 1:
                            roots.setdefault(operation["operationId"], set()).add(path[0])

    assert {op for op, found in roots.items() if "secret" in found} == set(SECRET_ONCE)
    assert names == {"secret", *SECRET_STATE}
    for _, _, operation in operations_of(document):
        for media in operation.get("requestBody", {}).get("content", {}).values():
            assert not [
                path
                for path in _property_paths(document, media.get("schema", {}))
                if path[-1].startswith("secret")
            ], operation["operationId"]


def test_a_field_called_name_is_fine_where_the_operation_does_not_carry_lead_data() -> None:
    """`ApiKey.name` é o nome de uma chave: o contrato só cobra o bloco das operações x-lead-data."""
    document = with_a_lead_data_operation()
    document["components"]["schemas"]["Thing"]["properties"]["name"] = {"type": "string"}

    assert toy_problems(document) == []


# --- os campos de estado de uma operação x-lead-data, travados -----------------------------------
# A lista de nomes pessoais acima barra o que se conhece; esta barra o que ninguém previu: o que uma
# operação x-lead-data devolve fora do `lead_data` é exatamente o que `LEAD_STATE` diz. Um campo novo
# no estado reprova até alguém incluí-lo ali, onde a revisão o vê e decide se é de estado ou da lead.


def test_a_new_field_in_the_state_of_an_x_lead_data_operation_fails_until_it_is_locked() -> None:
    document = with_a_lead_data_operation()
    document["components"]["schemas"]["ThingState"]["properties"]["origem"] = {"type": "string"}

    found = toy_problems(document)

    assert any(
        "o campo «items.state.origem» de read_thing não está em LEAD_STATE" in p for p in found
    )
    assert any("se é da lead, vai dentro de `lead_data`" in p for p in found)


def test_a_field_the_lock_lists_and_the_operation_no_longer_returns_fails_too() -> None:
    document = with_a_lead_data_operation()
    del document["components"]["schemas"]["ThingState"]["properties"]["status"]

    found = toy_problems(document)

    assert any(
        "«items.state.status» está em LEAD_STATE, mas read_thing já não o devolve" in p
        for p in found
    )


def test_an_x_lead_data_operation_without_a_lock_fails() -> None:
    found = contract_problems(with_a_lead_data_operation())  # o LEAD_STATE de verdade não a conhece

    assert any(
        "read_thing é x-lead-data e não tem os campos de estado travados em LEAD_STATE" in p
        for p in found
    )


def test_a_lock_for_an_operation_that_is_not_x_lead_data_or_not_in_the_api_fails() -> None:
    found = lock_problems(toy(), {"list_things": frozenset({"items"}), "sumiu": frozenset()})

    assert any("LEAD_STATE lista list_things, que não é x-lead-data" in p for p in found)
    assert any("LEAD_STATE lista sumiu, que a API não tem" in p for p in found)


def test_the_locked_state_fields_are_the_ones_of_the_real_x_lead_data_operations(
    document: dict[str, Any],
) -> None:
    marked = {
        operation["operationId"]
        for _, _, operation in operations_of(document)
        if operation.get("x-lead-data")
    }

    assert lock_problems(document, LEAD_STATE) == []
    assert marked == set(LEAD_STATE) >= {"list_jobs", "get_job", "download_job_pdf"}
    assert LEAD_STATE["download_job_pdf"] == frozenset()  # um arquivo: nenhum campo JSON


def test_a_file_response_must_be_declared_in_binary() -> None:
    document = toy()
    operation = operation_of(document, "list_things")
    operation["responses"]["200"]["content"] = {
        "application/pdf": {"schema": {"type": "string", "format": "binary"}}
    }
    assert contract_problems(document) == []

    operation["responses"]["200"]["content"] = {"application/pdf": {"schema": {"type": "string"}}}
    found = "\n".join(contract_problems(document))

    assert "a resposta 200 (application/pdf) não é binária" in found


def test_a_page_response_must_be_declared_as_text_and_not_as_a_binary_or_a_model() -> None:
    document = toy()
    operation = operation_of(document, "list_things")
    operation["responses"]["200"]["content"] = {"text/html": {"schema": {"type": "string"}}}
    assert contract_problems(document) == []

    operation["responses"]["200"]["content"] = {"text/html": {"schema": {"type": "integer"}}}
    found = "\n".join(contract_problems(document))

    assert "a resposta 200 (text/html) não é texto" in found


def test_a_synthetic_operation_without_role_without_summary_and_with_a_loose_dict_body_fails() -> (
    None
):
    """O caso que o ticket pede: sem `x-required-role`, sem `summary` e com corpo `dict` solto, tudo
    numa operação só, e a mensagem diz cada um e a operação."""
    document = toy()
    operation = operation_of(document, "create_thing")
    del operation["x-required-role"]
    del operation["summary"]
    loose_body(operation)

    found = "\n".join(contract_problems(document))

    assert "POST /things: sem `x-required-role`" in found
    assert "POST /things: sem `summary`" in found
    assert "POST /things: o corpo (application/json) não aponta para um modelo com nome" in found


def test_a_422_where_nothing_can_be_validated_fails() -> None:
    document = toy()
    operation = operation_of(document, "list_things")
    del operation["parameters"]  # sem parâmetro nem corpo: nada a validar

    found = contract_problems(document)

    assert any("declara 422, que não pode ocorrer" in problem for problem in found)


def test_a_409_of_an_operation_without_a_version_must_not_talk_about_the_version() -> None:
    document = toy()
    operation = operation_of(document, "rename_thing")
    del document["components"]["schemas"]["RenameThing"]["properties"]["expected_version"]
    operation["responses"]["409"]["description"] = "A versão esperada mudou (`version_conflict`)."

    found = contract_problems(document)

    assert any("sem versão não fala de `version_conflict`" in problem for problem in found)


@pytest.mark.parametrize(
    "description, valid",
    [
        (
            "Conflito de estado do vínculo: namespace ou formulário incompatível (`command_conflict`).",
            True,
        ),
        ("Conflito de estado do vínculo (`state_conflict`).", False),
        ("Escopo incompatível (`command_conflict`).", False),
        ("Conflito de estado do vínculo (`version_conflict`).", False),
    ],
)
def test_receipt_409_describes_its_state_conflict_without_inventing_a_wire_code(
    document, description, valid
):
    changed = copy.deepcopy(document)
    operation = operation_of(changed, "get_form_draft_test_command")
    operation["responses"]["409"]["description"] = description
    found = [p for p in contract_problems(changed) if "409" in p]
    assert (found == []) is valid


def test_a_marked_operation_must_declare_the_confirm_field() -> None:
    document = toy()
    del document["components"]["schemas"]["CreateThing"]["properties"]["confirm"]

    assert any(
        "não declara o campo `confirm`" in problem for problem in contract_problems(document)
    )


def test_a_model_that_is_an_open_object_is_a_loose_dict() -> None:
    document = toy()
    document["components"]["schemas"]["Thing"] = {"type": "object", "additionalProperties": True}

    found = contract_problems(document)

    assert any("o modelo Thing é um objeto aberto" in problem for problem in found)


def test_a_closed_empty_model_is_not_a_loose_dict() -> None:
    document = toy()
    document["components"]["schemas"]["Thing"] = {"type": "object", "additionalProperties": False}

    assert contract_problems(document) == []


def test_a_duplicated_operation_id_fails() -> None:
    document = toy()
    document["paths"]["/things"]["post"]["operationId"] = "list_things"

    assert any("`operation_id` list_things repetido" in p for p in contract_problems(document))


# Sorteados a cada execução: nenhum literal com cara de segredo entra no repositório (o GitGuardian do
# PR acusa), e a forma que o teste cobra é a que o sorteio monta.
@pytest.mark.parametrize(
    "make_secret",
    [
        lambda: f"gp_{secrets.token_hex(4)}_{secrets.token_urlsafe(32)}",  # uma chave de API
        lambda: f"eyJ{secrets.token_urlsafe(12)}.{secrets.token_urlsafe(12)}",  # um JWT
        lambda: "Aa1" + secrets.token_urlsafe(32),  # base64 comprido, com as três classes
        lambda: "a1" + secrets.token_hex(15),  # hexadecimal comprido, com número e letra
    ],
    ids=["api-key", "jwt", "base64", "hex"],
)
def test_an_example_that_looks_like_a_secret_fails(make_secret: Callable[[], str]) -> None:
    secret = make_secret()
    for where in ("example", "examples", "default", "enum"):
        document = toy()
        value: Any = [secret] if where in ("examples", "enum") else secret
        document["components"]["schemas"]["Thing"]["properties"]["name"][where] = value

        assert any("cara de segredo" in p for p in contract_problems(document)), where


def test_a_whole_api_key_in_a_description_fails_but_the_placeholder_does_not() -> None:
    document = toy()
    document["paths"]["/things"]["get"]["description"] = "Use gp_<id>_<segredo> no cabeçalho."
    assert contract_problems(document) == []

    document["paths"]["/things"]["get"]["description"] = (
        f"Use gp_{secrets.token_hex(4)}_{secrets.token_urlsafe(32)}"
    )
    assert any("chave de API" in problem for problem in contract_problems(document))


@pytest.mark.parametrize(
    "text",
    [
        str(uuid.uuid4()),  # um UUID é hexadecimal com hífens, e um id público
        "meu-formulario-de-diagnostico-contabil-para-escritorios-2027",  # slug com hífen
        "https://receptor.example.test/planos/webhook/entrada-de-leads-do-escritorio-2027",
        "a" * 40,  # comprido, mas de uma classe só
        "ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "Quantos itens por página (de 1 a 200; padrão 50), na ordem em que a lista chega.",
        "steps.1.questions.0.options.2.show_when.rules.0.operator",
    ],
    ids=["uuid", "slug", "url", "one-class", "upper-only", "sentence", "field-path"],
)
def test_an_example_that_only_looks_long_is_not_a_secret(text: str) -> None:
    assert not looks_like_a_secret(text)
    document = toy()
    document["components"]["schemas"]["Thing"]["properties"]["name"]["examples"] = [text]

    assert contract_problems(document) == []


def test_the_secret_filter_needs_a_mix_of_classes_not_just_a_length() -> None:
    assert looks_like_a_secret("Aa1" + "b" * 40)  # minúscula, maiúscula e número
    assert not looks_like_a_secret("Aa" + "b" * 40)  # sem número
    assert looks_like_a_secret("0f" * 20)  # hexadecimal com número e letra
    assert not looks_like_a_secret("f" * 40)  # hexadecimal sem número
    assert looks_like_a_secret("veja Aa1" + "z" * 40 + " no cabeçalho")  # no meio de uma frase


def test_examples_that_are_plain_text_pass() -> None:
    document = toy()
    properties = document["components"]["schemas"]["Thing"]["properties"]
    properties["name"]["examples"] = [
        "painel-de-vendas",
        "2027-04-15T12:00:00Z",
        "gp_<id>_<segredo>",
    ]
    properties["default"] = {"type": "string"}  # um campo chamado `default` não é um valor padrão

    assert contract_problems(document) == []


# --- o teste de eco -----------------------------------------------------------------------------

# Um valor que nenhuma resposta de erro pode devolver, sorteado a cada execução. Maiúsculas e
# sublinhado: nenhum campo da API o aceita como válido (nome, papel, data, id), então ele chega nas
# mensagens de validação, que é onde um valor recebido costuma escapar.
SENTINEL = "ZZ_SENT_" + secrets.token_hex(8).upper()


def resolved(document: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    ref = str(schema.get("$ref", ""))
    if ref.startswith(SCHEMAS):
        return resolved(document, document["components"]["schemas"][ref.removeprefix(SCHEMAS)])
    return schema


def filled(
    document: dict[str, Any],
    schema: dict[str, Any],
    name: str = "",
    seen: frozenset[str] = frozenset(),
) -> Any:
    """O valor de um esquema com o sentinela em cada campo (o `confirm` vai `true`, para a validação
    do corpo não parar antes na falta dele). Um modelo que cita a si mesmo (o plano de um
    formulário tem campos dentro de campos) para na segunda volta: `seen` são os modelos do caminho."""
    ref = str(schema.get("$ref", ""))
    if ref.startswith(SCHEMAS):
        if ref in seen:
            return None
        seen = seen | {ref}
    schema = resolved(document, schema)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [item for item in schema[key] if item.get("type") != "null"]
            return filled(document, options[0], name, seen) if options else None
    if "allOf" in schema:
        return filled(document, schema["allOf"][0], name, seen)
    if schema.get("type") == "object" or "properties" in schema:
        properties = schema.get("properties")
        if properties:
            return {key: filled(document, value, key, seen) for key, value in properties.items()}
        return {SENTINEL: SENTINEL}
    if schema.get("type") == "array":
        return [filled(document, schema.get("items", {}), name, seen)]
    return True if name == "confirm" else SENTINEL


def send_sentinels(
    client: ApiClient, document: dict[str, Any], method: str, path: str, operation: dict[str, Any]
) -> httpx.Response:
    """Chama a operação com o sentinela em todo parâmetro (caminho e query) e em todo campo do
    corpo, com a chave `total` do cliente."""
    url, query = f"{API}{path}", {}
    for parameter in operation.get("parameters", []):
        name = parameter["name"]
        if parameter["in"] == "path":
            url = url.replace("{" + name + "}", SENTINEL)
        elif parameter["in"] == "query":
            query[name] = "true" if name == "confirm" else SENTINEL
    kwargs: dict[str, Any] = {}
    content = operation.get("requestBody", {}).get("content", {})
    if "application/json" in content:
        kwargs["json"] = filled(document, content["application/json"]["schema"])
    elif "multipart/form-data" in content:
        schema = resolved(document, content["multipart/form-data"]["schema"])
        properties = schema.get("properties", {})
        files = {
            k: ("f.bin", SENTINEL.encode())
            for k, v in properties.items()
            if v.get("format") == "binary"
            or v.get("contentMediaType") == "application/octet-stream"
        }
        kwargs["files"] = files or None
        kwargs["data"] = {k: SENTINEL for k in properties if k not in files}
    return client.request(method, url, params=query, **kwargs)


class Leaky(ApiInput):
    label: str

    @field_validator("label")
    @classmethod
    def _check(cls, value: str) -> str:
        raise ValueError(f"o texto {value} não vale")


class Done(BaseModel):
    done: bool


def echoed(response: httpx.Response) -> bool:
    return SENTINEL in response.text or any(
        SENTINEL in value for value in response.headers.values()
    )


OPERATIONS = [
    pytest.param(method, path, id=operation["operationId"])
    for method, path, operation in operations_of(built_openapi())
]


@pytest.mark.parametrize(("method", "path"), OPERATIONS)
def test_no_error_response_gives_back_a_value_that_was_sent(
    api_client: Callable[..., ApiClient], method: str, path: str
) -> None:
    """O Pydantic repassa a mensagem de um `value_error` (a de um validador nosso), e uma mensagem
    escrita com o valor o devolveria. Todo campo de todo corpo, parâmetro e caminho do OpenAPI vai com
    o sentinela, e ele não pode voltar em nenhuma resposta de erro."""
    document = built_openapi()
    operation = next(op for m, p, op in operations_of(document) if (m, p) == (method, path))

    response = send_sentinels(api_client("total"), document, method, path, operation)

    if response.status_code >= 400:
        assert not echoed(response), (
            f"{operation['operationId']}: {response.status_code} devolve o valor"
        )


def test_the_sentinel_does_reach_the_validation_and_the_lookup_of_the_operations(
    api_client: Callable[..., ApiClient],
) -> None:
    """O teste de eco não pode passar por não ter olhado: o sentinela chega à validação do corpo, da
    query e à busca do caminho."""
    document = built_openapi()
    client = api_client("total")
    sent = {
        operation["operationId"]: send_sentinels(client, document, method, path, operation)
        for method, path, operation in operations_of(document)
    }

    create = sent["create_api_key"]
    assert create.status_code == 422
    assert {item["field"] for item in create.json()["errors"]} >= {"role", "expires_at"}
    assert sent["list_api_keys"].status_code == 422
    assert "limit" in {e["field"] for e in sent["list_api_keys"].json()["errors"]}
    assert sent["revoke_api_key"].status_code == 404  # o id do caminho foi buscado
    assert sent["get_current_api_key"].status_code == 200  # nada a enviar


def test_the_echo_test_does_catch_a_validator_that_writes_the_value_in_its_message(
    api_client: Callable[..., ApiClient],
) -> None:
    """O teste do teste: uma operação de prova cujo validador faz `raise ValueError(f"... {valor}")`
    devolve o sentinela, e `send_sentinels` e `echoed` o pegam."""

    router = OperationRouter(tag="Prova", description="Operação de prova do teste de eco.")

    @router.operation(
        "POST",
        "/probe/leaky",
        operation_id="probe_leaky",
        role="total",
        summary="Vaza",
        description="Um validador que escreve o valor na mensagem.",
        returns="Nada.",
        response_model=Done,
    )
    def leaky(body: Leaky) -> Done:
        return Done(done=True)

    client = api_client("total")
    add_routes(client, router)
    document = admin_api(client).openapi()
    operation = operation_of(document, "probe_leaky")

    response = send_sentinels(client, document, "POST", "/probe/leaky", operation)

    assert response.status_code == 422
    assert echoed(response)  # o valor voltou: o teste de eco reprovaria esta operação


def test_the_authorization_header_with_the_sentinel_is_not_echoed_either(
    api_client: Callable[..., ApiClient],
) -> None:
    client = api_client("total")

    response = client.get(f"{API}/me", headers={"Authorization": f"Bearer {SENTINEL}"})

    assert response.status_code == 401 and not echoed(response)
