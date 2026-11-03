"""O que sai das etapas e das perguntas da definição: o modelo das respostas, a tela pública, a
identidade da lead e os rótulos das opções.

Os normalizadores são os do reforma-2027, iguais para todo formulário:
- e-mail em minúsculas e sem espaços nas pontas;
- telefone brasileiro só com dígitos, sem o 55: DDD e número, com 10 ou 11 dígitos;
- escolha múltipla sem repetição, na ordem em que a lead marcou; um texto só vale como a lista
  dessa opção;
- resposta opcional em branco (texto vazio ou nenhuma opção marcada) vale como não respondida.
Uma resposta obrigatória em branco é recusada como ausente, como no envio pela página.

As condições (`show_when`, ver `app.forms.definition.conditions`) mudam o modelo só nos formulários
que as têm: a pergunta escondida perde a resposta, a obrigatória que tem condição só é exigida
quando aparece, e a opção escondida é recusada no campo da pergunta.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from types import SimpleNamespace
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    create_model,
    model_validator,
)
from pydantic_core import PydanticKnownError

from app.forms import ConsentText, LeadIdentity, Option, Question, Screen, Step
from app.forms.definition.document import (
    ConsentDoc,
    FormDocument,
    IdentityDoc,
    Problem,
    QuestionDoc,
    StepDoc,
)

if TYPE_CHECKING:
    from app.forms.definition.conditions import Conditions

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_CHARS_RE = re.compile(r"[\d\s()+.\-]+")
EMAIL_MAX_LENGTH = 254
PHONE_MAX_LENGTH = 24
# O tamanho máximo que o editor do admin e o modelo inicial escrevem numa resposta de texto que não
# diz o dela: a definição exige um no texto e no texto longo.
TEXT_MAX_LENGTH = {"text": 300, "textarea": 2000}
CHOICE_KINDS = ("choice", "choices")
TEXT_KINDS = ("text", "textarea", "email", "tel")
# O tipo da pergunta de cada papel da identidade da lead.
IDENTITY_KINDS = {"email": "email", "name": "text", "phone": "tel"}


def questions_of(steps: list[StepDoc]) -> Iterator[QuestionDoc]:
    for step in steps:
        yield from step.questions


# --- conferência ----------------------------------------------------------------------------


def question_problems(definition: FormDocument) -> list[Problem]:
    """O que a estrutura não confere sozinha nas etapas, na identidade e no consentimento."""
    problems: list[Problem] = []
    by_name: dict[str, QuestionDoc] = {}
    for i, step in enumerate(definition.steps):
        for j, question in enumerate(step.questions):
            at = f"steps.{i}.questions.{j}"
            if question.name in by_name:
                problems.append(Problem(f"{at}.name", "outra pergunta já usa este nome"))
            by_name.setdefault(question.name, question)
            problems += _question_problems(question, at)
    problems += _identity_problems(definition.identity, by_name)
    if definition.consent.marketing.whatsapp and definition.identity.phone is None:
        problems.append(
            Problem(
                "consent.marketing.whatsapp",
                "o canal WhatsApp precisa do telefone da lead (identity.phone)",
            )
        )
    return problems


def _question_problems(question: QuestionDoc, at: str) -> list[Problem]:
    problems: list[Problem] = []

    def refuse(key: str, message: str) -> None:
        problems.append(Problem(f"{at}.{key}", message))

    choice = question.kind in CHOICE_KINDS
    if choice and question.options is None:
        refuse("options", "a pergunta de escolha precisa das opções")
    if not choice and question.options is not None:
        refuse("options", "só perguntas de escolha têm opções")
    if question.kind != "choices":
        for key in ("min_choices", "max_choices"):
            if getattr(question, key) is not None:
                refuse(key, "só a escolha múltipla tem mínimo e máximo de escolhas")
    if question.kind not in TEXT_KINDS:
        for key in ("min_length", "max_length"):
            if getattr(question, key) is not None:
                refuse(key, "só respostas de texto têm limite de tamanho")
        for key in ("placeholder", "autocomplete"):
            if getattr(question, key) is not None:
                refuse(key, f"só perguntas de texto têm {key}")
    elif question.kind in ("email", "tel"):
        if question.min_length is not None:
            refuse("min_length", "e-mail e telefone não têm tamanho mínimo")
        if question.kind == "email" and (question.max_length or 0) > EMAIL_MAX_LENGTH:
            refuse("max_length", f"o e-mail tem no máximo {EMAIL_MAX_LENGTH} caracteres")
    elif question.max_length is None:
        refuse("max_length", "a resposta de texto precisa de um tamanho máximo")
    elif (question.min_length or 0) > question.max_length:
        refuse("min_length", f"o mínimo passa do máximo ({question.max_length})")
    seen: set[str] = set()
    for k, option in enumerate(question.options or ()):
        if option.value in seen:
            refuse(f"options.{k}.value", "outra opção da pergunta já usa este valor")
        seen.add(option.value)
    if question.kind == "choices" and seen:
        high = question.max_choices or len(seen)
        if high > len(seen):
            refuse("max_choices", f"passa do número de opções ({len(seen)})")
        elif (question.min_choices or 1) > high:
            refuse("min_choices", f"o mínimo passa do máximo ({high})")
    return problems


def _identity_problems(identity: IdentityDoc, by_name: dict[str, QuestionDoc]) -> list[Problem]:
    """O e-mail identifica a lead e é obrigatório; nome e telefone, se houver, vêm de perguntas
    de texto e de telefone."""
    problems: list[Problem] = []
    email = by_name.get(identity.email)
    if email is None:
        problems.append(Problem("identity.email", "não há pergunta com este nome"))
    elif email.kind != IDENTITY_KINDS["email"] or not email.required:
        problems.append(Problem("identity.email", "precisa ser uma pergunta de e-mail obrigatória"))
    for key, label in (("name", "texto"), ("phone", "telefone")):
        name = getattr(identity, key)
        if name is None:
            continue
        question = by_name.get(name)
        if question is None:
            problems.append(Problem(f"identity.{key}", "não há pergunta com este nome"))
        elif question.kind != IDENTITY_KINDS[key]:
            problems.append(Problem(f"identity.{key}", f"precisa ser uma pergunta de {label}"))
    return problems


# --- o que a compilação monta ---------------------------------------------------------------


def _blank(value: Any) -> bool:
    return (isinstance(value, str) and not value.strip()) or (isinstance(value, list) and not value)


def _unanswered(value: Any) -> Any:
    """Resposta opcional em branco: não respondida."""
    return None if _blank(value) else value


def _present(value: Any) -> Any:
    """Resposta obrigatória em branco: ausente, como quando o campo nem chega no envio."""
    if _blank(value):
        raise PydanticKnownError("missing")
    return value


class _Unavailable:
    """No lugar de uma opção escolhida que a condição dela escondeu, até o campo recusá-la."""


UNAVAILABLE_OPTION = "esta opção não está disponível para as respostas anteriores"


def _available(value: Any) -> Any:
    """Recusa, no campo da pergunta, a opção que estava escondida (`_Unavailable`)."""
    items = value if isinstance(value, list) else [value]
    if any(isinstance(item, _Unavailable) for item in items):
        raise ValueError(UNAVAILABLE_OPTION)
    return value


def _once(value: Any) -> Any:
    """Cada opção marcada uma vez, na ordem em que chegou, antes de contar as escolhas. Um texto só
    vale como a lista dessa opção: formulários HTML mandam o texto, e não uma lista, quando há uma
    única caixa marcada. O texto em branco segue para a conferência de resposta ausente."""
    if isinstance(value, str) and value.strip():
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(dict.fromkeys(value))
    return value


def _email(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().lower()
    if not EMAIL_RE.match(value):
        raise ValueError("e-mail inválido")
    return value


def _phone(value: str | None) -> str | None:
    """Só DDD e número (10 ou 11 dígitos). Aceita com ou sem +55 e com qualquer pontuação."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", value)
    if not PHONE_CHARS_RE.fullmatch(value):
        digits = ""  # letras ou símbolos: recusa sem tentar adivinhar o número
    if len(digits) in (12, 13) and digits.startswith("55"):
        digits = digits[2:]
    if len(digits) not in (10, 11) or digits[0] == "0":
        raise ValueError("informe o DDD e o número, por exemplo (11) 91234-5678")
    return digits


def _field(question: QuestionDoc) -> tuple[Any, Any]:
    """(tipo, `Field`) da resposta de uma pergunta. Os limites ficam no `Field`, onde a página
    pública os lê para os atributos do HTML. Uma pergunta com condição nunca é exigida pelo
    campo: quando ela aparece e é obrigatória, o modelo manda a resposta em branco, e o campo a
    recusa como ausente (`answers_model`)."""
    required = question.required and question.show_when is None
    blank = BeforeValidator(_present if question.required else _unanswered)
    default = ... if required else None
    if question.kind in CHOICE_KINDS:
        values = tuple(option.value for option in question.options or ())
        option = Literal[values]
        # Corre antes dos outros validadores: eles só veem a resposta que o campo aceita.
        guard = [BeforeValidator(_available)] if _conditional_options(question) else []
        if question.kind == "choice":
            kind: Any = option if required else option | None
            return Annotated[kind, blank, *guard], Field(default=default)
        kind = list[option] if required else list[option] | None
        limits = Field(
            default=default,
            min_length=question.min_choices or 1,
            max_length=question.max_choices or len(values),
        )
        return Annotated[kind, blank, BeforeValidator(_once), *guard], limits
    text: Any = str if required else str | None
    if question.kind == "email":
        size = Field(default=default, max_length=question.max_length or EMAIL_MAX_LENGTH)
        return Annotated[text, blank, AfterValidator(_email)], size
    if question.kind == "tel":
        size = Field(default=default, max_length=question.max_length or PHONE_MAX_LENGTH)
        return Annotated[text, blank, AfterValidator(_phone)], size
    size = Field(default=default, min_length=question.min_length, max_length=question.max_length)
    return Annotated[text, blank], size


def _conditional_options(question: QuestionDoc) -> bool:
    return any(option.show_when is not None for option in question.options or ())


def conditioned_answers(conditions: Conditions, data: Any, *, names: set[str] | None = None) -> Any:
    """Aplica as mesmas condições no envio completo e nas etapas já alcançadas da captura."""
    if not isinstance(data, Mapping):
        return data
    seen = conditions.evaluate(data)
    active = set(seen.visible)
    required = active if names is None else active & names
    shaped = {key: value for key, value in data.items() if key not in seen.hidden}
    for name in conditions.required_when_shown & required:
        if shaped.get(name) is None or _blank(shaped[name]):
            shaped[name] = ""
    for question in conditions.page_data:
        name = question["name"]
        options = question.get("options", ())
        if name not in active or not any("show_when" in option for option in options):
            continue
        known = {option["value"] for option in options}

        def refuse(item: Any, known=known, available=seen.options[name]) -> Any:
            if isinstance(item, str) and item in known and item not in available:
                return _Unavailable()
            return item

        chosen = shaped.get(name)
        if names is not None and name not in names:
            # Uma edição anterior pode invalidar a opção salva; a participante a preencherá depois.
            if isinstance(chosen, list):
                valid = [item for item in chosen if item in seen.options[name]]
                if valid:
                    shaped[name] = valid
                else:
                    shaped.pop(name, None)
            elif chosen is not None and chosen not in seen.options[name]:
                shaped.pop(name, None)
            continue
        if isinstance(chosen, list):
            shaped[name] = [refuse(item) for item in chosen]
        elif chosen is not None:
            shaped[name] = refuse(chosen)
    return shaped


def _given(conditions: Conditions) -> Callable[[Any, Any], Any]:
    def given(cls: Any, data: Any) -> Any:
        return conditioned_answers(conditions, data)

    return given


def answers_model(steps: list[StepDoc], conditions: Conditions | None = None) -> type[BaseModel]:
    """O modelo das respostas: um campo por pergunta, na ordem da tela, sem campos a mais. Com as
    condições (`conditions`) de um formulário que as tem, as respostas passam antes pela
    visibilidade que elas calculam."""
    fields = {question.name: _field(question) for question in questions_of(steps)}
    checks: dict[str, Any] = {}
    if conditions is not None and conditions.present:
        checks["_conditions"] = model_validator(mode="before")(_given(conditions))
    return create_model(
        "Answers",
        __config__=ConfigDict(extra="forbid", str_strip_whitespace=True),
        __validators__=checks,
        **fields,
    )


def screen_of(
    *, intro: str, minutes: int, steps: list[StepDoc], consent: ConsentDoc, identity: IdentityDoc
) -> Screen:
    """A tela pública. A pergunta do nome da lead é dado de contato, como as de e-mail e telefone:
    fica fora do rascunho guardado no aparelho."""
    return Screen(
        intro=intro,
        minutes=minutes,
        steps=tuple(
            Step(
                title=step.title,
                help=step.help,
                questions=tuple(
                    Question(
                        name=question.name,
                        label=question.label,
                        kind=question.kind,
                        help=question.help,
                        options=tuple(
                            Option(option.value, option.label) for option in question.options or ()
                        ),
                        contact=question.contact or question.name == identity.name,
                        autocomplete=question.autocomplete,
                        placeholder=question.placeholder,
                    )
                    for question in step.questions
                ),
            )
            for step in steps
        ),
        consent=ConsentText(
            processing=consent.processing,
            notice=consent.notice,
            marketing_email=consent.marketing.email,
            marketing_whatsapp=consent.marketing.whatsapp,
            acquisition_method=consent.acquisition_method,
            presentation=consent.presentation,
            position=consent.position,
        ),
    )


def identity_of(identity: IdentityDoc) -> Callable[[Any], LeadIdentity]:
    """Quais respostas identificam a lead: o e-mail sempre; nome e telefone, se houver."""

    def identify(answers: Any) -> LeadIdentity:
        return LeadIdentity(
            email=getattr(answers, identity.email),
            name=getattr(answers, identity.name) if identity.name else None,
            phone=getattr(answers, identity.phone) if identity.phone else None,
        )

    return identify


def labels_of(steps: list[StepDoc]) -> Callable[[BaseModel], SimpleNamespace]:
    """O rótulo da opção escolhida em cada pergunta de escolha (várias opções: separadas por
    vírgula, na ordem das respostas); None na pergunta opcional sem resposta."""
    tables = {
        question.name: {option.value: option.label for option in question.options or ()}
        for question in questions_of(steps)
        if question.kind in CHOICE_KINDS
    }

    def labels(answers: BaseModel) -> SimpleNamespace:
        chosen: dict[str, str | None] = {}
        for name, table in tables.items():
            value = getattr(answers, name)
            if value is None:
                chosen[name] = None
            elif isinstance(value, list):
                chosen[name] = ", ".join(table[item] for item in value)
            else:
                chosen[name] = table[value]
        return SimpleNamespace(**chosen)

    return labels
