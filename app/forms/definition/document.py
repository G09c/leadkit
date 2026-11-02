"""A estrutura do documento da definição, validada pelo pydantic, e os problemas que o compilador
devolve. O formato está descrito em `app.forms.definition`; aqui ficam os tipos, os limites e as
mensagens em português de cada erro de estrutura."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    StringConstraints,
    Tag,
    ValidationError,
    model_serializer,
    model_validator,
)

NAME_PATTERN = r"^[a-z][a-z0-9_]*$"
SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
VALUE_PATTERN = r"^[a-z0-9][a-z0-9_]*$"
MODEL_PATTERN = r"^[A-Z][A-Za-z0-9]*$"
PATH_PATTERN = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$"
PATTERN_HINTS = {
    NAME_PATTERN: "use letras minúsculas sem acento, números e _, começando por uma letra",
    SLUG_PATTERN: "use letras minúsculas sem acento, números e hífens entre as palavras",
    VALUE_PATTERN: "use letras minúsculas sem acento, números e _",
    MODEL_PATTERN: "comece por letra maiúscula e use só letras e números",
    PATH_PATTERN: "use os nomes dos campos separados por ponto, como fases ou plano.fases",
}
# As mensagens do leitor de JSON, em português.
JSON_MESSAGES = {
    "Expecting value": "falta um valor",
    "Expecting ',' delimiter": "falta uma vírgula",
    "Expecting ':' delimiter": "faltam os dois-pontos depois do nome do campo",
    "Expecting property name enclosed in double quotes": (
        "o nome do campo precisa de aspas duplas, ou sobra uma vírgula"
    ),
    "Unterminated string starting at": "um texto ficou sem as aspas de fechamento",
    "Extra data": "há conteúdo depois do fim do JSON",
    "Invalid control character at": (
        "quebra de linha ou tabulação dentro de um texto; use \\n ou \\t"
    ),
    "Invalid \\escape": "barra invertida inválida dentro de um texto",
}
# O que o banco não guarda num JSONB, e o Python lê sem reclamar.
NOT_A_NUMBER = "NaN e Infinity não são números em JSON"
BAD_CHARACTER = "o texto tem um caractere que não vale (o nulo, \\u0000, ou a metade de um emoji)"
_BAD_CHARACTER = re.compile("[\x00\ud800-\udfff]")
# A definição mais funda de hoje não passa de uns 10 níveis; o teto só barra o absurdo.
MAX_DEPTH = 64
TOO_DEEP = "há listas ou objetos demais, um dentro do outro"
# Blocos previstos para depois. Até lá, a definição que os traz é recusada com um aviso claro, em
# vez de compilar sem eles. Hoje não há nenhum.
RESERVED: dict[str, str] = {}

# O padrão proibido de um campo do plano é uma expressão regular escrita pela operadora e roda sobre
# o texto do LLM: o teto de tamanho barra o exagero.
PATTERN_MAX_LENGTH = 300
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
DATE_HINT = "use uma data no formato AAAA-MM-DD, como 2026-10-30"
# Os tipos de bloco dos dados do formulário.
BLOCK_KINDS = ("value", "list", "windows", "edition")

Name = Annotated[str, StringConstraints(pattern=NAME_PATTERN, max_length=64)]
# Nomes que viram campos de um modelo pydantic: não podem esconder um atributo do modelo.
RESERVED_NAMES = frozenset(name for name in dir(BaseModel) if not name.startswith("_"))
SLUG_MAX_LENGTH = 48
Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN, max_length=SLUG_MAX_LENGTH)]
OptionValue = Annotated[str, StringConstraints(pattern=VALUE_PATTERN, max_length=64)]
ModelName = Annotated[str, StringConstraints(pattern=MODEL_PATTERN, max_length=64)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Version = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Size = Annotated[int, Field(ge=0, le=100_000)]
# As fases com datas: quantas cabem num plano e o teto de dias de cada uma.
MAX_PHASES = 12
MAX_PHASE_DAYS = 366
Choices = Annotated[int, Field(ge=1, le=1000)]
MAX_POINTS = 100_000
MAX_SCORES = 64
MAX_RULES = 64
# As condições (`show_when`): quantas regras, quantos valores numa lista e o tamanho de cada texto.
MAX_CONDITION_RULES = 20
MAX_CONDITION_VALUES = 100
CONDITION_TEXT_MAX_LENGTH = 300


def _points(value: Any) -> int | float:
    """Pontos são um número (o inteiro fica inteiro), sem `true` nem texto."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        # ValueError, e não TypeError: o pydantic só converte o primeiro em problema da definição.
        raise ValueError("precisa ser um número")  # noqa: TRY004
    if abs(value) > MAX_POINTS:
        raise ValueError(f"use um valor entre -{MAX_POINTS} e {MAX_POINTS}")
    return value


Points = Annotated[int | float, BeforeValidator(_points)]


def _values_from_branch(value: Any) -> str:
    return "names" if isinstance(value, list) else "name"


# De onde um campo de opções do plano tira os valores: o nome de uma lista dos dados, ou uma lista de
# nomes (a união das listas, na ordem). As etiquetas dos ramos saem do caminho dos erros
# (`validation_problems`).
VALUES_FROM_BRANCHES = ("name", "names")
ValuesFrom = Annotated[
    Annotated[Name, Tag("name")] | Annotated[list[Name], Tag("names"), Field(min_length=1)],
    Discriminator(_values_from_branch),
]

QuestionKind = Literal["text", "email", "tel", "textarea", "choice", "choices"]
PlanKind = Literal["text", "list", "group", "options"]


def reserved_name(name: str) -> bool:
    """O nome esconderia um atributo do modelo pydantic em que ele vira campo."""
    return name in RESERVED_NAMES or name.startswith("model_")


def _not_reserved(name: str) -> str:
    if reserved_name(name):
        raise ValueError("nome reservado; escolha outro")
    return name


def identifier_of(text: str, *, separator: str, max_length: int) -> str:
    """Um nome ou um endereço tirado de um texto: minúsculas sem acento, com `separator` entre as
    palavras, até `max_length`, sem cortar uma palavra no meio (só uma primeira palavra maior que
    o máximo sai cortada)."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[a-z0-9]+", plain.lower())
    found = ""
    for word in words:
        longer = f"{found}{separator}{word}" if found else word
        if len(longer) > max_length:
            break
        found = longer
    return found or separator.join(words)[:max_length].strip(separator)


FieldName = Annotated[Name, AfterValidator(_not_reserved)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _condition_value(value: Any) -> Any:
    """O valor de uma regra: um texto ou uma lista de textos, nenhum em branco. Qual dos dois vale
    depende do operador, e quem confere é `app.forms.definition.conditions`."""
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
        if not items:
            raise ValueError("informe pelo menos um valor")
        if len(items) > MAX_CONDITION_VALUES:
            raise ValueError(f"use no máximo {MAX_CONDITION_VALUES} valores")
    else:
        raise ValueError("use um texto ou uma lista de textos")  # noqa: TRY004
    for item in items:
        if not isinstance(item, str):
            raise ValueError("use só textos")  # noqa: TRY004
        if not item.strip():
            raise ValueError("não pode ficar vazio")
        if len(item) > CONDITION_TEXT_MAX_LENGTH:
            raise ValueError(f"use no máximo {CONDITION_TEXT_MAX_LENGTH} caracteres")
    return value


class ConditionRuleDoc(_Strict):
    """Uma regra de condição: compara a resposta de uma pergunta anterior (`field`) com `value`
    pelo `operator`. O formato e o que cada operador faz estão em
    `app.forms.definition.conditions`."""

    field: Name
    operator: Literal["equals", "in", "contains"]
    value: Annotated[str | list[str], BeforeValidator(_condition_value)]


class ConditionDoc(_Strict):
    """A condição de uma pergunta ou de uma opção (`show_when`): vale quando todas as regras
    valem (`mode` `all`) ou quando ao menos uma vale (`any`)."""

    mode: Literal["all", "any"]
    rules: list[ConditionRuleDoc] = Field(min_length=1, max_length=MAX_CONDITION_RULES)


class OptionDoc(_Strict):
    value: OptionValue
    label: Label
    points: Points | None = None
    show_when: ConditionDoc | None = None


class QuestionDoc(_Strict):
    name: FieldName
    kind: QuestionKind
    label: Label
    help: Text | None = None
    placeholder: Label | None = None
    autocomplete: Label | None = None
    required: bool = Field(
        default=True,
        description=(
            "Se a lead precisa responder (padrão: `true`). Uma pergunta obrigatória que tem "
            "`show_when` só é exigida quando a pergunta aparece (`conditions.required_when_shown`): "
            "escondida pela condição, a lead não a responde, e o modelo das respostas não a exige."
        ),
    )
    contact: bool = False
    min_length: Size | None = None
    max_length: Size | None = None
    options: list[OptionDoc] | None = Field(default=None, min_length=1)
    min_choices: Choices | None = None
    max_choices: Choices | None = None
    show_when: ConditionDoc | None = None


class StepDoc(_Strict):
    title: Label
    help: Text | None = None
    questions: list[QuestionDoc] = Field(min_length=1)


class IdentityDoc(_Strict):
    email: Name
    name: Name | None = None
    phone: Name | None = None


class MarketingDoc(_Strict):
    email: Text | None = None
    whatsapp: Text | None = None


class ConsentDoc(_Strict):
    version: Version
    processing: Text | None = None
    notice: Text
    marketing: MarketingDoc = MarketingDoc()
    acquisition_method: Literal["checkbox", "submit"] = "checkbox"
    presentation: Literal["panel", "compact"] = "panel"
    position: Literal["first_step", "before_submit"] = "first_step"

    @model_validator(mode="after")
    def required_checkbox_text(self):
        if self.acquisition_method == "checkbox" and self.processing is None:
            raise ValidationError.from_exception_data(
                "ConsentDoc", [{"type": "missing", "loc": ("processing",), "input": None}]
            )
        return self

    @model_serializer(mode="wrap")
    def explicit_options(self, handler):
        found = handler(self)
        for key in ("acquisition_method", "presentation", "position", "processing"):
            if key not in self.model_fields_set:
                found.pop(key, None)
        return found


class PatternDoc(_Strict):
    """O padrão que um campo de texto do plano não pode conter (uma expressão regular, sem
    diferença de maiúsculas) e o que a rodada de reparo diz ao LLM quando ele aparece."""

    pattern: Annotated[str, StringConstraints(min_length=1, max_length=PATTERN_MAX_LENGTH)]
    message: Text | None = None


class PlanNodeDoc(_Strict):
    """Um nó do modelo do plano: um campo (com `name`) ou os itens de uma lista (sem `name`)."""

    name: FieldName | None = None
    kind: PlanKind
    # O nome do campo como o admin o mostra (com acento): só a tela do pedido o lê. Não vai ao LLM,
    # ao plano gravado, ao webhook nem ao PDF.
    label: Label | None = None
    description: Text | None = None
    internal: bool = False
    min_length: Size | None = None
    max_length: Size | None = None
    min_paragraphs: Size | None = None
    forbidden_pattern: PatternDoc | None = None
    min_items: Size | None = None
    max_items: Size | None = None
    items: PlanNodeDoc | None = None
    fields: list[PlanNodeDoc] | None = Field(default=None, min_length=1)
    model: ModelName | None = None
    values: list[OptionValue] | None = Field(default=None, min_length=1)
    values_from: ValuesFrom | None = None
    in_window: dict[OptionValue, Name] | None = Field(default=None, min_length=1)

    @property
    def source_names(self) -> tuple[str, ...]:
        """Os nomes das listas dos dados de onde as opções vêm (`values_from`), na ordem; vazio se
        o campo usa os valores fixos."""
        if self.values_from is None:
            return ()
        return (self.values_from,) if isinstance(self.values_from, str) else tuple(self.values_from)

    def option_values(self, lists: Mapping[str, Sequence[str]]) -> tuple[str, ...] | None:
        """As opções do campo: os valores fixos, ou a união dos itens das listas de `values_from`
        (`lists`, pelo nome), na ordem dos nomes e, dentro de cada um, na ordem da lista. O item que
        está em duas listas fica onde aparece primeiro. None se alguma lista não existe (outra
        conferência já recusou)."""
        if self.values_from is None:
            return tuple(self.values or ())
        if any(name not in lists for name in self.source_names):
            return None
        return tuple(dict.fromkeys(item for name in self.source_names for item in lists[name]))


class ForbiddenDoc(_Strict):
    """Expressões que o plano não pode conter: em todos os textos (`all`), só nos campos escolhidos
    (`only`) ou em todos menos nos escolhidos (`except`). Cada campo é o caminho dos nomes dele no
    plano (`fases.foco`); os itens de uma lista não entram no caminho."""

    expressions: list[Label] = Field(min_length=1, max_length=100)
    scope: Literal["all", "only", "except"] = "all"
    fields: list[Annotated[str, StringConstraints(min_length=1, max_length=300)]] = []


class PlanDoc(_Strict):
    description: Text | None = None
    fields: list[PlanNodeDoc] = Field(min_length=1)
    forbidden: list[ForbiddenDoc] = []


class PhasesDoc(_Strict):
    """As fases com datas do plano: `days` são os dias de cada fase, em ordem (a quantidade de fases
    é o tamanho da lista), e a primeira começa no dia da geração, em São Paulo. `plan_field` é o
    campo lista do plano com um item por fase (`fases`, ou `roteiro.fases` dentro de um grupo), e
    `windows`, o bloco de janelas dos dados de onde saem as janelas que as fases cruzam."""

    days: list[Annotated[int, Field(ge=1, le=MAX_PHASE_DAYS)]] = Field(
        min_length=1, max_length=MAX_PHASES
    )
    plan_field: Annotated[str, StringConstraints(pattern=PATH_PATTERN, max_length=300)] | None = (
        None
    )
    windows: Name | None = None


class PdfDoc(_Strict):
    html: str
    css: str = ""
    # As notas da pontuação que o PDF e a prévia podem mostrar. Sem a chave, nenhuma.
    scores: list[Name] = Field(default=[], max_length=MAX_SCORES)


# --- os avisos das páginas de espera e de falha -------------------------------------------------

NOTICE_URL_MAX_LENGTH = 2000
NOTICE_URL_HINT = "use um endereço que comece por https://, como https://example.test/material.pdf"
_NOT_IN_A_URL = re.compile(r"[\s\x00-\x1f\x7f\\]")


def _https_url(url: str) -> str:
    """O endereço do link de um aviso: só https, com domínio, sem espaços, controles, barra
    invertida nem usuário e senha. É escrito num `href`, então nada além disso passa."""
    if _NOT_IN_A_URL.search(url):
        raise ValueError(NOTICE_URL_HINT)
    try:
        parts = urlsplit(url)
        host, _ = parts.hostname, parts.port  # a porta que não é número levanta aqui
    except ValueError:
        raise ValueError(NOTICE_URL_HINT) from None
    if parts.scheme != "https" or not host or "@" in parts.netloc:
        raise ValueError(NOTICE_URL_HINT)
    return url


HttpsUrl = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=NOTICE_URL_MAX_LENGTH),
    AfterValidator(_https_url),
]


class NoticeLinkDoc(_Strict):
    """O link de um aviso: o rótulo e o endereço, que só pode ser https."""

    label: Label
    url: HttpsUrl


class NoticeDoc(_Strict):
    """Um aviso extra de uma página da lead: um título, um texto (parágrafos separados por linha em
    branco) e, se houver, um link. Tudo é texto puro: a página o escapa, e não há HTML livre."""

    title: Label
    text: Text
    link: NoticeLinkDoc | None = None


class NoticesDoc(_Strict):
    """Os avisos das páginas que a lead vê depois do envio: a de espera (`waiting`) e a de falha
    (`failed`). Cada um é opcional."""

    waiting: NoticeDoc | None = None
    failed: NoticeDoc | None = None


def _iso_date(value: Any) -> Any:
    """Só o texto AAAA-MM-DD vale: o pydantic leria também um número como data."""
    if isinstance(value, str) and ISO_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(DATE_HINT)


IsoDate = Annotated[date, BeforeValidator(_iso_date)]


class ValueBlock(_Strict):
    """Um bloco livre: um valor JSON qualquer, que os templates leem como está."""

    kind: Literal["value"]
    value: Any


class ListBlock(_Strict):
    """Uma lista de nomes (um catálogo de aulas, por exemplo), sem nomes repetidos. Os templates a
    leem como uma lista de textos, e um campo de opções fixas do plano pode tirar os valores dela."""

    kind: Literal["list"]
    items: list[Label] = Field(min_length=1, max_length=1000)


def data_lists(data: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Os itens de cada bloco de lista dos dados do formulário, pelo nome do bloco."""
    return {
        name: tuple(block.items) for name, block in data.items() if isinstance(block, ListBlock)
    }


class WindowDoc(_Strict):
    """Uma janela de datas, com início e fim no mesmo dia ou depois."""

    name: Name
    label: Label | None = None
    start: IsoDate
    end: IsoDate


class WindowsBlock(_Strict):
    """Janelas de datas (a janela de opção, a de desistência), com nomes únicos. Em sequência
    (`sequential`, o padrão), cada janela começa depois do fim da anterior. Os templates leem as
    janelas pelo nome (`data.janelas.opcao.start`)."""

    kind: Literal["windows"]
    windows: list[WindowDoc] = Field(min_length=1, max_length=50)
    sequential: bool = True


class EditionBlock(_Strict):
    """A edição do programa que o prompt cita: o nome e, se houver, o início e o fim."""

    kind: Literal["edition"]
    name: Label
    starts: IsoDate | None = None
    ends: IsoDate | None = None


DataBlockDoc = Annotated[
    ValueBlock | ListBlock | WindowsBlock | EditionBlock, Field(discriminator="kind")
]


class BandLevelDoc(_Strict):
    """Um nível de uma faixa: vale a partir de `min` (a nota >= `min`). O último vale para o resto
    e não leva `min`."""

    min: Points | None = None
    key: OptionValue
    label: Label


class BandsDoc(_Strict):
    """As faixas de uma nota: o nível e o rótulo dele entram na pontuação, com os nomes `key` e
    `label`, ao lado da nota. Os níveis vão do maior limite para o menor."""

    key: Name
    label: Name
    levels: list[BandLevelDoc] = Field(min_length=1)


class ScoreDoc(_Strict):
    """Uma nota: o nome e a fórmula (a linguagem de `app.forms.definition.formulas`), com faixas
    opcionais."""

    name: Name
    formula: str
    bands: BandsDoc | None = None


class RuleDoc(_Strict):
    """Uma regra de prioridade ou de persona: vale a primeira cuja condição (`when`, uma fórmula
    verdadeira ou falsa) bate; sem `when`, ela sempre vale (o "senão", só na última). O resultado
    é `value` ou, na persona, a resposta de uma pergunta de escolha (`answer`)."""

    when: str | None = None
    value: str | None = None
    answer: Name | None = None


class ScoringDoc(_Strict):
    scores: list[ScoreDoc] = Field(min_length=1, max_length=MAX_SCORES)
    priority: list[RuleDoc] = Field(default=[], max_length=MAX_RULES)
    persona: list[RuleDoc] = Field(default=[], max_length=MAX_RULES)


class HandoffDoc(_Strict):
    receiver_id: UUID
    receiver_revision: Annotated[int, Field(ge=1)]


class ReleaseDoc(_Strict):
    mode: Literal["automatic", "manual"] = "automatic"


class CompletionDoc(_Strict):
    """Conclusão após persistência. Texto puro, sem HTML nem interpretação de template."""

    handoff: HandoffDoc | None = None
    mode: Literal["result", "confirmation", "button", "redirect"] = Field(
        default="result",
        description="Próximo passo após commit: resultado PDF (padrão legado), confirmação local, botão ou encaminhamento com recibo de continuidade. Não altera geração nem autorização de PDF.",
    )
    message: Text = Field(
        default="Recebemos suas respostas. Obrigada por participar.",
        description="Mensagem de confirmação em texto puro, de 1 a 2000 caracteres. HTML é escapado; não aceita interpolação de respostas.",
    )
    button_label: Label = Field(
        default="Continuar",
        description="Rótulo do botão de continuidade, em texto puro, de 1 a 300 caracteres. Não é o rótulo de envio do formulário.",
    )
    url: HttpsUrl | None = Field(
        default=None,
        description="URL HTTPS exata autorizada pela configuração autenticada. Obrigatória em button/redirect e ausente em result/confirmation; sem usuário/senha ou override público. Sem handoff conserva o endereço exato; com opt-in acrescenta somente a referência opaca, sem contato, respostas ou bearer PDF.",
        examples=["https://example.test/proximo-passo"],
    )


class ExamplesDoc(_Strict):
    answers: dict[str, Any]
    plan: dict[str, Any] | None = None


def _opening_source(value: str) -> str:
    from app.forms.definition.images import name_problem

    if not value.startswith("asset:"):
        raise ValueError("use uma imagem do formulário no formato asset:nome")
    if problem := name_problem(value[6:]):
        raise ValueError(problem)
    return value


class OpeningLogoDoc(_Strict):
    """Imagem opcional acima do título, independente da marca e do PDF."""

    src: Annotated[str, StringConstraints(strip_whitespace=True), AfterValidator(_opening_source)]
    alt: Label


class PartialCaptureDoc(_Strict):
    """Retomada opt-in; omissão preserva formulários existentes."""

    contact_permission_field: Name
    contact_permission_value: str = Field(min_length=1, max_length=200)
    ttl_hours: int = Field(default=72, ge=1, le=168)


class FormDocument(_Strict):
    """A definição de um formulário: as etapas e as perguntas, a identidade e o consentimento da lead,
    a pontuação, as instruções e o prompt do LLM, o modelo do plano, o HTML e o CSS do PDF, a prévia
    da página de resultado e os exemplos. É o que o banco guarda de cada versão publicada e o que a
    exportação escreve (`get_form_version` o devolve como está guardado). O formato e o que cada
    parte faz estão em `app.forms.definition`."""

    format: Literal[1]
    slug: Slug
    title: Label
    intro: Text
    opening_logo: OpeningLogoDoc | None = None
    minutes: Annotated[int, Field(ge=1, le=60)]
    steps: list[StepDoc] = Field(min_length=1)
    identity: IdentityDoc
    consent: ConsentDoc
    partial_capture: PartialCaptureDoc | None = None
    scoring: ScoringDoc | None = None
    generation: Literal["pdf", "none"] = "pdf"
    release: ReleaseDoc | None = None
    instructions: str | None = None
    prompt: str | None = None
    plan: PlanDoc | None = None
    pdf: PdfDoc | None = None
    preview: str | None = None
    data: dict[Name, DataBlockDoc] = {}
    phases: PhasesDoc | None = None
    notices: NoticesDoc | None = None
    completion: CompletionDoc = Field(default_factory=CompletionDoc)
    examples: ExamplesDoc


class PdfExamplesDoc(ExamplesDoc):
    plan: dict[str, Any]


class PdfFormDocument(FormDocument):
    generation: Literal["pdf"] = "pdf"
    instructions: str
    prompt: str
    plan: PlanDoc
    pdf: PdfDoc
    preview: str
    examples: PdfExamplesDoc


class CaptureExamplesDoc(ExamplesDoc):
    plan: None = None


class CaptureFormDocument(FormDocument):
    release: None = None
    examples: CaptureExamplesDoc
    generation: Literal["none"]
    instructions: None = None
    prompt: None = None
    plan: None = None
    pdf: None = None
    preview: None = None
    phases: None = None


DefinitionDocument = PdfFormDocument | CaptureFormDocument


# --- problemas ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    """Um problema da definição. `path` é o caminho do campo no documento, com os índices das
    listas a partir de 0 (`steps.1.questions.0.options`); `line`, a linha do template (ou do
    texto JSON, quando o caminho é vazio)."""

    path: str
    message: str
    line: int | None = None

    def __str__(self) -> str:
        where = self.path or "definição"
        if self.line is not None:
            where += f", linha {self.line}"
        return f"{where}: {self.message}"


class DefinitionError(ValueError):
    """A definição não compila. `problems` traz todos os problemas do grupo em que a compilação
    parou (estrutura, regras, exemplos ou templates), na ordem do documento."""

    def __init__(self, problems: Iterable[Problem]) -> None:
        self.problems = tuple(problems)
        super().__init__("definição inválida: " + "; ".join(str(p) for p in self.problems))


def path_of(*parts: str | int) -> str:
    return ".".join(str(part) for part in parts if part != "")


def validation_problems(error: ValidationError, prefix: str = "") -> list[Problem]:
    """Erros do pydantic como problemas, com o caminho e a mensagem em português. Nunca repete o
    valor recebido."""
    problems = []
    for item in error.errors(include_input=False, include_url=False):
        loc, message = item["loc"], message_of(item)
        if loc and loc[-1] == "[key]":  # a chave do objeto, e não o valor dela
            loc, message = loc[:-1], f"nome inválido; {message}"
        loc = _without_values_from_branch(_without_block_kind(loc, item["type"]))
        problems.append(Problem(path_of(prefix, *loc), message))
    return problems


def _without_block_kind(loc: tuple[int | str, ...], error_type: str) -> tuple[int | str, ...]:
    """O caminho de um erro num bloco dos dados do formulário: o pydantic põe o tipo do bloco no
    caminho (`data.janelas.windows.windows.0`), e o tipo desconhecido ou ausente aponta para o
    bloco (o erro é no campo `kind` dele)."""
    if error_type in ("union_tag_invalid", "union_tag_not_found"):
        return (*loc, "kind")
    if loc[:1] == ("data",) and len(loc) > 2 and loc[2] in BLOCK_KINDS:
        return (*loc[:2], *loc[3:])
    return loc


def _without_values_from_branch(loc: tuple[int | str, ...]) -> tuple[int | str, ...]:
    """O caminho de um erro em `values_from`: o pydantic põe a etiqueta do ramo da união no caminho
    (`values_from.names.1`), e o caminho que a operadora lê é `values_from.1`."""
    if loc[:1] != ("plan",):  # nos dados, um bloco pode se chamar values_from
        return loc
    for k, part in enumerate(loc[:-1]):
        if part == "values_from" and loc[k + 1] in VALUES_FROM_BRANCHES:
            return (*loc[: k + 1], *loc[k + 2 :])
    return loc


def counted(count: int, singular: str, plural: str) -> str:
    """ "1 item", "3 itens"."""
    return f"{count} {singular if count == 1 else plural}"


def message_of(error: Mapping[str, Any]) -> str:
    ctx = error.get("ctx") or {}
    match error["type"]:
        case "missing":
            return "campo obrigatório"
        case "extra_forbidden":
            return "campo desconhecido"
        case "string_type":
            return "precisa ser um texto"
        case "int_type" | "int_parsing" | "int_from_float":
            return "precisa ser um número inteiro"
        case "bool_type" | "bool_parsing":
            return "precisa ser true ou false"
        case "list_type":
            return "precisa ser uma lista"
        case "dict_type" | "model_type" | "model_attributes_type":
            return "precisa ser um objeto"
        case "literal_error":
            expected = str(ctx.get("expected", "")).replace(" or ", " ou ")
            return f"use um destes valores: {expected}"
        case "string_too_short":
            minimum = ctx.get("min_length", 1)
            return (
                "não pode ficar vazio" if minimum == 1 else f"use pelo menos {minimum} caracteres"
            )
        case "string_too_long":
            return f"use no máximo {counted(ctx['max_length'], 'caractere', 'caracteres')}"
        case "string_pattern_mismatch":
            return PATTERN_HINTS.get(ctx.get("pattern", ""), "formato inválido")
        case "too_short":
            return f"precisa de pelo menos {counted(ctx['min_length'], 'item', 'itens')}"
        case "too_long":
            return f"aceita no máximo {counted(ctx['max_length'], 'item', 'itens')}"
        case "greater_than_equal":
            return f"precisa ser pelo menos {ctx['ge']}"
        case "less_than_equal":
            return f"precisa ser no máximo {ctx['le']}"
        case "union_tag_invalid":
            return f"use um destes tipos: {', '.join(BLOCK_KINDS)}"
        case "union_tag_not_found":
            return "campo obrigatório"
        case "value_error":
            message = str(ctx.get("error", "")).strip()
            return message or "valor inválido"
        case (
            "forbidden_expression"
            | "forbidden_pattern"
            | "forbidden_pattern_timeout"
            | "too_few_paragraphs"
        ):
            return str(error["msg"])  # a regra do plano, já em português e sem o texto do plano
    return "valor inválido"


def fingerprint(document: Mapping[str, Any]) -> str:
    """A impressão digital do conteúdo da definição: o sha256, em hexadecimal, do documento em
    JSON canônico (chaves em ordem, sem espaços, UTF-8). Dois documentos com o mesmo conteúdo JSON,
    escritos em outra ordem ou com outros espaços, têm a mesma impressão."""
    canonical = json.dumps(
        document, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def value_problems(value: Any, path: tuple[str | int, ...] = ()) -> list[Problem]:
    """O que o JSON do Python aceita e o banco (JSONB) não guarda: NaN e Infinity, o caractere
    nulo e a metade de um emoji (um surrogate sozinho). Também põe um teto no aninhamento."""
    if len(path) > MAX_DEPTH:
        return [Problem(path_of(*path), TOO_DEEP)]
    if isinstance(value, str):
        return [Problem(path_of(*path), BAD_CHARACTER)] if _BAD_CHARACTER.search(value) else []
    if isinstance(value, float):
        return [] if math.isfinite(value) else [Problem(path_of(*path), NOT_A_NUMBER)]
    problems: list[Problem] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and _BAD_CHARACTER.search(key):
                problems.append(Problem(path_of(*path), f"nome inválido; {BAD_CHARACTER}"))
                continue
            problems += value_problems(item, (*path, key))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            problems += value_problems(item, (*path, index))
    return problems


def parse(document: Mapping[str, Any] | str | bytes) -> FormDocument:
    """O documento lido e conferido na estrutura: tipos, campos obrigatórios e desconhecidos.
    Levanta `DefinitionError`."""
    if isinstance(document, str | bytes):
        try:
            document = json.loads(document)
        except json.JSONDecodeError as exc:
            detail = JSON_MESSAGES.get(exc.msg, "formato inválido")
            message = f"JSON inválido: {detail} (coluna {exc.colno})"
            raise DefinitionError([Problem("", message, exc.lineno)]) from None
        except UnicodeDecodeError:
            raise DefinitionError([Problem("", "o texto JSON precisa estar em UTF-8")]) from None
        except RecursionError:
            raise DefinitionError([Problem("", f"JSON inválido: {TOO_DEEP}")]) from None
    if not isinstance(document, Mapping):
        raise DefinitionError([Problem("", "a definição precisa ser um objeto JSON")])
    problems = value_problems(document)
    if problems:
        raise DefinitionError(problems)
    problems = [Problem(key, message) for key, message in RESERVED.items() if key in document]
    if document.get("generation") == "none" and "release" in document:
        problems.append(Problem("release", "captura não usa política de liberação de PDF"))
    try:
        parsed = (
            CaptureFormDocument if document.get("generation") == "none" else PdfFormDocument
        ).model_validate({key: value for key, value in document.items() if key not in RESERVED})
    except ValidationError as exc:
        raise DefinitionError([*problems, *validation_problems(exc)]) from None
    if problems:
        raise DefinitionError(problems)
    return parsed
