"""A sub-aplicação FastAPI da API de admin (ADR 0015, "Montagem, prefixo e versão").

`create_admin_api()` devolve a aplicação que `create_app` monta em `/admin/api/v1` com `app.mount`.
Ela não é um `APIRouter` do app principal: tem o OpenAPI, os tratadores de erro e a autenticação
dela, e o app principal, o formato de erro das páginas e o da API pública ficam como estão.

- **Sem CORS**: a API não é para páginas de outra origem.
- **Toda resposta** leva `Cache-Control: no-store` e `X-Content-Type-Options: nosniff`
  (`PrivateHeaders`; o `500` e o `413` do app principal levam os mesmos no `error_response`).
- **Negado por padrão**: ao fim da montagem, `refuse_unguarded_routes` recusa toda rota que não seja
  de operação (com papel) nem declarada aberta (`OpenRouter`).
- **Sem `/docs` nem `/openapi.json` automáticos**: o do FastAPI é público. O OpenAPI sai de
  `openapi()`; a referência Scalar (`GET /reference`) e o JSON dela (`GET /openapi.json`) são rotas
  abertas de propósito, fora do esquema, com a regra de acesso própria de `app.admin.api.reference`
  (sessão do admin ou chave em produção; abertas em `development` e `test`).
- **O `Deps` do processo** (`get_deps()`) é lido a cada chamada, não na montagem: a aplicação se
  constrói sem banco nem configuração, o que deixa o gerador do `openapi.json` rodar em qualquer
  máquina.
- **A versão do contrato** (`info.version`, `1.N.0`) sai do número de operações: `N` sobe sozinho a
  cada operação nova, e o que quebra cliente é `/v2`, não número.
- **O que o app entrega na montagem** fica em `app.state`: o relógio (`clock`, o mesmo da sessão do
  admin; os testes o trocam), os disparadores no Hatchet (`trigger` e `test_trigger`), que as
  operações dos pedidos e dos rascunhos usam, e o que a referência precisa para decidir quem entra:
  a sessão do admin (`admin_auth`, o `AdminAuth` do app pai, que a sub-aplicação não enxerga) e o
  ambiente (`environment`). Mais o bloqueio por IP (`throttle`) e o balde de uso por chave
  (`limiter`), que são da API.
- **Rotas e estado são coisas separadas**: `create_admin_api` é `build_admin_api` (as rotas, os
  erros e os cabeçalhos, a parte cara) seguida de `bind_admin_api` (o estado de cima). As rotas só
  leem `request.app.state`; os testes montam algumas sub-aplicações por processo e religam o estado
  a cada app (`tests_app/conftest.py`, `lend_admin_api`). Uma rota que guardasse algo fora de
  `app.state` passaria de um teste para o seguinte.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.admin.api import (
    api_keys,
    assets,
    audit,
    captures,
    destinations,
    draft_tests,
    drafts,
    errors,
    forms,
    handoffs,
    integration_hub,
    integrations,
    jobs,
    journey,
    previews,
    publication,
    reference,
    submissions,
)
from app.admin.api.limits import RateLimiter
from app.admin.api.operations import count_operations, refuse_unguarded_routes, tags_of
from app.admin.auth import AdminAuth, Clock, LoginThrottle

if TYPE_CHECKING:
    from app.api.main import TestTrigger, Trigger

CONTRACT_MAJOR = 1  # o `v1` do caminho; o que quebra cliente é `/v2`
DEFAULT_RATE_LIMIT_PER_MINUTE = 120  # o padrão de `API_RATE_LIMIT_PER_MINUTE`

DESCRIPTION = """\
Tudo o que o painel admin faz, por chave de API com papel.

**Autenticação.** Só `Authorization: Bearer <chave>`: a chave não vai na URL nem em cookie, e a
sessão do painel não vale em nenhuma operação (ela só abre esta referência e o `openapi.json`). Cada
operação diz o papel mínimo (`x-required-role`): `leitura` só consulta; `total` faz tudo o que o
admin logado faz. Toda escrita fica na auditoria em nome da chave (`chave:<nome>`).

**Erros.** Um formato só, `{"code", "detail"}`, com `errors` (`field`, `message`, `line`) no `422`.
A resposta nunca traz o valor recebido.

**Operações marcadas.** As que apagam ou sobrescrevem algo difícil de desfazer (`x-destructive`) e
as que mudam o que a lead vê, para onde os dados dela vão ou chamam um serviço de fora
(`x-external-effect`) exigem `"confirm": true` em toda chamada (na query, em `DELETE`). Sem ele, a
resposta é `422 confirmation_required` e nada acontece.

**Limite de uso.** Cada chave tem um balde de chamadas por minuto; passou, `429 rate_limited` com
`Retry-After`.

**Fora das operações.** De qualquer endereço podem vir `404 not_found` (caminho sem operação),
`405 method_not_allowed` (com `Allow`, os métodos do caminho), `413 payload_too_large`,
`500 internal_error` e `503 unavailable` (banco fora do ar, com `Retry-After`).

## MCP: usar a API pelo Claude

O **MCP** (`python -m app.admin_mcp`) é um programa que roda no computador da operadora e deixa o
Claude Desktop e o Claude Code chamarem esta API numa conversa. É um cliente HTTP desta API, só por
`stdio` (não há MCP remoto), com a chave de quem o usa: nada novo sobe no servidor, e a autorização é
a da chave.

**Configurar.** A tela [Configurar o MCP](/admin/mcp), no menu do painel, leva do zero ao servidor
funcionando: escolhe ou cria a chave (o segredo aparece uma vez), mostra a instalação, monta o comando
do Claude Code ou o bloco do Claude Desktop já com o endereço deste painel e confere a chave.

**Ferramentas por papel.** Cada operação desta referência é uma ferramenta, com o `operationId` como
nome, o `summary` e a `description` como texto, e os parâmetros e o corpo juntos num só objeto de
entrada. Uma chave `leitura` recebe as operações de papel `leitura`; uma `total`, todas. Vêm além
delas algumas **compostas**, que juntam as chamadas de um fluxo (`attention_overview`,
`diagnose_order`, `edit_form_draft`, `publish_form_with_review` e `connect_baserow_integration`) e só
chamam ferramentas que a chave já tem. Para explorar, prefira `leitura`: são menos ferramentas.

**Confirmações.** Uma operação marcada (`x-destructive` ou `x-external-effect`) não executa sozinha: o
MCP mostra o efeito e o alvo e só chama a API com o sim da pessoa. O `confirm` que o modelo escrever
por conta própria é ignorado.

**Dado da lead.** Fica desligado: sem `GERADOR_MCP_LEAD_DATA=1`, o nome, o e-mail e as respostas das
leads não chegam ao modelo, e o argumento `include_lead_data`, a busca `q` de `list_jobs` e a
`download_job_pdf` não existem. Com a opção e uma chave `total` eles voltam, e o que o Claude ler vai
ao provedor do modelo.

**Pastas.** PDFs e HTML vão para arquivo, em `GERADOR_MCP_DOWNLOAD_DIR` (ou numa pasta só do usuário
no diretório temporário), e a ferramenta devolve o caminho, nunca os bytes. As imagens
(`upload_asset` e `replace_asset`) só saem de `GERADOR_MCP_UPLOAD_DIR`, uma pasta que a pessoa
escolhe e que não tem padrão. **A imagem enviada é servida sem login**: ponha nessa pasta só o que
pode ser público.

**A chave.** Vai no ambiente do cliente (`GERADOR_API_URL` e `GERADOR_API_KEY`), nunca na linha de
comando do servidor, e só viaja no cabeçalho `Authorization: Bearer`: não aparece em retorno de
ferramenta, em erro nem em log.
"""

# Os roteadores da API, um por recurso: cada fase de recurso inclui o dela aqui. A tag e a descrição
# dela vêm do próprio roteador (`OperationRouter`), e a lista de tags do OpenAPI sai desta.
ROUTERS = (
    api_keys.router,
    captures.router,
    submissions.router,
    assets.router,
    audit.router,
    destinations.router,
    forms.router,
    drafts.router,
    draft_tests.router,
    previews.router,
    integrations.router,
    handoffs.router,
    journey.router,
    integration_hub.router,
    jobs.router,
    publication.router,
)
TAGS = tags_of(ROUTERS)  # uma tag repetida entre roteadores derruba a montagem


class PrivateHeaders:
    """Toda resposta sai sem cache e sem adivinhação de tipo (`nosniff`), seja qual for o caminho
    que a gerou: operação, erro de validação, 404 do roteador."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_private(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in errors.PRIVATE_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_private)


def build_admin_api() -> FastAPI:
    """As rotas, os erros e os cabeçalhos da sub-aplicação, sem o que o app entrega a ela (o estado
    de `bind_admin_api`). É a parte cara: o FastAPI monta o esquema de cada operação, e a montagem
    termina conferindo todas (`refuse_unguarded_routes`). O estado não entra aqui, e é por isso que
    os testes montam uma só vez por processo e religam o estado a cada teste."""
    api = FastAPI(
        title="Leadkit: API de admin",
        version=f"{CONTRACT_MAJOR}.0.0",  # a versão de verdade sai da contagem das operações, abaixo
        description=DESCRIPTION,
        openapi_tags=TAGS,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # Relativo ao host de quem lê (a referência e o MCP resolvem contra a origem em que o
        # documento foi servido): o `openapi.json` é o mesmo em qualquer máquina.
        servers=[{"url": errors.PREFIX}],
        # Barra final não é a mesma operação: 404 no formato da API, e não um 307 do Starlette.
        redirect_slashes=False,
        # Um modelo, um esquema: o `FormDocument` entra no `PUT` e sai no `GET`, e o contrato o descreve
        # com um nome só, e não com um `-Input` e um `-Output` que o cliente e o MCP teriam de unir.
        separate_input_output_schemas=False,
    )
    api.add_middleware(PrivateHeaders)
    errors.install(api)
    for router in ROUTERS:
        api.include_router(router)
    api.include_router(reference.router)  # as únicas rotas abertas, fora do esquema
    refuse_unguarded_routes(api)
    api.version = f"{CONTRACT_MAJOR}.{count_operations(api)}.0"
    return api


def bind_admin_api(
    api: FastAPI,
    *,
    clock: Clock = time.time,
    trigger: Trigger | None = None,
    test_trigger: TestTrigger | None = None,
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
    admin_auth: AdminAuth | None = None,
    environment: str = "production",
) -> FastAPI:
    """Entrega à sub-aplicação o que o app dá a ela (`app.state`): o relógio, os disparadores, a sessão
    do admin e o ambiente, mais o bloqueio por IP e o balde de uso, que nascem novos a cada chamada.
    As rotas só leem `request.app.state`, nunca guardam nada do app, então ligar de novo o estado
    de uma sub-aplicação já montada deixa-a como uma recém-criada."""
    api.state.clock = clock
    api.state.trigger = trigger
    api.state.test_trigger = test_trigger
    api.state.admin_auth = admin_auth
    api.state.environment = environment
    # O bloqueio por IP da chave errada é o mesmo mecanismo do login, mas outro contador: um script
    # com a chave vencida não tranca a operadora para fora do login do mesmo escritório.
    api.state.throttle = LoginThrottle(clock)
    api.state.limiter = RateLimiter(rate_limit_per_minute, clock)
    return api


def create_admin_api(
    *,
    clock: Clock = time.time,
    trigger: Trigger | None = None,
    test_trigger: TestTrigger | None = None,
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
    admin_auth: AdminAuth | None = None,
    environment: str = "production",
) -> FastAPI:
    """A sub-aplicação, sem montar. `clock` mede o que a API mede (vencimento das chaves, bloqueio
    e balde de uso); `trigger` e `test_trigger` são os disparadores do app;
    `rate_limit_per_minute` é o `API_RATE_LIMIT_PER_MINUTE` do balde de cada chave.

    `admin_auth` (a sessão do painel) e `environment` (`APP_ENV`) são para a referência
    (`app.admin.api.reference`): a sub-aplicação não enxerga o `AdminAuth` do app pai. Sem eles, a
    referência pede uma chave, como em produção: esquecer de passá-los fecha, não abre."""
    return bind_admin_api(
        build_admin_api(),
        clock=clock,
        trigger=trigger,
        test_trigger=test_trigger,
        rate_limit_per_minute=rate_limit_per_minute,
        admin_auth=admin_auth,
        environment=environment,
    )
