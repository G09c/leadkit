"""O contrato do formulário (`FormSpec`) e o registro que o resolve (`FormRegistry`).

Todo formulário vive no banco (ADR 0013): a definição em JSON de cada versão publicada, que o
compilador (`app.forms.definition`) transforma neste contrato. A API, o pipeline e os adaptadores
não sabem de onde o formulário veio; eles pedem o formulário só pelo registro (`Deps.forms`), que no
app é o `StoredRegistry` (`app.forms.stored`): `current(slug)` para um envio novo e
`get(slug, version_number)` para tudo o que vem depois dele, que segue a versão gravada no envio.
Um formulário inativo não recebe envios novos (`InactiveForm`), e os que já existem seguem a versão
deles.

O contrato traz:
- `answers_model`: modelo Pydantic das respostas (validação no servidor, nunca no cliente);
- `conditions`: as perguntas e opções condicionais (`show_when`), com o avaliador que diz o que a
  lead vê para certas respostas (`app.forms.definition.conditions`); None, ou sem condição nenhuma,
  é um formulário em que tudo sempre aparece;
- `plan_model`: modelo Pydantic da saída do LLM (vira o schema de structured outputs);
- `instructions` e o prompt, de `templates`: só as respostas que o prompt cita vão para o LLM;
- `identity()`: quais respostas identificam a lead (e-mail obrigatório);
- `score()` opcional: a pontuação comercial calculada das respostas, no envio;
- `check_plan()` opcional: recebe o plano gerado (já validado pelo `plan_model`) e as respostas e
  devolve os problemas que o modelo do plano não vê sozinho (o valor fora da janela da fase, por
  exemplo); cada problema volta ao LLM na rodada de reparo, então cita só posição e regra, nunca
  texto do plano nem dados da lead;
- `screen`: metadados das páginas públicas (`Screen`, abaixo);
- `templates`: o prompt, o PDF e a prévia, já compilados no ambiente isolado (`app.forms.sandbox`);
- `example_plan`: o plano de exemplo, que o LLM falso e o modo de teste devolvem.

Para criar um formulário, crie do zero ou duplique um existente no admin (`/admin/forms/new`), ou
importe a definição (`/admin/forms/import`); para mudar um, edite o rascunho dele, teste e publique
(`app.forms.drafts`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo
from pydantic_core import PydanticCustomError

if TYPE_CHECKING:
    from app.forms.definition.conditions import Conditions
    from app.forms.definition.images import AssetRef
    from app.forms.definition.scoring import Scoring
    from app.forms.definition.templates import FormTemplates

# A primeira publicação usa versão 1. Bancos novos não recebem formulários automaticamente;
# envios importados que omitem o número conservam o default compatível da primeira versão.
FIRST_VERSION = 1


@dataclass(frozen=True)
class LeadIdentity:
    email: str
    name: str | None = None
    phone: str | None = None


@dataclass(frozen=True)
class ScoreResult:
    scores: dict[str, Any] = field(default_factory=dict)
    priority: str | None = None
    persona: str | None = None


# --- metadados de tela (páginas públicas) -----------------------------------------------------

QuestionKind = Literal["text", "email", "tel", "textarea", "choice", "choices"]


@dataclass(frozen=True)
class Option:
    """`value` é o que fica gravado nas respostas (estável: a pontuação depende dele);
    `label` é o texto exibido."""

    value: str
    label: str


# Os tipos de pergunta que são sempre dado de contato, marcados ou não (`Question.contact`).
CONTACT_KINDS = ("email", "tel")


@dataclass(frozen=True)
class Question:
    """Uma pergunta da tela; `name` é a chave da resposta em `Answers` (o alias, se houver).

    `kind`: text, email, tel, textarea, choice (uma opção) ou choices (várias; a resposta é uma
    lista). Obrigatoriedade e limites de tamanho vêm do modelo `Answers`, não daqui.
    `contact=True` marca dado de contato (nome, escritório, cidade): fica fora do rascunho salvo
    no aparelho. Perguntas `email` e `tel` (`CONTACT_KINDS`) ficam fora sempre.
    """

    name: str
    label: str
    kind: QuestionKind = "text"
    help: str | None = None
    options: tuple[Option, ...] = ()
    contact: bool = False
    autocomplete: str | None = None
    placeholder: str | None = None


@dataclass(frozen=True)
class Step:
    """Uma etapa do formulário. Sem JavaScript, todas as etapas aparecem numa página só."""

    title: str
    questions: tuple[Question, ...]
    help: str | None = None


@dataclass(frozen=True)
class ConsentText:
    """Textos do consentimento, identificados por `FormSpec.consent_text_version`: mudou o texto,
    suba a versão. O processamento é obrigatório; cada canal de marketing é opcional e só aparece
    com texto (WhatsApp só faz sentido se o formulário pede telefone)."""

    processing: str | None
    notice: str
    marketing_email: str | None = None
    marketing_whatsapp: str | None = None
    acquisition_method: Literal["checkbox", "submit"] = "checkbox"
    presentation: Literal["panel", "compact"] = "panel"
    position: Literal["first_step", "before_submit"] = "first_step"


@dataclass(frozen=True)
class NoticeLink:
    """O link de um aviso: o rótulo e o endereço, sempre https (a definição recusa o resto)."""

    label: str
    url: str


@dataclass(frozen=True)
class Notice:
    """Um aviso extra de uma página da lead: título, texto em parágrafos e, se houver, um link. É
    texto puro: as páginas o escapam."""

    title: str
    text: str
    link: NoticeLink | None = None

    @property
    def paragraphs(self) -> tuple[str, ...]:
        """O texto em parágrafos, separados por uma linha em branco."""
        text = self.text.replace("\r\n", "\n").replace("\r", "\n")
        return tuple(part.strip() for part in re.split(r"\n\s*\n", text) if part.strip())


@dataclass(frozen=True)
class Notices:
    """Os avisos das páginas que a lead vê depois do envio: a de espera e a de falha. Sem aviso, a
    página sai como sempre."""

    waiting: Notice | None = None
    failed: Notice | None = None


@dataclass(frozen=True)
class OpeningLogo:
    """Logo de abertura: referência declarada e endereço do conteúdo vinculado à versão."""

    src: str
    alt: str
    url: str | None = None


@dataclass(frozen=True)
class Screen:
    """Páginas públicas do formulário: entrada e etapas (`/p/{slug}`), espera e resultado
    (`/d/{public_id}`), com os avisos extras da espera e da falha. A prévia do plano no resultado
    vem de `FormSpec.templates`."""

    intro: str
    minutes: int
    steps: tuple[Step, ...]
    consent: ConsentText
    notices: Notices = Notices()
    opening_logo: OpeningLogo | None = None


def answer_fields(model: type[BaseModel]) -> dict[str, FieldInfo]:
    """Campos de `Answers` pela chave que chega no envio (o alias, se houver)."""
    return {(info.alias or name): info for name, info in model.model_fields.items()}


IDENTITY_ROLES = ("email", "name", "phone")


def identity_fields(spec: FormSpec) -> dict[str, str | None] | None:
    """Quais respostas a identidade do contrato lê, pelo papel (`email`, `name`, `phone`): a chave
    que chega no envio (o alias, se houver), ou None no papel que o formulário não coleta. A
    identidade é uma função; ela recebe respostas de sonda, em que cada campo traz o próprio nome,
    e o que ela devolve diz de qual campo veio cada dado. None se a identidade faz mais que ler as
    respostas: aí não dá para saber."""
    fields = spec.answers_model.model_fields
    markers = {f"\x1f{name}\x1f": name for name in fields}
    values: dict[str, Any] = {name: mark for mark, name in markers.items()}
    probe = spec.answers_model.model_construct(None, **values)  # sem validar: só as marcas
    try:
        lead = spec.identity(probe)
    except (AttributeError, TypeError, ValueError):
        return None
    found: dict[str, str | None] = {}
    for role in IDENTITY_ROLES:
        name = markers.get(getattr(lead, role) or "")
        found[role] = (fields[name].alias or name) if name is not None else None
    return found


def _check_screen(spec: FormSpec, screen: Screen) -> None:
    fields = answer_fields(spec.answers_model)
    questions = [question for step in screen.steps for question in step.questions]
    names = [question.name for question in questions]
    problems = [f"{name}: não existe em Answers" for name in names if name not in fields]
    problems += [f"{name}: repetida" for name in sorted(set(names)) if names.count(name) > 1]
    problems += [
        f"{question.name}: escolha sem opções"
        for question in questions
        if question.kind in ("choice", "choices") and not question.options
    ]
    problems += [
        f"{key}: obrigatória e fora da tela"
        for key, info in fields.items()
        if info.is_required() and key not in names
    ]
    if problems:
        raise ValueError(f"tela do formulário {spec.slug!r} inconsistente: {'; '.join(problems)}")


@dataclass(frozen=True)
class Handoff:
    receiver_id: UUID
    receiver_revision: int


@dataclass(frozen=True)
class Completion:
    """Próximo passo após salvar respostas; não altera geração nem autorização do PDF."""

    mode: Literal["result", "confirmation", "button", "redirect"] = "result"
    message: str = "Recebemos suas respostas. Obrigada por participar."
    button_label: str = "Continuar"
    url: str | None = None
    handoff: Handoff | None = None


@dataclass(frozen=True)
class FormSpec:
    slug: str
    version: str
    title: str
    consent_text_version: str
    answers_model: type[BaseModel]
    plan_model: type[BaseModel] | None
    identity: Callable[[Any], LeadIdentity]
    instructions: str | None
    prompt_template: str | None
    prompt_version: str
    example_plan: dict[str, Any] | None
    # O prompt, o PDF e a prévia, já compilados no ambiente isolado (`app.forms.sandbox`).
    templates: FormTemplates | None
    generation: Literal["pdf", "none"] = "pdf"
    completion: Completion = Completion()
    release_mode: Literal["automatic", "manual"] = "automatic"
    partial_capture: Mapping[str, Any] | None = None
    # As condições das perguntas e das opções: o avaliador que diz o que aparece para certas
    # respostas. O modelo das respostas já as aplica; quem lê o envio ou grava as respostas usa o
    # mesmo resultado (`Conditions.evaluate`).
    conditions: Conditions | None = None
    # Número da versão no registro: o envio grava este número, e o resto do pedido segue a mesma
    # versão. Não é o `version`, o texto `<slug>-v<N>` que vai no envio e no webhook, embora
    # andem juntos numa versão publicada no banco.
    version_number: int = FIRST_VERSION
    score: Callable[[Any], ScoreResult] | None = None
    screen: Screen | None = None
    check_plan: Callable[[Any, Any], list[str]] | None = None
    # A pontuação declarada: `score` é o `scoring.score` dela, e o admin lê o resto
    # (`scoring.run`: qual regra decidiu a prioridade e a persona).
    scoring: Scoring | None = None
    # Os rótulos dos campos do plano, pelo caminho dos nomes (`situacao`, `fases.foco`): só o admin
    # os lê, na tela do pedido. Campo sem rótulo não consta, e o admin mostra o nome dele.
    plan_labels: Mapping[str, str] = field(default_factory=dict)

    # Ausente em contratos manuais: nenhuma saída recebe dados sem classificação recuperável.
    plan_projector: Callable[[Mapping[str, Any], str], dict[str, Any]] | None = None

    def project_plan(
        self,
        document: Mapping[str, Any],
        *,
        audience: Literal["internal", "participant"] = "participant",
    ) -> dict[str, Any]:
        """Cópia JSON validada pela versão deste contrato, destinada à audiência pedida."""
        if (
            self.generation != "pdf"
            or self.plan_model is None
            or self.plan_projector is None
            or not self.version
            or not isinstance(self.version_number, int)
            or self.version_number < 1
        ):
            raise PlanProjectionError("plan_projection_unavailable")
        return self.plan_projector(document, audience)

    def require_generation(self) -> None:
        """Recusa operações de geração em uma captura, antes de chamar provedores."""
        if self.generation != "pdf":
            raise GenerationUnavailable("este formulário apenas captura respostas")

    def render_prompt(self, answers: BaseModel) -> str:
        self.require_generation()
        return self.templates.render_prompt(answers)

    def with_assets(self, assets: Mapping[str, AssetRef]) -> FormSpec:
        """O mesmo contrato com as imagens do formulário (`asset:<nome>`) da abertura, PDF e prévias, cada nome com o conteúdo fixado. A versão publicada leva o que fixou na publicação
        (`app.forms.assets.version_refs`); o rascunho, as imagens de agora."""
        screen = self.screen
        if screen and screen.opening_logo:
            logo = screen.opening_logo
            ref = assets.get(logo.src[6:])
            screen = replace(screen, opening_logo=replace(logo, url=ref.url if ref else None))
        return replace(
            self,
            screen=screen,
            templates=replace(self.templates, assets=dict(assets)) if self.templates else None,
        )

    def validate_plan(self, data: str, answers: BaseModel) -> BaseModel:
        """Plano do LLM (texto JSON) validado pelo `plan_model` e depois pelo `check_plan`. Os
        problemas do `check_plan` saem como `ValidationError` (tipo `plan_check`): seguem o mesmo
        caminho dos erros do modelo, da rodada de reparo ao `plan_invalid`."""
        self.require_generation()
        plan = self.plan_model.model_validate_json(data)
        problems = self.check_plan(plan, answers) if self.check_plan else []
        if problems:
            raise ValidationError.from_exception_data(
                self.plan_model.__name__,
                [
                    {
                        "type": PydanticCustomError(
                            "plan_check", "{problem}", {"problem": problem}
                        ),
                        "loc": (),
                        "input": None,
                    }
                    for problem in problems
                ],
                hide_input=True,
            )
        return plan

    def __post_init__(self) -> None:
        # Tela inconsistente com `Answers` quebra na importação, não no navegador do lead.
        if self.screen is not None:
            _check_screen(self, self.screen)


class PlanProjectionError(ValueError):
    """Projeção recusada sem incluir valores ou erros de validação do documento."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(
            "A projeção do plano está indisponível."
            if code == "plan_projection_unavailable"
            else "Não foi possível projetar o plano informado."
        )


class GenerationUnavailable(ValueError):
    """A operação exige um formulário com geração de PDF."""


class UnknownForm(LookupError):
    """O registro não tem o formulário, ou não tem a versão pedida dele."""


class InactiveForm(UnknownForm):
    """O formulário existe, mas está inativo: não recebe envios novos. Só `current(slug)` o
    levanta; `get(slug, versão)` segue atendendo os envios que já existem. Quem não trata o caso à
    parte o vê como um formulário desconhecido, e nada novo é servido."""


class FormRegistry(Protocol):
    """O ponto único que resolve um formulário: slug e versão entram, o contrato (`FormSpec`)
    sai. O motor pede formulários só por aqui (`Deps.forms`); o registro do app é o
    `app.forms.stored.StoredRegistry`, que lê as versões publicadas do banco.

    - `current(slug)`: a versão atual, a que recebe um envio novo. Um formulário inativo não
      recebe: levanta `InactiveForm`.
    - `get(slug, version_number)`: a versão com que um envio foi feito, ativo ou não. Tudo o que
      vem depois do envio passa por aqui, para o pedido inteiro seguir a versão que a lead
      respondeu.
    - `current_forms()`: a versão atual de cada formulário, ativo ou não: a lista do admin.

    A versão atual é a de maior número: publicar cria sempre a seguinte. O conteúdo de um par
    (slug, versão) não muda com o app no ar, então o que se compila de uma versão pode ficar em
    cache por slug e versão. Levanta `UnknownForm` para o que não conhece.
    """

    def current(self, slug: str) -> FormSpec: ...

    def get(self, slug: str, version_number: int) -> FormSpec: ...

    def current_forms(self) -> list[FormSpec]: ...
