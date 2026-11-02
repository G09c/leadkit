"""Os modelos dos destinos do webhook: o que as operações de `/destinations` recebem e devolvem.

Dados, não telas: os códigos (`form`, `default`, `none`, `delivered`), as datas em ISO 8601 UTC e
nenhum rótulo. O escopo de um destino é a palavra `default` (o destino padrão) ou o endereço (`slug`)
do formulário.

**O segredo só sai na criação e na troca.** Nenhum modelo de leitura o traz: a leitura diz só
`secret_unreadable` (o guardado não abre com a chave atual) e `secret_created_at`. O campo `secret`
existe em `SavedDestination` (só na criação) e em `RotatedDestination`, as respostas que o mostram
uma vez; nenhum corpo de entrada tem um campo de segredo, porque ele nasce no servidor. A URL do destino é
`url_redacted` para a chave `leitura`: só o esquema e o host, porque o caminho costuma valer como
senha. Nenhum exemplo parece um segredo.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field

from app import destinations
from app.adapters.webhook import Probe
from app.adapters.webhook import outcome as verdict_of
from app.admin.actions import destinations as actions
from app.admin.api.common import ConfirmedInput, Page, VersionedInput, utc
from app.admin.api.schemas.capture_deliveries import CaptureDeliveryCounts
from app.admin.queries import destinations as queries

Serving = Literal["form", "default", "none"]
Outcome = Literal["delivered", "retry", "rejected", "no_response"]

SERVING_TEXT = (
    "Quem entrega os eventos deste escopo: `form` (o destino próprio do formulário), `default` (o "
    "destino padrão) ou `none` (nenhum: os planos não são entregues em lugar nenhum). No escopo "
    "`default`, é ele mesmo (`default`) ou `none`."
)
VERSION_TEXT = (
    "A versão do destino: um texto opaco que muda a cada mudança (endereço ou segredo). Mande-o em "
    "`expected_version` ao escrever. Enquanto o destino não existe, a versão é `0`: é a que a criação "
    "manda."
)
SCOPE_TEXT = (
    "`default` (o destino padrão) ou o endereço (`slug`) do formulário dono do destino, como "
    "`list_forms` o mostra."
)


def _scope(form_slug: str | None) -> str:
    return form_slug or actions.DEFAULT_SCOPE


class Destination(BaseModel):
    purpose: Literal["internal", "participant"]
    purpose_origin: Literal["legacy", "explicit", "default"]

    """Um destino do webhook guardado, como a API o mostra. **Nunca traz o segredo.**"""

    events: list[
        Literal[
            "plano.concluido",
            "submissao.persistida",
            "submissao.persistida.v2",
            "plano.liberado",
            "contato.capturado",
            "contato.revogado",
            "formulario.concluido",
        ]
    ] = Field(
        default_factory=lambda: ["plano.concluido"],
        description="Eventos que a operadora declarou aceitos pelo receptor.",
    )
    scope: str = Field(description=SCOPE_TEXT, examples=["default", "diagnostico-rapido"])
    url: str = Field(
        description="O endereço do receptor, para onde vão os eventos declarados em `events`. **A chave "
        "`total` o recebe inteiro; a `leitura`, só com o esquema e o host** (`https://host`, e a porta "
        "se houver; `url_redacted` diz qual foi o caso), porque o caminho de uma URL de webhook costuma "
        "valer como senha.",
        examples=["https://receptor.example.test/planos"],
    )
    url_redacted: bool = Field(
        description="`true`: `url` traz só o esquema e o host, sem o caminho (a chave é `leitura`). "
        "`false`: é o endereço inteiro."
    )
    host: str = Field(
        description="O host do receptor, com a porta se houver.",
        examples=["receptor.example.test"],
    )
    version: str = Field(description=VERSION_TEXT, examples=["1"])
    secret_unreadable: bool = Field(
        description="O segredo guardado **não abre** com a `WEBHOOK_ENCRYPTION_KEY` atual (a chave foi "
        "trocada sem manter a antiga): as entregas para este destino falham, e o envio de teste "
        "também, até alguém gerar um segredo novo com `rotate_destination_secret`."
    )
    secret_created_at: AwareDatetime = Field(
        description="Quando o segredo foi gerado pela última vez (ISO 8601, UTC)."
    )
    created_by: str = Field(
        description="Quem criou: o usuário do admin, `chave:<nome>` ou `ambiente` (o destino padrão "
        "que veio do `WEBHOOK_URL`).",
        examples=["operadora"],
    )
    created_at: AwareDatetime = Field(description="Quando foi criado (ISO 8601, UTC).")
    updated_by: str = Field(description="Quem fez a última mudança.")
    updated_at: AwareDatetime = Field(description="Quando foi a última mudança.")

    @classmethod
    def of(
        cls,
        stored: destinations.StoredDestination,
        *,
        unreadable: bool,
        full_url: bool,
    ) -> Destination:
        """O destino guardado. `full_url` é o papel `total`: sem ele, a URL sai só com o esquema e o
        host."""
        return cls(
            scope=_scope(stored.form_slug),
            events=list(stored.events),
            purpose=stored.purpose,
            purpose_origin=stored.purpose_origin,
            url=stored.url if full_url else destinations.origin_of(stored.url),
            url_redacted=not full_url,
            host=stored.host,
            version=str(stored.version),
            secret_unreadable=unreadable,
            secret_created_at=utc(stored.secret_created_at),
            created_by=stored.created_by,
            created_at=utc(stored.created_at),
            updated_by=stored.updated_by,
            updated_at=utc(stored.updated_at),
        )


# --- a lista e o detalhe ------------------------------------------------------------------------


class DestinationSummary(BaseModel):
    """Um escopo na lista: o destino padrão ou um formulário, e quem entrega os eventos dele. Nunca
    traz a URL (`get_destination` a traz, conforme o papel) nem o segredo."""

    scope: str = Field(description=SCOPE_TEXT, examples=["default", "diagnostico-rapido"])
    title: str | None = Field(
        description="O título do formulário. Nulo no destino padrão e no de um formulário que saiu do "
        "código mas ainda tem destino guardado (que ainda pode ser removido).",
        examples=["Diagnóstico rápido"],
    )
    stored: bool = Field(
        description="Este escopo tem destino próprio. No escopo `default`, `false` quer dizer que não "
        "há destino padrão."
    )
    version: str = Field(description=VERSION_TEXT, examples=["1"])
    serving: Serving = Field(description=SERVING_TEXT)
    serving_host: str | None = Field(
        description="O host de quem entrega. Nulo em `none`.", examples=["receptor.example.test"]
    )
    secret_unreadable: bool = Field(
        description="O segredo do destino próprio deste escopo não abre com a chave atual. `false` "
        "também quando o escopo não tem destino próprio."
    )

    @classmethod
    def of_form(cls, row: queries.FormRow, *, unreadable: bool) -> DestinationSummary:
        return cls(
            scope=row.slug,
            title=row.title,
            stored=row.version > 0,
            version=str(row.version),
            serving=row.serving,  # type: ignore[arg-type]
            serving_host=row.host,
            secret_unreadable=unreadable,
        )

    @classmethod
    def of_default(
        cls, stored: destinations.StoredDestination | None, *, unreadable: bool
    ) -> DestinationSummary:
        return cls(
            scope=actions.DEFAULT_SCOPE,
            title=None,
            stored=stored is not None,
            version=str(stored.version) if stored else "0",
            serving="default" if stored else "none",
            serving_host=stored.host if stored else None,
            secret_unreadable=unreadable,
        )


class DestinationPage(Page[DestinationSummary]):
    """Uma página de escopos: primeiro o destino padrão, depois os formulários na ordem do registro."""

    unserved: list[str] = Field(
        description="Os formulários (`slug`) sem destino nenhum, nem próprio nem padrão: os planos "
        "deles **não são entregues**. A lista é inteira, qualquer que seja a página."
    )
    unreadable: list[str] = Field(
        description="Os escopos (`default` ou o `slug`) cujo segredo não abre com a "
        "`WEBHOOK_ENCRYPTION_KEY` atual: as entregas para eles falham até alguém gerar um segredo "
        "novo. A lista é inteira, qualquer que seja a página."
    )

    @classmethod
    def of(
        cls, found: queries.Overview, items: list[DestinationSummary], next_cursor: str | None
    ) -> DestinationPage:
        return cls(
            items=items,
            next_cursor=next_cursor,
            unserved=found.unserved,
            unreadable=[_scope(slug) for slug in found.unreadable],
        )


class DestinationDetail(BaseModel):
    serving_purpose: Literal["internal", "participant"] | None = Field(
        default=None,
        description="Finalidade efetiva, inclusive do destino padrão herdado. Nenhum documento ou dado pessoal acompanha este estado.",
    )

    """O destino de um escopo e quem entrega os eventos dele."""

    capture_delivery_counts: CaptureDeliveryCounts = Field(
        default_factory=CaptureDeliveryCounts,
        description="Intenções locais de captura por estado. Remover a configuração apaga esse histórico; pendentes não mudam de destino e não serão enviadas.",
    )

    serving_events: list[str] = Field(
        default_factory=list,
        description="Eventos aceitos pelo destino efetivo, inclusive quando herdado do padrão.",
    )
    scope: str = Field(description=SCOPE_TEXT, examples=["default", "diagnostico-rapido"])
    title: str | None = Field(
        description="O título do formulário. Nulo no destino padrão e no de um formulário que saiu do "
        "código mas ainda tem destino guardado.",
        examples=["Diagnóstico rápido"],
    )
    version: str = Field(description=VERSION_TEXT, examples=["1"])
    destination: Destination | None = Field(
        description="O destino próprio deste escopo. Nulo: ainda não tem (para criá-lo, "
        '`save_destination` com `expected_version` `"0"`).'
    )
    serving: Serving = Field(description=SERVING_TEXT)
    serving_host: str | None = Field(
        description="O host de quem entrega os eventos deste escopo. Nulo em `none`.",
        examples=["receptor.example.test"],
    )

    @classmethod
    def of(cls, found: queries.Detail, *, full_url: bool) -> DestinationDetail:
        own = found.stored
        return cls(
            capture_delivery_counts=CaptureDeliveryCounts.of(found.capture_counts),
            scope=_scope(found.form_slug),
            title=found.form_title,
            version=str(own.version) if own else "0",
            destination=Destination.of(own, unreadable=found.unreadable, full_url=full_url)
            if own
            else None,
            serving=found.serving,  # type: ignore[arg-type]
            serving_host=found.serving_host,
            serving_events=list(found.serving_events),
            serving_purpose=found.serving_purpose,
        )


# --- criar ou mudar o endereço ------------------------------------------------------------------


class SaveDestinationRequest(ConfirmedInput, VersionedInput):
    purpose: Literal["internal", "participant"] | None = Field(
        default=None,
        description="Finalidade do receptor. Interna inclui dados comerciais; participante recebe só plano público. Criação omitida nunca autoriza uso interno; mudar URL exige escolha explícita. Giro de segredo conserva a escolha.",
    )

    """O endereço a gravar. Criar o destino (`expected_version` `"0"`) gera o segredo; mudar o endereço
    mantém o segredo. Como o endereço decide para onde vão os dados das leads, a operação exige
    `confirm: true` em toda chamada."""

    events: (
        list[
            Literal[
                "plano.concluido",
                "submissao.persistida",
                "submissao.persistida.v2",
                "plano.liberado",
                "contato.capturado",
                "contato.revogado",
                "formulario.concluido",
            ]
        ]
        | None
    ) = Field(
        default=None,
        min_length=1,
        description="Na criação, omitido mantém PDF legado; na atualização, omitido conserva os eventos atuais. Captura exige autorização explícita para submissao.persistida.",
    )
    url: str = Field(
        max_length=destinations.MAX_URL_LENGTH,
        description="A URL do receptor: `https` (ou `http`, que em produção pede `confirm_http`), com "
        "host, sem usuário e senha, sem `#`, sem espaços, sem IP link-local (`169.254.x.x`, `fe80::`) e "
        f"com até {destinations.MAX_URL_LENGTH} caracteres. O receptor confere a assinatura "
        "(`X-MCC-Signature`) de cada evento com o segredo.",
        examples=["https://receptor.example.test/planos"],
    )
    confirm_http: bool = Field(
        default=False,
        description="Em produção, `http` manda os dados das leads sem criptografia e só passa com "
        "`confirm_http: true`: a mesma confirmação que a página pede (sem ela, `422 "
        "confirmation_required`). Fora de produção e com `https`, não faz nada.",
        examples=[False],
    )


class SavedDestination(Destination):
    """O destino como ficou. Na criação, e só nela, traz o segredo, que **não aparece de novo**."""

    created: bool = Field(
        description='`true`: o destino acabou de nascer (`expected_version` `"0"`) e `secret` traz o '
        "segredo. `false`: só o endereço mudou, e o segredo continua o mesmo."
    )
    secret: str | None = Field(
        description="O segredo do destino, para pôr no receptor, que confere com ele a assinatura de "
        "cada evento. **Só existe nesta resposta e só na criação**: guarde-o; se a resposta se "
        "perder, gere outro com `rotate_destination_secret` (o antigo deixa de valer). Nulo quando só o "
        "endereço mudou.",
        examples=["<segredo-mostrado-uma-vez>"],
        repr=False,
    )

    @classmethod
    def of_saved(cls, saved: destinations.SaveResult, *, unreadable: bool) -> SavedDestination:
        base = Destination.of(saved.destination, unreadable=unreadable, full_url=True)
        return cls(**base.model_dump(), created=saved.secret is not None, secret=saved.secret)


# --- trocar o segredo ---------------------------------------------------------------------------


class RotateDestinationSecretRequest(ConfirmedInput, VersionedInput):
    """A troca do segredo. O antigo deixa de valer na hora, por isso a operação exige `confirm: true`."""


class RotatedDestination(Destination):
    """O destino com o segredo novo, que **não aparece de novo**."""

    secret: str = Field(
        description="O segredo novo do destino. **Só existe nesta resposta**: ponha-o no receptor "
        "agora, porque o antigo já não vale. Novos envios usam este segredo; entregas PDF fixadas "
        "exigem repetir explicitamente com a revisão atual. Intenções de respostas após salvar conservam "
        "a revisão original e não podem ser repetidas com o segredo ou a revisão novos.",
        examples=["<segredo-mostrado-uma-vez>"],
        repr=False,
    )

    @classmethod
    def of_rotated(cls, rotated: actions.Rotated) -> RotatedDestination:
        # O segredo novo foi cifrado agora com a chave atual: ele abre.
        base = Destination.of(rotated.destination, unreadable=False, full_url=True)
        return cls(**base.model_dump(), secret=rotated.secret)


# --- remover ------------------------------------------------------------------------------------


class RemovedDestination(BaseModel):
    """O destino que saiu e quem entrega os eventos do escopo agora."""

    capture_delivery_counts: CaptureDeliveryCounts = Field(
        default_factory=CaptureDeliveryCounts,
        description="Intenções locais de captura por estado. Remover a configuração apaga esse histórico; pendentes não mudam de destino e não serão enviadas.",
    )

    scope: str = Field(description=SCOPE_TEXT, examples=["default", "diagnostico-rapido"])
    removed: Destination = Field(
        description="O destino que saiu, como estava antes de ser removido."
    )
    serving: Serving = Field(
        description="Quem entrega os eventos deste escopo depois da remoção: `default` (o formulário "
        "passa a usar o destino padrão para novos envios) ou `none` (novos envios ficam sem entrega). Intenções PDF fixadas não são redirecionadas. Nunca "
        "`form`."
    )
    serving_host: str | None = Field(
        description="O host de quem entrega agora. Nulo em `none`.",
        examples=["receptor.example.test"],
    )

    @classmethod
    def of_removed(cls, removed: actions.Removed) -> RemovedDestination:
        return cls(
            capture_delivery_counts=CaptureDeliveryCounts.of(removed.capture_counts),
            scope=_scope(removed.destination.form_slug),
            removed=Destination.of(removed.destination, unreadable=False, full_url=True),
            serving=removed.serving,  # type: ignore[arg-type]
            serving_host=removed.serving_host,
        )


# --- o evento de teste --------------------------------------------------------------------------


class SendDestinationTestEventRequest(ConfirmedInput):
    """O envio do evento de teste. Sai para o receptor de verdade, por isso exige `confirm: true`."""

    event: Literal["webhook.teste", "submissao.persistida", "submissao.persistida.v2"] = Field(
        default="webhook.teste",
        description="Teste sintético do contrato selecionado exige que o destino o aceite; respostas após salvar usam o envelope2. Não cria submissão.",
    )


class DestinationTestResult(BaseModel):
    """O que o receptor respondeu ao evento de teste."""

    scope: str = Field(description=SCOPE_TEXT, examples=["default", "diagnostico-rapido"])
    outcome: Outcome = Field(
        description="O que a entrega do evento selecionado faria com essa resposta: `delivered` (2xx: conta como "
        "entregue), `retry` (408, 425, 429 ou 5xx: seria repetido por cerca de 3,5 h e depois "
        "marcado como falho), `rejected` (outro status, 3xx inclusive: redirecionamentos não são "
        "seguidos; seria recusado de vez) ou `no_response` (o receptor não respondeu: `error` diz o "
        "tipo da falha, e o evento seria repetido)."
    )
    status: int | None = Field(
        description="O status HTTP da resposta. Nulo em `no_response`.", examples=[204]
    )
    elapsed_ms: int = Field(description="Quanto o receptor levou para responder, em milissegundos.")
    error: str | None = Field(
        description="Só em `no_response`: o tipo da falha de rede (`ConnectError`, `ReadTimeout`...), "
        "nunca a URL.",
        examples=["ConnectError"],
    )
    body_start: str | None = Field(
        description="O começo da resposta do receptor (até 500 caracteres), se ele mandou corpo.",
        examples=['{"recebido": true}'],
    )

    @classmethod
    def of(cls, scope: str, probe: Probe) -> DestinationTestResult:
        return cls(
            scope=scope,
            outcome="no_response" if probe.status is None else verdict_of(probe.status),
            status=probe.status,
            elapsed_ms=probe.elapsed_ms,
            error=probe.error,
            body_start=probe.body_start[: actions.BODY_START_CHARS] or None,
        )
