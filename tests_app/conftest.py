"""Testes do app novo (pytest). Precisam de um Postgres: `TEST_DATABASE_URL`, cujo banco deve
terminar em `_test` (as tabelas são apagadas entre os testes).

Serial ou em paralelo:

- Serial (`python -m pytest -q`, ou `-n 0`): um processo, o banco de `TEST_DATABASE_URL`, como sempre.
- Em paralelo (`python -m pytest -q -n auto --dist loadgroup`, pytest-xdist): cada worker usa o seu
  banco, porque a suíte recria o schema e esvazia as tabelas, e dois workers no mesmo banco se
  atropelariam. `<nome>_test` vira `<nome>_<worker>_test` (`mcc_app_test` -> `mcc_app_gw0_test`,
  `mcc_app_gw1_test`...), com o sufixo `_test` que a proteção exige. O banco do worker é criado na
  primeira vez, pelo banco de manutenção `postgres` do mesmo servidor, então o usuário da URL precisa
  poder criar bancos (`CREATEDB`); nas vezes seguintes ele é reaproveitado, e o schema é refeito do
  zero a cada execução. Os bancos `*_gw*_test` ficam no servidor: apague-os com `DROP DATABASE`
  quando não precisar mais deles.
- `--dist loadgroup` obedece o `xdist_group`: os testes de um mesmo grupo vão para o mesmo worker, em
  série (hoje só o e2e do Hatchet, que divide o tenant do Hatchet Cloud).

Os outros estados dos testes não se cruzam entre workers: o que vive no processo (o cache das
imagens, o `Settings`, o `deps` global) é de cada worker; arquivos usam `tmp_path`, portas usam a
porta 0, e o ambiente só muda por `monkeypatch`. O que é de fora do processo e do banco (o tenant
do Hatchet Cloud) leva `xdist_group`. Sessões do banco ficam em `with deps.sessions() as session:`:
uma esquecida aberta trava o `TRUNCATE` do teste seguinte (ver `DatabaseReset`).

Para a suíte levar minutos e não dezenas deles, cada worker guarda entre um teste e o seguinte o que é
caro de refazer e não muda: o engine do banco, o `FormSpec` de cada definição compilada e algumas
sub-aplicações da API de admin montadas. Cada bloco abaixo diz o que o torna seguro e qual teste o
prova (`test_database_reset.py`, `test_compile_memo.py`, `test_admin_api_reuse.py`); `CONTRIBUTING.md`,
"Testes e lint", diz o que isso pede de quem escreve teste.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from fastapi import FastAPI

BASE_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://mcc@127.0.0.1:55433/mcc_app_test"
)
# Chave do advisory lock que serializa o CREATE DATABASE dos workers (qualquer inteiro fixo serve).
CREATE_DATABASE_LOCK = 7_243_001


def worker_database_url(base_url: str, worker: str | None) -> str:
    """O banco de teste do worker do xdist (`gw0`, `gw1`...): `<nome>_test` vira `<nome>_<worker>_test`.
    Sem worker (suíte serial), a URL volta como veio. Uma URL cujo banco não termina em `_test` também
    volta igual, para a proteção do `database_url` recusá-la."""
    if not worker:
        return base_url
    url = sa.engine.make_url(base_url)
    name = url.database or ""
    if not name.endswith("_test"):
        return base_url
    renamed = url.set(database=f"{name.removesuffix('_test')}_{worker}_test")
    return renamed.render_as_string(hide_password=False)


# O xdist põe `PYTEST_XDIST_WORKER` no ambiente do worker antes de carregar este arquivo.
TEST_DATABASE_URL = worker_database_url(BASE_DATABASE_URL, os.environ.get("PYTEST_XDIST_WORKER"))


def create_database_if_missing(url: str) -> None:
    """Cria o banco da URL se ele não existe, conectando ao banco de manutenção `postgres` do mesmo
    servidor. Os workers criam os seus ao mesmo tempo; o advisory lock os põe em fila, porque o
    Postgres copia o `template1` a cada CREATE DATABASE e não gosta de pressa."""
    target = sa.engine.make_url(url)
    name = target.database or ""
    if not re.fullmatch(r"\w+", name):
        raise RuntimeError(f"nome de banco de teste inesperado: {name!r}")
    maintenance = sa.create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    lock = {"key": CREATE_DATABASE_LOCK}
    try:
        with maintenance.connect() as connection:
            connection.execute(sa.text("select pg_advisory_lock(:key)"), lock)
            try:
                exists = connection.execute(
                    sa.text("select 1 from pg_database where datname = :name"), {"name": name}
                ).scalar()
                if not exists:
                    connection.execute(sa.text(f'create database "{name}"'))
            finally:
                connection.execute(sa.text("select pg_advisory_unlock(:key)"), lock)
    except sa.exc.DBAPIError as error:
        raise RuntimeError(
            f"não consegui criar o banco {name!r} do worker: em paralelo (pytest-xdist) o usuário de "
            "TEST_DATABASE_URL precisa poder criar bancos (CREATEDB) e conectar ao banco `postgres`. "
            "Sem isso, rode a suíte serial (sem `-n`)."
        ) from error
    finally:
        maintenance.dispose()


def _fake_hatchet_token() -> str:
    """Token com formato JWT para construir o cliente do Hatchet sem rede. Não é credencial."""

    def b64(data: dict[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    claims = {
        "sub": "00000000-0000-0000-0000-000000000001",
        "server_url": "http://localhost:8888",
        "grpc_broadcast_address": "localhost:7077",
    }
    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}.offline"


os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["TOKEN_SECRETS"] = "test-secret-0123456789abcdefghijklmnopqrstuvwxyz"
os.environ["LLM_MODE"] = "fake"
os.environ["PDF_MODE"] = "fake"
os.environ["STORAGE_MODE"] = "local"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
# Chave sorteada a cada execução: nada com cara de segredo entra no repositório. O worker do teste
# e2e herda esta, como herda o resto do ambiente.
os.environ["WEBHOOK_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
for name in ("WEBHOOK_URL", "WEBHOOK_SECRET", "RECONCILE_CRON", "RETENTION_CRON"):
    os.environ.pop(name, None)
if os.environ.get("HATCHET_E2E") != "1":
    os.environ["HATCHET_CLIENT_TOKEN"] = _fake_hatchet_token()
    os.environ["HATCHET_CLIENT_TLS_STRATEGY"] = "none"

from app import destinations
from app.adapters.assets import SHARED_CACHE
from app.adapters.llm import FakePlanGenerator
from app.adapters.pdf import FakePdfRenderer
from app.adapters.storage import LocalStorage
from app.adapters.webhook import WebhookTransport
from app.admin.api.application import bind_admin_api, build_admin_api
from app.api import main as api_main
from app.db import make_engine, make_session_factory
from app.deps import Deps, override_deps
from app.destinations import SecretCipher
from app.forms import FIRST_VERSION, FormSpec, UnknownForm
from app.forms.definition import compile_definition, utc_now
from app.forms.definition import compiler as definition_compiler
from app.forms.stored import StoredRegistry
from app.integrations.baserow import BaserowTransport
from app.settings import Settings
from app.tokens import TokenSigner

DEMO_ANSWERS: dict[str, Any] = {
    "email": " Contadora@Example.test ",
    "nome": "Contadora Exemplo",
    "escritorio": "Escritório Exemplo",
    "tamanho_equipe": "2_a_5",
    "principal_desafio": "captacao",
    "objetivo_90_dias": "Organizar a captação de clientes e medir a conversão.",
}
DEMO_CONSENT: dict[str, Any] = {"privacy": True, "text_version": "demo-v1"}
TABLES = (
    "journey_events, event_credentials, event_sources, "
    "plan_releases, handoff_references, integration_credentials, handoff_receivers, audit_log, api_keys, integration_syncs, integrations, deliveries, artifacts, plans, jobs, "
    "partial_deliveries, partial_captures, capture_receipts, submissions, leads, webhook_destinations, webhook_env_seed, draft_test_commands, draft_tests, form_drafts, "
    "form_version_assets, form_assets, form_versions, forms"
)
# Destino padrão dos testes que só precisam de "há webhook": o domínio .invalid nunca resolve.
RECORDED_DESTINATION = "https://receptor.invalid/planos"


@pytest.fixture(autouse=True)
def empty_asset_cache() -> None:
    """O cache das imagens dos formulários é do processo: um teste não herda o de outro, que mascararia
    um arquivo que o storage já não tem."""
    SHARED_CACHE.clear()


# --- a compilação das definições, uma vez por processo -----------------------------------------------
#
# `compile_definition` é uma função pura (a definição, a versão e o relógio dão o `FormSpec`), e a
# suíte a chama uns 9000 vezes, quase sempre para as mesmas definições: o registro de cada teste
# compila de novo o demo, o MCC e o reforma-2027, e `legacy_spec` também. O Jinja do ambiente isolado
# é 3/4 do custo (uns 7 ms por chamada, 68 s da suíte serial). Os testes compilam cada definição uma vez
# por processo e dividem o `FormSpec`, como o registro do app divide entre os pedidos. Quem erra não
# fica no cache (a mesma definição reprova de novo, com os problemas), e quem passa um relógio seu
# (as fases com datas) compila sempre.
_COMPILED: dict[tuple[str, int], FormSpec] = {}
_COMPILED_DOCUMENTS: dict[tuple[str, int, str], dict[str, Any]] = {}


def memoized_compile(original: Callable[..., FormSpec]) -> Callable[..., FormSpec]:
    def compile_once(
        document: Any, *, version_number: int = FIRST_VERSION, clock: Any = utc_now
    ) -> FormSpec:
        if isinstance(document, bytes | str):
            try:
                as_text = document.decode() if isinstance(document, bytes) else document
            except UnicodeDecodeError:
                # O parser público conserva o erro de domínio para bytes sem UTF-8.
                return original(document, version_number=version_number, clock=clock)
        else:
            # `sort_keys`: o JSONB do banco devolve as chaves de um objeto noutra ordem que o arquivo, e
            # a ordem delas não é do documento (o banco nem a guarda); a das listas é, e fica na chave.
            as_text = json.dumps(document, sort_keys=True, ensure_ascii=False, default=str)
        if clock is not utc_now:
            spec = original(document, version_number=version_number, clock=clock)
        else:
            key = (hashlib.sha256(as_text.encode()).hexdigest(), version_number)
            if key not in _COMPILED:
                _COMPILED[key] = original(document, version_number=version_number, clock=clock)
            spec = _COMPILED[key]
        _COMPILED_DOCUMENTS[(spec.slug, spec.version_number, spec.version)] = json.loads(as_text)
        return spec

    compile_once.__wrapped__ = original  # type: ignore[attr-defined]
    return compile_once


@pytest.fixture(scope="session", autouse=True)
def compile_each_definition_once() -> Iterator[None]:
    """Troca o `compile_definition` pelo que compila cada definição uma vez, em todo módulo que o
    importou pelo nome (o app, o `conftest` e os testes); os que o importam depois o recebem do pacote."""
    original = definition_compiler.compile_definition
    replacement = memoized_compile(original)
    patched = []
    for module in list(sys.modules.values()):
        if getattr(module, "compile_definition", None) is original:
            module.compile_definition = replacement
            patched.append(module)
    yield
    for module in patched:
        module.compile_definition = original
    _COMPILED.clear()


# --- a sub-aplicação da API de admin, montada uma vez por processo ----------------------------------
#
# `create_app` monta a API de admin (`build_admin_api`): o FastAPI monta o esquema de cada operação, e
# isso leva de 40 a 80 ms de CPU, pagos em mais de mil testes (todos os que criam o app). As rotas da
# sub-aplicação só leem `request.app.state`, então os testes montam algumas poucas por processo e, a
# cada `create_app`, religam o estado (`bind_admin_api`, o relógio, os disparadores, o balde de uso...)
# de uma que esteja livre. Um teste que cria dois apps (a chave `leitura` e a `total`, cada uma com o
# relógio dela) leva duas, e não divide o estado; a sub-aplicação volta para a fila quando o teste
# acaba. O que um teste mudou na montagem (uma rota de prova, um tratador) a tira da fila: ela não
# é reaproveitada, e o próximo teste monta outra.
_IDLE_ADMIN_APIS: list[tuple[FastAPI, tuple[Any, ...]]] = []
# Cada emprestada leva também os nomes do estado que o `bind_admin_api` deu a ela: o que a rota
# guardar depois em `app.state` (o texto do `/openapi.json`, que `reference` guarda ali) é do teste.
_LENT_ADMIN_APIS: list[tuple[FastAPI, tuple[Any, ...], frozenset[str]]] = []


def _assembly_of(api: FastAPI) -> tuple[Any, ...]:
    """O que `build_admin_api` montou, para saber se um teste mexeu: as rotas, o middleware, os
    tratadores de erro e as dependências trocadas, cada um por identidade."""
    return (
        tuple(map(id, api.router.routes)),
        tuple(map(id, api.user_middleware)),
        tuple((key, id(handler)) for key, handler in api.exception_handlers.items()),
        tuple(api.dependency_overrides),
    )


def lend_admin_api(**state: Any) -> FastAPI:
    """O `create_admin_api` que o `create_app` dos testes usa: uma sub-aplicação montada, livre, com o
    estado religado; se não há nenhuma livre, monta uma."""
    if _IDLE_ADMIN_APIS:
        api, assembly = _IDLE_ADMIN_APIS.pop()
    else:
        api = build_admin_api()
        assembly = _assembly_of(api)
    bind_admin_api(api, **state)
    _LENT_ADMIN_APIS.append((api, assembly, frozenset(api.state._state)))
    return api


def take_back_admin_apis() -> None:
    """Devolve à fila as sub-aplicações emprestadas que continuam como foram montadas; as que o teste
    mexeu somem. O que ficou guardado nelas se descarta: o esquema (`openapi()`) e o que as rotas
    puseram em `app.state` depois do `bind_admin_api` (o texto do `/openapi.json`), porque o teste
    pode ter mudado o documento, e o próximo não pode herdá-lo."""
    while _LENT_ADMIN_APIS:
        api, assembly, bound = _LENT_ADMIN_APIS.pop()
        if _assembly_of(api) == assembly:
            api.openapi_schema = None
            for name in set(api.state._state) - bound:
                del api.state._state[name]
            _IDLE_ADMIN_APIS.append((api, assembly))


@pytest.fixture(autouse=True)
def reuse_admin_apis(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(api_main, "create_admin_api", lend_admin_api)
    yield
    take_back_admin_apis()


def _test_database_name(url: str) -> str:
    """Valida o alvo antes de abrir uma conexão, sem repetir a DSN em erros."""
    try:
        target = sa.engine.make_url(url)
    except (sa.exc.ArgumentError, ValueError):
        raise RuntimeError(
            "TEST_DATABASE_URL precisa apontar para um banco de teste válido"
        ) from None
    name = target.database or ""
    if target.get_backend_name() != "postgresql" or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*_test", name
    ):
        raise RuntimeError(
            "TEST_DATABASE_URL precisa apontar para um banco de teste terminado em _test"
        )
    return name


def reset_test_schema(url: str) -> None:
    """Recria somente public de um banco de teste confirmado pela conexão.

    Não chama downgrade nem modifica o guard de migrations da aplicação. A baseline continua
    validando o banco vazio pelo caminho normal de upgrade.
    """
    expected = _test_database_name(url)
    engine = make_engine(url)
    try:
        with engine.begin() as connection:
            actual = connection.exec_driver_sql("SELECT current_database()").scalar_one()
            if actual != expected or not actual.endswith("_test"):
                raise RuntimeError("o banco conectado não é o banco de teste solicitado")
            connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    _test_database_name(TEST_DATABASE_URL)
    if TEST_DATABASE_URL != BASE_DATABASE_URL:  # worker do xdist: o banco dele pode não existir
        create_database_if_missing(TEST_DATABASE_URL)
    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parents[1] / "app" / "alembic.ini"))
    reset_test_schema(TEST_DATABASE_URL)
    command.upgrade(config, "head")
    engine = make_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalars().all() == ["0021_partial_capture"]
    finally:
        engine.dispose()
    return TEST_DATABASE_URL


class RecordingWebhook:
    """Transporte do webhook que grava (evento, id, payload) em vez de mandar."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.urls: list[str] = []
        self.error = error

    def send(
        self, url: str, secret: str, event: str, event_id: str, payload: dict[str, Any]
    ) -> int:
        self.calls.append((event, event_id, payload))
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return 200


def record_webhook(deps: Deps, error: Exception | None = None) -> RecordingWebhook:
    """Liga a entrega como o WEBHOOK_URL ligava antes do destino no admin: um destino padrão no
    banco, num host que não existe, e os envios gravados no transporte, sem rede."""
    webhook = RecordingWebhook(error)
    deps.webhook = webhook  # type: ignore[assignment]
    with deps.sessions.begin() as session:
        if None not in destinations.listing(session):
            destinations.save(
                session,
                deps.cipher,
                form_slug=None,
                purpose="internal",
                url=RECORDED_DESTINATION,
                actor="testes",
                expected_version=0,
                production=False,
                http_confirmed=False,
            )
    return webhook


# --- o Baserow de mentira ------------------------------------------------------------------------

# O token das integrações dos testes, sorteado a cada execução: nada com cara de segredo entra no
# repositório. Os testes conferem que ele nunca aparece em log, auditoria, tela nem banco.
BASEROW_TOKEN = secrets.token_hex(16)
BASEROW_URL = "https://baserow.example.test"
BASEROW_TABLE = 7


def no_baserow() -> BaserowTransport:
    """O Baserow dos testes que não o usam: recusa qualquer chamada, para a rede nunca ser tocada."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"chamada ao Baserow sem o Baserow de mentira: {request.url}")

    return BaserowTransport(client=httpx.Client(transport=httpx.MockTransport(refuse)))


def baserow_field(
    field_id: int,
    name: str,
    kind: str = "text",
    *,
    options: tuple[str, ...] = (),
    primary: bool = False,
    read_only: bool = False,
) -> dict[str, Any]:
    """Um campo como a API do Baserow o descreve; as opções de seleção têm id `field_id * 100 + n`."""
    described: dict[str, Any] = {
        "id": field_id,
        "table_id": BASEROW_TABLE,
        "name": name,
        "order": field_id,
        "type": kind,
        "primary": primary,
        "read_only": read_only,
    }
    if kind in ("single_select", "multiple_select"):
        described["select_options"] = [
            {"id": field_id * 100 + n, "value": value, "color": "blue"}
            for n, value in enumerate(options, start=1)
        ]
    return described


def demo_baserow_fields() -> list[dict[str, Any]]:
    """Uma tabela para o formulário demo: um campo de cada tipo que a integração preenche, e um
    somente leitura. As opções de seleção são os rótulos do demo."""
    return [
        baserow_field(101, "Nome", primary=True),
        baserow_field(102, "E-mail", "email"),
        baserow_field(103, "Telefone", "phone_number"),
        baserow_field(104, "Código do pedido"),
        baserow_field(105, "Respostas", "long_text"),
        baserow_field(106, "PDF", "url"),
        baserow_field(107, "Consentiu", "boolean"),
        baserow_field(
            108,
            "Equipe",
            "single_select",
            options=("Só eu", "De 2 a 5 pessoas", "De 6 a 15 pessoas", "Mais de 15 pessoas"),
        ),
        baserow_field(
            109,
            "Desafio",
            "single_select",
            options=(
                "Atrair novos clientes",
                "Transformar propostas em contratos",
                "Organizar a operação e as entregas",
                "Cuidar da gestão financeira e da equipe",
            ),
        ),
        baserow_field(110, "Criado em", "created_on", read_only=True),
    ]


class FakeBaserow:
    """Um Baserow de mentira, em memória, sobre `httpx.MockTransport`: os campos, as linhas, o
    filtro `filter__field_<id>__equal` e a validação que o Baserow faz (o token, o id de cada
    opção de seleção, um e-mail com `@`). `calls` guarda (método, caminho) de cada pedido;
    `fail(...)` enfileira uma resposta ou um erro de rede para o próximo pedido que bater;
    `before` roda antes de cada pedido (para os testes de concorrência)."""

    def __init__(
        self,
        fields: list[dict[str, Any]] | None = None,
        *,
        token: str = BASEROW_TOKEN,
        table_id: int = BASEROW_TABLE,
    ) -> None:
        self.fields = fields if fields is not None else demo_baserow_fields()
        self.token = token
        self.table_id = table_id
        self.rows: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.requests: list[httpx.Request] = []
        self.failures: list[tuple[str, str, httpx.Response | Exception]] = []
        self.before: Callable[[httpx.Request], None] | None = None
        self._next_id = 1
        self._lock = threading.Lock()

    def transport(self) -> BaserowTransport:
        return BaserowTransport(client=httpx.Client(transport=httpx.MockTransport(self)))

    def install(self, deps: Deps) -> FakeBaserow:
        deps.baserow = self.transport()
        return self

    def fail(
        self, method: str, outcome: int | Exception, *, where: str = "", body: Any = None
    ) -> None:
        """O próximo pedido `method` cujo caminho tem `where` recebe este status (com o corpo
        `body`, em JSON) ou levanta este erro de rede."""
        response = (
            outcome
            if isinstance(outcome, Exception)
            else httpx.Response(outcome, json=body if body is not None else {})
        )
        self.failures.append((method, where, response))

    def calls_to(self, method: str, where: str = "") -> list[str]:
        return [path for found, path in self.calls if found == method and where in path]

    # -- o servidor ----------------------------------------------------------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.before is not None:
            self.before(request)
        with self._lock:
            return self._handle(request)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/database/")
        self.requests.append(request)
        self.calls.append((request.method, path))
        for index, (method, where, outcome) in enumerate(self.failures):
            if method == request.method and where in path:
                del self.failures[index]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        if request.headers.get("Authorization") != f"Token {self.token}":
            return httpx.Response(401, json={"error": "ERROR_TOKEN_DOES_NOT_EXIST", "detail": ""})
        match = re.fullmatch(r"(fields|rows)/table/(\d+)/(?:(\d+)/)?", path)
        if match is None or int(match.group(2)) != self.table_id:
            return httpx.Response(404, json={"error": "ERROR_TABLE_DOES_NOT_EXIST", "detail": ""})
        kind, row_id = match.group(1), match.group(3)
        if kind == "fields" and request.method == "GET":
            return httpx.Response(200, json=self.fields)
        if kind == "rows" and row_id is None and request.method == "GET":
            return self._find(request)
        if kind == "rows" and row_id is None and request.method == "POST":
            return self._write(None, json.loads(request.content))
        if kind == "rows" and row_id is not None and request.method == "PATCH":
            if int(row_id) not in self.rows:
                return httpx.Response(404, json={"error": "ERROR_ROW_DOES_NOT_EXIST"})
            return self._write(int(row_id), json.loads(request.content))
        return httpx.Response(405, json={})

    def _find(self, request: httpx.Request) -> httpx.Response:
        results = list(self.rows.values())
        for name, value in request.url.params.items():
            filtered = re.fullmatch(r"filter__(field_\d+)__equal", name)
            if filtered:
                results = [row for row in results if row.get(filtered.group(1)) == value]
        size = int(request.url.params.get("size", 100))
        page = {"count": len(results), "next": None, "previous": None, "results": results[:size]}
        return httpx.Response(200, json=page)

    def _write(self, row_id: int | None, body: dict[str, Any]) -> httpx.Response:
        by_key = {f"field_{item['id']}": item for item in self.fields}
        invalid: dict[str, Any] = {}
        for key, value in body.items():
            described = by_key.get(key)
            if described is None:
                invalid[key] = [{"error": "Este campo não existe.", "code": "unknown"}]
                continue
            ids = {option["id"] for option in described.get("select_options", ())}
            if described["type"] == "single_select" and value is not None and value not in ids:
                invalid[key] = [{"error": "Esta opção não existe.", "code": "invalid_option"}]
            if described["type"] == "multiple_select" and not (
                isinstance(value, list) and set(value) <= ids
            ):
                invalid[key] = [{"error": "Estas opções não existem.", "code": "invalid_option"}]
            if described["type"] == "email" and value and "@" not in str(value):
                invalid[key] = [{"error": "Informe um e-mail válido.", "code": "invalid"}]
        if invalid:
            return httpx.Response(
                400, json={"error": "ERROR_REQUEST_BODY_VALIDATION", "detail": invalid}
            )
        if row_id is None:
            row_id, self._next_id = self._next_id, self._next_id + 1
            self.rows[row_id] = {"id": row_id, "order": "1.00000000000000000000"}
        self.rows[row_id].update(body)
        return httpx.Response(200, json=self.rows[row_id])

    def row_where(self, field_id: int, value: Any) -> dict[str, Any]:
        """A única linha cujo campo tem este valor."""
        (found,) = [row for row in self.rows.values() if row.get(f"field_{field_id}") == value]
        return found


@dataclass(frozen=True)
class Received:
    path: str
    headers: httpx.Headers
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)

    def signed_with(self, secret: str) -> bool:
        """A assinatura do contrato (`app/adapters/webhook.py`): HMAC-SHA256 do segredo sobre
        `{timestamp}.{corpo}`."""
        timestamp = self.headers["X-MCC-Timestamp"].encode()
        mac = hmac.new(secret.encode(), timestamp + b"." + self.body, hashlib.sha256)
        return self.headers["X-MCC-Signature"] == f"v1={mac.hexdigest()}"


class Receiver:
    """Receptor de webhook local, com HTTP de verdade em 127.0.0.1, como o do teste e2e. Grava
    cada pedido e responde com `status`, `body` e `headers`, que o teste pode trocar."""

    def __init__(self) -> None:
        self.requests: list[Received] = []
        self.status = 204
        self.body = b""
        self.headers: dict[str, str] = {}
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                headers = httpx.Headers(list(self.headers.items()))
                receiver.requests.append(Received(self.path, headers, body))
                self.send_response(receiver.status)
                for name, value in receiver.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(receiver.body)))
                self.end_headers()
                self.wfile.write(receiver.body)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        # O `shutdown()` espera o laço do servidor acordar, o que por padrão leva até 0,5 s (o
        # `poll_interval`): em dezenas de testes, uns 25 s de espera só para fechar a porta.
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def url(self, path: str = "/hook") -> str:
        return f"http://{self.host}{path}"


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    current = Receiver()
    yield current
    current.server.shutdown()
    current.server.server_close()


# Quanto o `reset` do `deps` espera, em segundos, por uma sessão de um teste anterior que ficou aberta
# (ou por um lock) antes de desistir: os testes levam milissegundos, então uma que passa disto foi
# esquecida.
LOCK_WAIT = 15.0
# As sessões abertas e paradas no banco do teste (outro processo do pytest usa outro banco), com a
# última consulta de cada uma.
OPEN_SESSIONS = (
    "select left(query, 120) from pg_stat_activity where datname = current_database() "
    "and pid <> pg_backend_pid() and state like 'idle in transaction%'"
)
TABLE_NAMES = tuple(name.strip() for name in TABLES.split(","))
# As tabelas que o `deps` deixa com linhas: os três formulários migrados e as versões deles.
SEEDED_TABLES = ("forms", "form_versions")


class DatabaseReset:
    """Deixa o banco de teste como o `deps` o promete: tabelas vazias, menos os três formulários
    migrados na versão 1.

    Um `TRUNCATE` das 18 tabelas custa uns 20 ms no Postgres, mais uns 5 ms para semear os
    formulários, e é o que mais pesa na suíte (2500 testes pedem o `deps`). A maioria deles deixa
    linhas em poucas tabelas, então só estas são esvaziadas (o `CASCADE` leva as que dependem delas
    por chave estrangeira), e os formulários semeados ficam como estão enquanto a impressão digital
    deles (um md5 do texto de todas as linhas de `forms` e de `form_versions`) é a que a última
    semeadura deixou. Qualquer linha mudada, apagada ou acrescentada nelas, ou um banco que outro
    teste refez, muda a impressão digital: aí as tabelas dos formulários são esvaziadas e semeadas de
    novo, como a cada teste antes. O banco, depois do `reset`, é o mesmo de antes; o que muda é o
    trabalho.
    """

    FINGERPRINT = (
        "md5(coalesce((select string_agg(t::text, ',' order by t.id) from forms t), '') || '|' "
        "|| coalesce((select string_agg(t::text, ',' order by t.id) from form_versions t), ''))"
    )
    PROBE = sa.text(
        "select "
        + ", ".join(f"exists(select 1 from {name})" for name in TABLE_NAMES)
        + f", {FINGERPRINT}, array({OPEN_SESSIONS})"
    )

    def __init__(self) -> None:
        self.fingerprint: str | None = None  # a da última semeadura; None: ainda não semeou

    def reset(self, engine: sa.Engine) -> None:
        """Esvazia o que o teste anterior deixou e garante os três formulários.

        Uma sessão que um teste anterior deixou aberta (sem `with` nem `close()`) é recusada logo,
        com a última consulta dela: antes de o `reset` esvaziar só as tabelas sujas, o `TRUNCATE` das 18
        esperava o lock dela, e agora, sem a espera, ela passaria despercebida até outro teste
        precisar do lock. Uma sessão que só acaba de terminar (uma thread do teste anterior) tem até
        `LOCK_WAIT` segundos para fechar."""
        try:
            with engine.begin() as connection:
                connection.execute(sa.text(f"set local lock_timeout = '{int(LOCK_WAIT * 1000)}ms'"))
                *found, fingerprint, open_sessions = connection.execute(self.PROBE).one()
                self._refuse_open_sessions(connection, open_sessions)
                filled = {
                    name for name, has_rows in zip(TABLE_NAMES, found, strict=True) if has_rows
                }
                reseed = fingerprint != self.fingerprint
                dirty = filled | set(SEEDED_TABLES) if reseed else filled - set(SEEDED_TABLES)
                if dirty:
                    connection.execute(sa.text(f"TRUNCATE {', '.join(sorted(dirty))} CASCADE"))
                if reseed:  # os três formulários, ativos, como a migration 0008 os leva
                    seed_legacy_forms(connection, inactive=())
                    seeded = connection.execute(sa.select(sa.text(self.FINGERPRINT))).scalar_one()
            if reseed:
                self.fingerprint = seeded
        except sa.exc.OperationalError as error:
            if getattr(error.orig, "sqlstate", None) != "55P03":  # lock_not_available
                raise
            with engine.connect() as connection:
                open_sessions = connection.execute(sa.text(OPEN_SESSIONS)).scalars().all()
            raise self._forgotten(
                open_sessions, f"o TRUNCATE esperou {LOCK_WAIT} s por um lock"
            ) from error

    def _refuse_open_sessions(self, connection: sa.Connection, open_sessions: list[str]) -> None:
        deadline = time.monotonic() + LOCK_WAIT
        while open_sessions:
            if time.monotonic() >= deadline:
                raise self._forgotten(open_sessions, "há uma sessão aberta no banco")
            time.sleep(0.05)
            # O Postgres guarda o que `pg_stat_activity` mostrou até o fim da transação: sem isto,
            # a sessão que acabou de fechar continuaria aparecendo.
            connection.execute(sa.text("select pg_stat_clear_snapshot()"))
            open_sessions = list(connection.execute(sa.text(OPEN_SESSIONS)).scalars())

    @staticmethod
    def _forgotten(open_sessions: list[str], what: str) -> RuntimeError:
        return RuntimeError(
            f"{what}: um teste anterior deixou uma sessão aberta (use `with deps.sessions() as "
            f"session`). Última consulta de cada sessão aberta: {open_sessions}"
        )


@pytest.fixture(scope="session")
def database_reset(database_url: str) -> DatabaseReset:
    return DatabaseReset()


@pytest.fixture(scope="session")
def shared_engine(database_url: str) -> Iterator[sa.Engine]:
    """Um engine por processo: abrir um a cada teste custa a conexão e a autenticação (uns 8 ms). Uma
    sessão que um teste deixa aberta continua sendo achada pelo `reset`, que a recusa com a última
    consulta dela."""
    engine = make_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def deps(shared_engine: sa.Engine, database_reset: DatabaseReset, tmp_path: Path) -> Iterator[Deps]:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    database_reset.reset(shared_engine)
    sessions = make_session_factory(shared_engine)
    current = Deps(
        settings=settings,
        sessions=sessions,
        signer=TokenSigner(settings.signing_secrets),
        llm=FakePlanGenerator(),
        pdf=FakePdfRenderer(),
        storage=LocalStorage(tmp_path / "artifacts"),
        webhook=WebhookTransport(),
        cipher=SecretCipher.from_settings(settings),
        forms=StoredRegistry(sessions),  # o registro do app, como em produção
        baserow=no_baserow(),
    )
    override_deps(current)
    yield current
    override_deps(None)


@pytest.fixture
def api_client(deps: Deps) -> Callable[..., Any]:
    """`api_client("leitura")`: cria uma chave sorteada do papel e devolve o cliente que a manda no
    `Authorization` (o app inteiro, com a API de admin montada). Sem papel, é `total`. `name`, `clock`,
    `triggered`, `test_trigger` (o disparador da task `draft_test`) e os ajustes de ambiente
    (`API_RATE_LIMIT_PER_MINUTE="10"`) vêm por nome. Ver
    `admin_api_support.api_client`; para comparar a página com a API, `assert_same_effect`."""
    # Importado aqui, e não no topo: o `admin_api_support` importa o app e o próprio `conftest`.
    from admin_api_support import api_client as build

    return lambda role="total", **options: build(deps, options.pop("clock", None), role, **options)


# --- os três formulários migrados ---------------------------------------------------------------

LEGACY_FORMS = Path(__file__).parent / "fixtures" / "forms"
LEGACY_SLUGS = ("demo", "mcc", "reforma-2027")


def seed_legacy_forms(
    connection: sa.Connection, *, inactive: Iterable[str] = ("demo",), audit: bool = False
) -> None:
    """Publica definições fictícias no banco do teste, sem carregar seeds de runtime.

    Os slugs são identificadores técnicos estáveis dos cenários. Não há formulário de teste
    instalado automaticamente na aplicação distribuída.
    """
    import uuid
    from datetime import UTC

    from app.forms.definition import fingerprint

    metadata = sa.MetaData()
    forms = sa.Table("forms", metadata, autoload_with=connection)
    versions = sa.Table("form_versions", metadata, autoload_with=connection)
    for slug in LEGACY_SLUGS:
        document = legacy_definition(slug)
        form_id = connection.scalar(sa.select(forms.c.id).where(forms.c.slug == slug))
        if form_id is None:
            form_id = uuid.uuid4()
            connection.execute(
                forms.insert().values(
                    id=form_id,
                    slug=slug,
                    title=document["title"],
                    active=slug not in inactive,
                    created_by="fixture",
                    created_at=datetime.now(UTC),
                )
            )
        if connection.scalar(
            sa.select(sa.func.count()).select_from(versions).where(versions.c.form_id == form_id)
        ):
            continue
        connection.execute(
            versions.insert().values(
                id=uuid.uuid4(),
                form_id=form_id,
                number=1,
                definition=document,
                fingerprint=fingerprint(document),
                note="Cenário fictício de teste",
                published_by="fixture",
                published_at=datetime.now(UTC),
            )
        )


def legacy_definition(slug: str) -> dict[str, Any]:
    """A definição sintética de um cenário, independente dos seeds de runtime.
    Uma cópia nova a cada chamada."""
    return json.loads((LEGACY_FORMS / f"{slug}.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def legacy_spec(
    slug: str, clock: Callable[[], datetime] | None = None, *, version_number: int = 1
) -> FormSpec:
    """O contrato do formulário migrado `slug`, compilado da definição da versão 1. `clock` é o
    relógio do prompt e das fases com datas (o do sistema, se não vier)."""
    options: dict[str, Any] = {"version_number": version_number}
    if clock is not None:
        options["clock"] = clock
    return compile_definition(legacy_definition(slug), **options)


class FixedRegistry:
    """Um registro com as versões dadas na criação, que não mudam: o dos testes que publicam uma
    versão sem passar pelo banco (`publish`)."""

    def __init__(self, versions: Iterable[FormSpec]) -> None:
        self._versions = {(spec.slug, spec.version_number): spec for spec in versions}
        self._current: dict[str, FormSpec] = {}
        for spec in self._versions.values():
            current = self._current.get(spec.slug)
            if current is None or spec.version_number > current.version_number:
                self._current[spec.slug] = spec

    def current(self, slug: str) -> FormSpec:
        try:
            return self._current[slug]
        except KeyError:
            raise UnknownForm(slug) from None

    def get(self, slug: str, version_number: int) -> FormSpec:
        try:
            return self._versions[(slug, version_number)]
        except KeyError:
            raise UnknownForm(slug, version_number) from None

    def current_forms(self) -> list[FormSpec]:
        return list(self._current.values())


# --- versões de formulário ----------------------------------------------------------------------

NEWER_DEMO_INTRO = "Versão 2 do diagnóstico, publicada depois dos primeiros envios."
NEWER_DEMO_PREVIEW = "Prévia da versão 2"


def newer_demo_definition() -> dict[str, Any]:
    """Documento da versão 2: a fixture compilada e a publicação no banco usam o mesmo conteúdo."""
    document = legacy_definition("demo")
    document["intro"] = NEWER_DEMO_INTRO
    for step in document["steps"]:
        for question in step["questions"]:
            for option in question.get("options", ()):
                if option["value"] == "2_a_5":
                    option["label"] = "Entre 2 e 5 pessoas"
    document["plan"]["fields"].append(
        {"name": "prazo", "kind": "text", "min_length": 3, "max_length": 80}
    )
    document["examples"]["plan"] = {
        **document["examples"]["plan"],
        "titulo": "Plano da versão 2",
        "prazo": "até o fim do trimestre",
    }
    document["preview"] = (
        f"<p>{NEWER_DEMO_PREVIEW}: {{{{ plan.titulo }}}} ({{{{ plan.prazo }}}})</p>"
    )
    return document


@pytest.fixture
def newer_demo() -> FormSpec:
    """Versão 2 com outra introdução, rótulo de opção, campo do plano e prévia."""
    return compile_definition(newer_demo_definition(), version_number=2)


def publish(deps: Deps, *versions: FormSpec) -> None:
    """Publica versões novas no registro do teste, ao lado dos três formulários migrados (a versão
    1 de cada um). A de maior número de cada formulário vira a atual; as outras continuam valendo
    para os envios delas."""
    # A política de acesso lê a publicação histórica real. O registro de teste pode trocar
    # renderizadores em memória, mas não pode inventar uma publicação ausente do banco.
    from app.forms.definition import fingerprint
    from app.models import Form, FormVersion

    with deps.sessions.begin() as session:
        for spec in versions:
            document = _COMPILED_DOCUMENTS.get((spec.slug, spec.version_number, spec.version))
            form_id = session.scalar(sa.select(Form.id).where(Form.slug == spec.slug))
            if document is not None and form_id is None:
                form = Form(slug=spec.slug, title=spec.title, created_by="testes")
                session.add(form)
                session.flush()
                form_id = form.id
            exists = (
                session.scalar(
                    sa.select(FormVersion.id).where(
                        FormVersion.form_id == form_id, FormVersion.number == spec.version_number
                    )
                )
                if form_id
                else None
            )
            if document is not None and form_id is not None and exists is None:
                session.add(
                    FormVersion(
                        form_id=form_id,
                        number=spec.version_number,
                        definition=document,
                        fingerprint=fingerprint(document),
                        note="Publicação sintética do teste.",
                        published_by="testes",
                    )
                )
    deps.forms = FixedRegistry([*(legacy_spec(slug) for slug in LEGACY_SLUGS), *versions])
