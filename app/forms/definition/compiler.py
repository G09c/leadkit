"""A compilação: o documento conferido vira o contrato do formulário (`FormSpec`).

A conferência segue uma ordem e para no primeiro grupo de problemas, com todos os problemas do
grupo: a estrutura do documento; as regras entre os campos (nomes repetidos, identidade,
limites, condições, a pontuação); com os modelos montados, os exemplos (as respostas, o plano e a
pontuação das respostas de exemplo); e, por fim, os templates.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from app.forms import FIRST_VERSION, Completion, FormSpec, Handoff, OpeningLogo
from app.forms.definition import plan as plan_tree
from app.forms.definition import questions
from app.forms.definition.conditions import Conditions, condition_problems
from app.forms.definition.data import data_problems
from app.forms.definition.document import (
    DefinitionError,
    FormDocument,
    Problem,
    parse,
    validation_problems,
)
from app.forms.definition.notices import notices_of
from app.forms.definition.phases import build_calendar, phases_problems
from app.forms.definition.scoring import compile_scoring, example_problems, scoring_problems
from app.forms.definition.templates import compile_templates, released_problems

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _stop_on(problems: list[Problem]) -> None:
    if problems:
        raise DefinitionError(problems)


def _rule_problems(definition: FormDocument) -> list[Problem]:
    completion_problems = []
    partial = definition.partial_capture
    if partial:
        first = {q.name: q for q in definition.steps[0].questions}
        consent = definition.consent
        permission = first.get(partial.contact_permission_field)
        identities = definition.identity.model_dump().values()
        if consent.acquisition_method != "checkbox" or consent.position != "first_step":
            completion_problems.append(
                Problem("partial_capture", "exige consentimento por caixa na primeira etapa")
            )
        if any(name and name not in first for name in identities):
            completion_problems.append(
                Problem("partial_capture", "a identidade deve estar na primeira etapa")
            )
        if (
            permission is None
            or permission.kind != "choice"
            or permission.show_when is not None
            or not permission.required
            or partial.contact_permission_value not in {o.value for o in permission.options}
        ):
            completion_problems.append(
                Problem(
                    "partial_capture",
                    "escolha autorização obrigatória e incondicional na primeira etapa",
                )
            )
    if definition.completion.mode in ("button", "redirect") and not definition.completion.url:
        completion_problems.append(Problem("completion.url", "informe o endereço HTTPS do destino"))
    if definition.completion.mode in ("result", "confirmation") and definition.completion.url:
        completion_problems.append(Problem("completion.url", "este modo não usa destino externo"))
    if definition.generation == "none" and definition.completion.mode == "result":
        completion_problems.append(Problem("completion.mode", "resultado PDF exige geração de PDF"))
    if definition.completion.handoff and definition.completion.mode not in ("button", "redirect"):
        completion_problems.append(
            Problem("completion.handoff", "encaminhamento exige botão ou redirecionamento")
        )
    return completion_problems + (
        questions.question_problems(definition)
        + condition_problems(definition)
        + (
            plan_tree.plan_problems(definition.plan, definition.data)
            if definition.generation == "pdf"
            else []
        )
        + data_problems(definition.data)
        + (phases_problems(definition) if definition.generation == "pdf" else [])
        + scoring_problems(definition)
        + (released_problems(definition) if definition.generation == "pdf" else [])
    )


def _example(
    model: type[BaseModel], value: Mapping[str, Any], path: str, problems: list[Problem]
) -> BaseModel | None:
    """O exemplo validado pelo modelo da própria versão, ou None com os problemas anotados."""
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        problems += validation_problems(exc, path)
        return None


def compile_definition(
    document: Mapping[str, Any] | str | bytes,
    *,
    version_number: int = FIRST_VERSION,
    clock: Clock = utc_now,
) -> FormSpec:
    """O contrato do formulário da definição. Levanta `DefinitionError` (formato e parâmetros em
    `app.forms.definition`)."""
    definition = parse(document)
    _stop_on(_rule_problems(definition))
    conditions = Conditions(definition.steps)
    answers = questions.answers_model(definition.steps, conditions)
    plan = (
        plan_tree.plan_model(definition.plan, definition.data)
        if definition.generation == "pdf"
        else None
    )
    projector = plan_tree.plan_projector(definition.plan, plan) if plan is not None else None
    problems: list[Problem] = []
    example_answers = _example(answers, definition.examples.answers, "examples.answers", problems)
    example_plan = (
        _example(plan, definition.examples.plan, "examples.plan", problems) if plan else None
    )
    _stop_on(problems)
    assert example_answers is not None
    label = f"{definition.slug}-v{version_number}"
    scoring = compile_scoring(definition, label=label)
    _stop_on(example_problems(scoring, example_answers))
    identity = questions.identity_of(definition.identity)
    calendar = None
    templates = instructions = None
    if definition.generation == "pdf":
        calendar = build_calendar(definition)
        templates, instructions = compile_templates(
            definition,
            plan_model=plan,
            project_plan=projector,
            example_answers=example_answers,
            example_plan=example_plan,
            lead_name=identity(example_answers).name,
            clock=clock,
            scoring=scoring,
            calendar=calendar,
        )
    return FormSpec(
        partial_capture=definition.partial_capture.model_dump()
        if definition.partial_capture
        else None,
        generation=definition.generation,
        slug=definition.slug,
        version=label,
        title=definition.title,
        consent_text_version=definition.consent.version,
        answers_model=answers,
        conditions=conditions,
        plan_model=plan,
        plan_projector=projector,
        plan_labels=plan_tree.plan_labels(definition.plan.fields) if definition.plan else {},
        identity=identity,
        instructions=instructions,
        prompt_template=definition.prompt,
        prompt_version=label,
        example_plan=example_plan.model_dump(mode="json") if example_plan else None,
        version_number=version_number,
        score=scoring.score if scoring is not None else None,
        scoring=scoring,
        check_plan=(calendar.checker(clock) if calendar is not None and calendar.fields else None),
        screen=replace(
            questions.screen_of(
                intro=definition.intro,
                minutes=definition.minutes,
                steps=definition.steps,
                consent=definition.consent,
                identity=definition.identity,
            ),
            opening_logo=OpeningLogo(**definition.opening_logo.model_dump())
            if definition.opening_logo
            else None,
            notices=notices_of(definition.notices),  # os avisos da espera e da falha
        ),
        templates=templates,
        completion=Completion(
            **definition.completion.model_dump(exclude={"handoff"}),
            handoff=Handoff(**definition.completion.handoff.model_dump())
            if definition.completion.handoff
            else None,
        ),
        release_mode=definition.release.mode if definition.release else "automatic",
    )
