"""Formulário público montado a partir dos metadados de tela (`app.forms.Screen`): o que o
template precisa para desenhar as etapas e a leitura do envio (form-urlencoded) de volta.

Campos de controle começam com `_` (um campo do pydantic não pode começar assim, então não
colidem com respostas): `_idem`, `_consent_version`, `_privacy`, `_mkt_*`, `_t_*` (UTMs) e o
honeypot `_website`.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from pydantic.fields import FieldInfo
from starlette.datastructures import FormData

from app.forms import CONTACT_KINDS, FormSpec, Question, QuestionKind, Screen, answer_fields
from app.intake import clean_tracking

if TYPE_CHECKING:
    from app.forms.definition.conditions import Conditions

CHOICE_KINDS = ("choice", "choices")
TRACKING_PREFIX = "_t_"
PRIVACY = "_privacy"
PRIVACY_TARGET = "consent-processamento"
PRIVACY_ERROR = "Marque esta opção para gerarmos o seu plano."
PRIVACY_LABEL = "Autorização para gerar o seu plano"
EMAIL_ERROR = "Informe um e-mail válido, por exemplo nome@example.test"
GENERAL_ERROR = "Confira esta resposta."
# O autor do formulário às vezes já escreve o "(opcional)" no rótulo: a página não o repete.
WRITTEN_OPTIONAL = re.compile(r"\(\s*opcional\s*\)", re.IGNORECASE)


@dataclass(frozen=True)
class FormPost:
    """Envio lido do formulário. `values` guarda o que foi digitado, para devolver à tela."""

    answers: dict[str, Any]
    values: dict[str, list[str]]
    privacy: bool
    marketing_email: bool
    marketing_whatsapp: bool
    consent_version: str
    idempotency_key: str
    tracking: dict[str, str]
    honeypot: bool
    privacy_supplied: bool = False
    partial_token: str | None = None
    partial_revision: int = 0
    partial_step: int = 0


def _texts(form: FormData, name: str) -> list[str]:
    return [value for value in form.getlist(name) if isinstance(value, str) and value.strip()]


def read_post(screen: Screen, form: FormData, conditions: Conditions | None = None) -> FormPost:
    """O envio lido do formulário. Com as condições do formulário (`FormSpec.conditions`), a
    pergunta que elas escondem para estas respostas fica de fora, também do que volta à tela."""
    answers: dict[str, Any] = {}
    values: dict[str, list[str]] = {}
    for step in screen.steps:
        for question in step.questions:
            given = _texts(form, question.name)
            if not given:
                continue  # em branco = não respondida (o modelo decide se pode)
            values[question.name] = given
            answers[question.name] = given if question.kind == "choices" else given[0]
    if conditions is not None and conditions.present:
        for name in conditions.evaluate(answers).hidden:
            answers.pop(name, None)
            values.pop(name, None)
    key = next(iter(_texts(form, "_idem")), "")
    return FormPost(
        partial_token=next(iter(_texts(form, "_partial_token")), None),
        answers=answers,
        values=values,
        privacy=_texts(form, PRIVACY) == ["1"],
        privacy_supplied=PRIVACY in form,
        marketing_email=bool(_texts(form, "_mkt_email")),
        marketing_whatsapp=bool(_texts(form, "_mkt_whatsapp")),
        consent_version=next(iter(_texts(form, "_consent_version")), ""),
        # A chave nasce na renderização: clique duplo e reenvio do mesmo formulário não criam
        # um segundo pedido. Sem chave válida, o envio vale por si.
        idempotency_key=key if 0 < len(key) <= 100 else str(uuid.uuid4()),
        tracking=clean_tracking(
            {
                name.removeprefix(TRACKING_PREFIX): value
                for name, value in form.items()
                if name.startswith(TRACKING_PREFIX) and isinstance(value, str)
            }
        ),
        honeypot=bool(_texts(form, "_website")),
    )


def _count(number: int, singular: str, plural: str) -> str:
    return f"{number} {singular if number == 1 else plural}"


def _wait(seconds: int) -> str:
    """O tempo de espera para a lead, arredondado para cima: em minutos até uma hora, depois em
    horas."""
    minutes = max(1, math.ceil(seconds / 60))
    if minutes < 60:
        return _count(minutes, "minuto", "minutos")
    return _count(math.ceil(seconds / 3600), "hora", "horas")


def too_many_submissions(limit: int, window: timedelta, retry_after: int) -> str:
    """Aviso da página quando a lead passou do limite de envios por e-mail (429). Não repete o
    e-mail nem nenhuma resposta."""
    hours = int(window.total_seconds() // 3600)
    period = "na última hora" if hours == 1 else f"nas últimas {hours} horas"
    return (
        f"Você já enviou este formulário {_count(limit, 'vez', 'vezes')} {period}, que é o máximo "
        f"por e-mail. Tente de novo daqui a cerca de {_wait(retry_after)}. Se precisar de ajuda, "
        "fale com a equipe da Leadkit."
    )


def queue_full(retry_after: int) -> str:
    """Aviso da página quando há pedidos demais em andamento (503)."""
    return (
        "Estamos com muitos pedidos agora e não conseguimos receber o seu. Suas respostas não "
        f"foram enviadas. Tente de novo daqui a cerca de {_wait(retry_after)}."
    )


def _message(error: Mapping[str, Any], kind: QuestionKind) -> str:
    """Mensagem em pt-BR por tipo de erro do pydantic. Nunca repete o valor recebido."""
    ctx = error.get("ctx") or {}
    match error["type"]:
        case "missing":
            if kind == "choice":
                return "Escolha uma opção para continuar."
            if kind == "choices":
                return "Marque pelo menos uma opção para continuar."
            return "Preencha este campo para continuar."
        case "string_too_short":
            return f"Escreva pelo menos {_count(ctx['min_length'], 'caractere', 'caracteres')}."
        case "string_too_long":
            return f"Use no máximo {_count(ctx['max_length'], 'caractere', 'caracteres')}."
        case "too_short":
            return f"Marque pelo menos {_count(ctx['min_length'], 'opção', 'opções')}."
        case "too_long":
            return f"Marque no máximo {_count(ctx['max_length'], 'opção', 'opções')}."
        case "literal_error" | "enum":
            return "Escolha uma das opções da lista."
        case "value_error" if kind == "email":
            return EMAIL_ERROR
        case "value_error":
            # Texto do validador do próprio formulário (escrito para o lead, sem o valor).
            message = str(ctx.get("error", "")).strip()
            return message[:1].upper() + message[1:] if message else GENERAL_ERROR
    return GENERAL_ERROR


def form_errors(spec: FormSpec, screen: Screen, post: FormPost) -> dict[str, str]:
    """Erros por pergunta (chave = `Question.name`) e do consentimento (`PRIVACY`), juntos,
    para a pessoa corrigir tudo de uma vez."""
    kinds = {question.name: question.kind for step in screen.steps for question in step.questions}
    errors: dict[str, str] = {}
    try:
        spec.answers_model.model_validate(post.answers)
    except ValidationError as exc:
        for error in exc.errors(include_input=False, include_url=False):
            name = str(error["loc"][0]) if error["loc"] else ""
            errors.setdefault(name, _message(error, kinds.get(name, "text")))
    if screen.consent.acquisition_method == "checkbox" and not post.privacy:
        errors[PRIVACY] = PRIVACY_ERROR
    elif screen.consent.acquisition_method == "submit" and post.privacy_supplied:
        errors[PRIVACY] = (
            "Este formulário registra sua concordância ao enviar, sem caixa de confirmação."
        )
    return errors


@dataclass(frozen=True)
class QuestionView:
    """Uma pergunta como a página a desenha. `required` é o que o modelo das respostas exige da
    pergunta (vira o atributo HTML `required`); `required_when_shown` é a obrigatória que tem
    condição: o modelo só a exige quando ela aparece, então a página não põe o atributo (sem
    JavaScript, ela aparece sempre, e travaria o envio de quem não a vê no caminho dela), e o
    JavaScript o põe quando a pergunta aparece."""

    question: Question
    required: bool
    min_length: int | None
    max_length: int | None
    value: str = ""
    selected: frozenset[str] = frozenset()
    error: str | None = None
    required_when_shown: bool = False

    @property
    def id(self) -> str:
        return f"q-{self.question.name}"

    @property
    def mandatory(self) -> bool:
        """A lead precisa responder (quando a pergunta aparece): não leva o "(opcional)"."""
        return self.required or self.required_when_shown

    @property
    def mark_optional(self) -> bool:
        """A página acrescenta o "(opcional)" ao rótulo: a resposta não é obrigatória e o autor não
        o escreveu no próprio rótulo."""
        return not self.mandatory and not WRITTEN_OPTIONAL.search(self.question.label)

    @property
    def draft(self) -> bool:
        """Vai para o rascunho no aparelho (nunca dados de contato)."""
        return not self.question.contact and self.question.kind not in CONTACT_KINDS

    @property
    def describedby(self) -> str:
        ids = [f"{self.id}-ajuda"] if self.question.help else []
        if self.error:
            ids.append(f"{self.id}-erro")
        return " ".join(ids)


@dataclass(frozen=True)
class StepView:
    number: int
    title: str
    help: str | None
    questions: tuple[QuestionView, ...]

    @property
    def optional(self) -> bool:
        return not any(question.mandatory for question in self.questions)

    @property
    def mark_optional(self) -> bool:
        """A página acrescenta o "(opcional)" ao título da etapa: toda pergunta dela é opcional e o
        autor não o escreveu no título."""
        return self.optional and not WRITTEN_OPTIONAL.search(self.title)


def _limit(info: FieldInfo, name: str) -> int | None:
    """`min_length`/`max_length` declarados no campo de `Answers` (viram atributos HTML)."""
    return next((getattr(item, name) for item in info.metadata if hasattr(item, name)), None)


def steps_view(
    spec: FormSpec,
    screen: Screen,
    values: Mapping[str, list[str]] | None = None,
    errors: Mapping[str, str] | None = None,
) -> tuple[StepView, ...]:
    fields = answer_fields(spec.answers_model)
    values, errors = values or {}, errors or {}
    conditional_required = spec.conditions.required_when_shown if spec.conditions else frozenset()
    views = []
    for number, step in enumerate(screen.steps, start=1):
        questions = []
        for question in step.questions:
            info = fields[question.name]
            given = [] if question.name in errors else values.get(question.name, [])
            questions.append(
                QuestionView(
                    question=question,
                    required=info.is_required(),
                    min_length=_limit(info, "min_length"),
                    max_length=_limit(info, "max_length"),
                    value=given[0] if given and question.kind not in CHOICE_KINDS else "",
                    selected=frozenset(given) if question.kind in CHOICE_KINDS else frozenset(),
                    error=errors.get(question.name),
                    required_when_shown=question.name in conditional_required,
                )
            )
        views.append(StepView(number, step.title, step.help, tuple(questions)))
    return tuple(views)


def conditions_data(spec: FormSpec) -> list[dict[str, Any]] | None:
    """As perguntas com as condições, para o bloco JSON da página (`Conditions.page_data`); None
    num formulário sem condição nenhuma: a página não leva o bloco, e o JavaScript não avalia nada."""
    conditions = spec.conditions
    return conditions.page_data if conditions is not None and conditions.present else None


def error_summary(steps: tuple[StepView, ...], errors: Mapping[str, str]) -> list[tuple[str, str]]:
    """Itens do resumo no topo: (id do campo para o link, rótulo da pergunta), na ordem da tela."""
    items = []
    for step in steps:
        for view in step.questions:
            if view.error:
                kind = view.question.kind
                items.append(
                    (f"{view.id}-0" if kind in CHOICE_KINDS else view.id, view.question.label)
                )
    if PRIVACY in errors:
        items.append((PRIVACY_TARGET, PRIVACY_LABEL))
    if "" in errors and steps:  # erro do modelo inteiro, sem campo próprio
        items.append(("etapa-1", errors[""]))
    return items
