"""O que as instruções e o prompt de uma definição leem dela, o que vai ao LLM ou o valida, e o que
muda entre duas definições do mesmo formulário.

O que vai ao LLM (`llm_parts`) decide se a publicação precisa de um teste do rascunho: é a
definição inteira, menos o que se sabe que não vai. Ficam de fora a página pública (título,
introdução e tempo), os avisos das páginas de espera e de falha, as etapas e as perguntas, a
identidade, o consentimento, o PDF, a prévia e os exemplos, e o rótulo (`label`) dos campos do plano,
que só o admin lê. Dos dados do formulário, contam só os blocos que as instruções ou o prompt leem, as
janelas das fases e as listas de onde o modelo do plano tira as opções (`values_from`); das
perguntas, só o que o prompt lê delas: o tipo e os valores das opções, pelas respostas
(`answers`), e também os rótulos das opções, por `labels`. As condições das perguntas e das opções
(`show_when`) contam inteiras: elas decidem quais respostas chegam ao prompt, mesmo as de uma
pergunta que ele não cita, porque uma condição sobre outra pergunta a esconde. A pontuação
(`scoring`, com os pontos das opções) conta só se o prompt lê `scores`. Um bloco novo da definição
conta até alguém dizer aqui que ele não vai ao LLM: na dúvida, a publicação pede o teste.

Tudo aqui lê o documento da definição como o banco o guarda, sem compilar: serve também a uma
versão guardada que não compila mais. Um template que não compila conta como se lesse tudo. A
impressão digital do que vai ao LLM lê o documento como está: os testes e as publicações já guardam
a de hoje. Só a comparação por seção (`compare`) lê a forma normal (`app.forms.definition.normal`),
sem os padrões do formato escritos por extenso.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.forms.definition.condition_text import condition_sentence, targets_of
from app.forms.definition.document import ConsentDoc, fingerprint
from app.forms.definition.normal import normalized
from app.forms.sandbox import SandboxTemplate, TemplateProblem

# O que não vai ao LLM nem o valida, nem pelo prompt, nem pelo modelo do plano. Os dados voltam em
# parte, pelo que o prompt lê deles, pelas janelas das fases e pelas listas do modelo do plano, e as
# perguntas, pelo que o prompt lê delas (`llm_parts`). O endereço (`slug`) só muda antes da
# primeira publicação, e o prompt não o recebe.
NOT_FOR_LLM = frozenset(
    {
        "slug",
        "title",
        "intro",
        "opening_logo",
        "minutes",
        "steps",
        "identity",
        "consent",
        "pdf",
        "preview",
    }
    | {"examples", "scoring", "notices", "completion", "generation", "release", "partial_capture"}
)
# O nome de cada parte que vai ao LLM, para a tela dizer o que mudou.
LLM_PART_NAMES = {
    "instructions": "as instruções",
    "prompt": "o prompt",
    "plan": "o modelo do plano",
    "data": "os dados do formulário que o prompt lê ou que as fases e o modelo do plano usam",
    "phases": "as fases com datas",
    "scoring": "a pontuação que o prompt lê",
    "answers": "as perguntas que o prompt cita",
    "labels": "os rótulos das opções que o prompt cita",
    "conditions": "as condições das perguntas e das opções",
}


@dataclass(frozen=True)
class Reads:
    """O que as instruções e o prompt leem da definição, na ordem em que aparecem. None: o
    template usa a variável inteira, e qualquer campo dela pode ir ao LLM."""

    answers: tuple[str, ...] | None  # as respostas que o prompt cita (`answers.<pergunta>`)
    labels: tuple[str, ...] | None  # os rótulos das opções (`labels.<pergunta>`)
    data: tuple[str, ...] | None  # os blocos dos dados do formulário, nas instruções ou no prompt
    scores: tuple[str, ...] | None  # as notas que o prompt cita (`scores.<nota>`)

    @property
    def questions(self) -> tuple[str, ...] | None:
        """As perguntas cuja resposta ou rótulo vai ao LLM, sem repetir."""
        if self.answers is None or self.labels is None:
            return None
        return tuple(dict.fromkeys((*self.answers, *self.labels)))


def _template(document: Mapping[str, Any], key: str) -> SandboxTemplate | None:
    source = document.get(key)
    if not isinstance(source, str):
        return None
    try:
        return SandboxTemplate(source, name=key, html=False)
    except TemplateProblem:
        return None


def _merge(*found: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if any(item is None for item in found):
        return None
    return tuple(dict.fromkeys(name for item in found if item for name in item))


def reads(document: Mapping[str, Any]) -> Reads:
    """O que as instruções e o prompt da definição leem. As instruções só recebem `data`; o prompt,
    também as respostas e os rótulos (`app.forms.definition.templates`)."""
    instructions = _template(document, "instructions")
    prompt = _template(document, "prompt")
    if prompt is None:
        return Reads(None, None, None, None)
    return Reads(
        answers=prompt.reads("answers"),
        labels=prompt.reads("labels"),
        data=_merge(
            prompt.reads("data"), instructions.reads("data") if instructions is not None else None
        ),
        scores=prompt.reads("scores"),
    )


# --- o que vai ao LLM -------------------------------------------------------------------------


def _questions(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """As perguntas da definição pelo nome, sem conferir a estrutura (pode não compilar)."""
    found: dict[str, Mapping[str, Any]] = {}
    steps = document.get("steps")
    for step in steps if isinstance(steps, list) else ():
        questions = step.get("questions") if isinstance(step, Mapping) else None
        for question in questions if isinstance(questions, list) else ():
            if isinstance(question, Mapping) and isinstance(question.get("name"), str):
                found.setdefault(question["name"], question)
    return found


def _options(question: Mapping[str, Any], *, labels: bool) -> list[Any]:
    options = question.get("options")
    if not isinstance(options, list):
        return []
    if labels:
        return options
    return [option.get("value") if isinstance(option, Mapping) else option for option in options]


def _cited(
    questions: Mapping[str, Mapping[str, Any]], names: tuple[str, ...] | None, *, labels: bool
) -> dict[str, Any]:
    chosen = list(questions) if names is None else [name for name in names if name in questions]
    return {
        name: {
            "kind": questions[name].get("kind"),
            "options": _options(questions[name], labels=labels),
        }
        for name in chosen
    }


def _conditions(document: Mapping[str, Any]) -> dict[str, Any]:
    """As condições (`show_when`) das perguntas e das opções, pelo nome da pergunta. Vazio num
    formulário sem condições."""
    found: dict[str, Any] = {}
    for name, question in _questions(document).items():
        options = {
            str(option.get("value")): option["show_when"]
            for option in _options(question, labels=True)
            if isinstance(option, Mapping) and option.get("show_when") is not None
        }
        shown = {"show_when": question.get("show_when"), "options": options}
        if shown["show_when"] is not None or options:
            found[name] = shown
    return found


def _scoring(document: Mapping[str, Any]) -> dict[str, Any]:
    """A pontuação como o prompt a lê: o bloco `scoring` e os pontos de cada opção."""
    points = {
        name: {
            option.get("value"): option.get("points")
            for option in _options(question, labels=True)
            if isinstance(option, Mapping)
        }
        for name, question in _questions(document).items()
    }
    return {"scoring": document.get("scoring"), "points": points}


def _phase_windows(document: Mapping[str, Any]) -> tuple[str, ...]:
    """O bloco de janelas das fases com datas, se a definição tem: as fases o leem sem passar pelo
    prompt, e uma data que muda ali muda o calendário e a conferência do plano."""
    phases = document.get("phases")
    name = phases.get("windows") if isinstance(phases, Mapping) else None
    return (name,) if isinstance(name, str) else ()


def _plan_lists(document: Mapping[str, Any]) -> tuple[str, ...]:
    """As listas dos dados de onde os campos de opções do modelo do plano tiram os valores
    (`values_from`: um nome, ou uma lista de nomes), em qualquer nível: o `enum` do schema que o
    LLM recebe e a conferência do plano saem delas, mesmo que o prompt não as leia."""
    names: list[str] = []

    def visit(node: Any) -> None:
        if not isinstance(node, Mapping):
            return
        source = node.get("values_from")
        if isinstance(source, str):
            names.append(source)
        elif isinstance(source, list):
            names.extend(name for name in source if isinstance(name, str))
        visit(node.get("items"))
        fields = node.get("fields")
        for child in fields if isinstance(fields, list) else ():
            visit(child)

    plan = document.get("plan")
    fields = plan.get("fields") if isinstance(plan, Mapping) else None
    for field in fields if isinstance(fields, list) else ():
        visit(field)
    return tuple(dict.fromkeys(names))


def _without_labels(plan: Mapping[str, Any]) -> dict[str, Any]:
    """O modelo do plano sem o `label` dos campos: o rótulo é só do admin, e rotular um campo não
    pede o teste do rascunho nem muda a impressão digital de quem não rotula."""

    def clean(node: Any) -> Any:
        if not isinstance(node, Mapping):
            return node
        kept = {key: value for key, value in node.items() if key != "label"}
        if "items" in kept:
            kept["items"] = clean(kept["items"])
        if isinstance(kept.get("fields"), list):
            kept["fields"] = [clean(child) for child in kept["fields"]]
        return kept

    fields = plan.get("fields")
    if not isinstance(fields, list):
        return dict(plan)
    return {**plan, "fields": [clean(node) for node in fields]}


def llm_parts(document: Mapping[str, Any]) -> dict[str, Any]:
    """O que vai ao LLM ou o valida, parte por parte (as chaves de `LLM_PART_NAMES` e as que o
    formato ganhar depois). A pontuação só entra se o prompt lê `scores`: uma definição que não lê
    mantém as partes e a impressão digital de antes dela."""
    found = reads(document)
    parts = {key: value for key, value in document.items() if key not in NOT_FOR_LLM}
    if isinstance(parts.get("plan"), Mapping):
        parts["plan"] = _without_labels(parts["plan"])
    data = document.get("data")
    blocks = data if isinstance(data, Mapping) else {}
    used = (
        None
        if found.data is None
        else [*found.data, *_phase_windows(document), *_plan_lists(document)]
    )
    parts["data"] = dict(blocks) if used is None else {n: blocks[n] for n in used if n in blocks}
    questions = _questions(document)
    parts["answers"] = _cited(questions, found.answers, labels=False)
    parts["labels"] = _cited(questions, found.labels, labels=True)
    if found.scores != ():
        parts["scoring"] = _scoring(document)
    # Só quando há condições: a impressão digital de um formulário sem elas é a de antes.
    if conditions := _conditions(document):
        parts["conditions"] = conditions
    return parts


def llm_fingerprint(document: Mapping[str, Any]) -> str:
    """A impressão digital do que vai ao LLM ou o valida: dois rascunhos com a mesma passaram pelo
    mesmo teste."""
    return fingerprint(llm_parts(document))


def llm_changes(old: Mapping[str, Any], new: Mapping[str, Any]) -> tuple[str, ...]:
    """O que mudou entre as duas definições e vai ao LLM, por extenso ("o prompt"); vazio se nada
    que vai ao LLM mudou."""
    before, after = llm_parts(old), llm_parts(new)
    changed = [key for key in dict.fromkeys((*before, *after)) if before.get(key) != after.get(key)]
    return tuple(LLM_PART_NAMES.get(key, f"a parte {key} da definição") for key in changed)


# --- os valores das opções --------------------------------------------------------------------


@dataclass(frozen=True)
class DroppedValue:
    """Um valor de opção da versão publicada que o rascunho não grava mais na mesma pergunta."""

    question: str  # o nome da pergunta na versão publicada
    label: str  # o rótulo dela na versão publicada
    value: str
    option: str  # o rótulo da opção na versão publicada


def dropped_option_values(
    published: Mapping[str, Any], draft: Mapping[str, Any]
) -> tuple[DroppedValue, ...]:
    """Os valores de opção que a versão publicada grava e o rascunho não grava mais na mesma
    pergunta (pelo nome): a opção saiu, o valor mudou, ou a pergunta saiu, mudou de nome ou deixou
    de ser de escolha. Os envios novos não trazem mais esses valores, e o receptor do webhook pode
    depender deles."""
    before, after = _questions(published), _questions(draft)
    found = []
    for name, question in before.items():
        now = after.get(name)
        kept = {
            option.get("value")
            for option in (_options(now, labels=True) if now is not None else ())
            if isinstance(option, Mapping)
        }
        for option in _options(question, labels=True):
            if isinstance(option, Mapping) and option.get("value") not in kept:
                found.append(
                    DroppedValue(
                        question=name,
                        label=str(question.get("label", name)),
                        value=str(option.get("value")),
                        option=str(option.get("label", "")),
                    )
                )
    return tuple(found)


# --- a versão do texto do consentimento -------------------------------------------------------


def _consent_texts(consent: Any) -> Any:
    """Os textos do consentimento, sem a versão e com os padrões do formato (o que a lead lê)."""
    if not isinstance(consent, Mapping):
        return consent
    try:
        found = ConsentDoc.model_validate(consent).model_dump(
            exclude={"version", "presentation", "position"}
        )
        found.setdefault("acquisition_method", "checkbox")
        return found
    except ValidationError:
        return {key: value for key, value in consent.items() if key != "version"}


def consent_version(
    document: Mapping[str, Any], previous: Mapping[str, Any], *, number: int
) -> str | None:
    """A versão nova do texto do consentimento para a versão `number` do formulário, se o texto
    mudou desde a versão anterior (`previous`); None se não mudou. Cada envio grava a versão do
    texto que a lead leu, e quem enviou com a anterior concorda de novo. A versão nova é
    `<slug>-v<número>`, e nunca repete a da versão anterior."""
    before, after = previous.get("consent"), document.get("consent")
    if _consent_texts(before) == _consent_texts(after):
        return None
    label = f"{document.get('slug')}-v{number}"
    old = before.get("version") if isinstance(before, Mapping) else None
    return label if label != old else f"v{number}-{fingerprint(dict(document))[:8]}"


# --- a diferença por seção --------------------------------------------------------------------


@dataclass(frozen=True)
class DiffLine:
    kind: str  # add | del | ctx | hunk
    text: str


@dataclass(frozen=True)
class SectionChange:
    """Uma seção da definição numa comparação: se mudou, e as linhas que mudaram."""

    key: str  # o id da seção na página
    title: str
    changed: bool
    lines: tuple[DiffLine, ...]


def _at(*path: str) -> Callable[[Mapping[str, Any]], Any]:
    def get(document: Mapping[str, Any]) -> Any:
        value: Any = document
        for key in path:
            value = value.get(key) if isinstance(value, Mapping) else None
        return value

    return get


def _page(document: Mapping[str, Any]) -> Any:
    return {
        key: document[key]
        for key in ("title", "intro", "opening_logo", "minutes")
        if key in document
    }


def _spoken(holder: Any, targets: Mapping[str, Any]) -> Any:
    """A pergunta ou a opção com a condição (`show_when`) na frase ("Aparece quando ..."), a mesma do
    resumo da pergunta no editor; sem a frase (a condição não lê), ela fica como está."""
    if not isinstance(holder, Mapping) or "show_when" not in holder:
        return holder
    said = condition_sentence(holder["show_when"], targets)
    return holder if said is None else {**holder, "show_when": said}


def _steps(document: Mapping[str, Any]) -> Any:
    """As etapas como a comparação as mostra: a condição de cada pergunta e de cada opção em
    português, e não no JSON das regras. A frase lê os rótulos das perguntas e das opções do próprio
    documento."""
    steps = document.get("steps")
    if not isinstance(steps, list):
        return steps
    targets = targets_of(steps)
    shown = []
    for step in steps:
        questions = step.get("questions") if isinstance(step, Mapping) else None
        if not isinstance(questions, list):
            shown.append(step)
            continue
        said = []
        for question in questions:
            question = _spoken(question, targets)
            options = question.get("options") if isinstance(question, Mapping) else None
            if isinstance(options, list):
                question = {**question, "options": [_spoken(o, targets) for o in options]}
            said.append(question)
        shown.append({**step, "questions": said})
    return shown


# As seções da comparação, na ordem da leitura do formulário; o resto vai em "Outras partes".
SECTIONS: tuple[tuple[str, str, Callable[[Mapping[str, Any]], Any]], ...] = (
    ("pagina", "Página pública: título, introdução, logo e tempo", _page),
    ("avisos", "Avisos das páginas de espera e de falha", _at("notices")),
    ("conclusao", "Conclusão após salvar as respostas", _at("completion")),
    ("liberacao", "Liberação do plano", _at("release")),
    ("etapas", "Etapas e perguntas", _steps),
    ("identidade", "Identidade da lead", _at("identity")),
    ("consentimento", "Consentimento", _at("consent")),
    ("pontuacao", "Pontuação", _at("scoring")),
    ("instrucoes", "Instruções", _at("instructions")),
    ("prompt", "Prompt", _at("prompt")),
    ("modelo-do-plano", "Modelo do plano", _at("plan")),
    ("fases", "Fases com datas", _at("phases")),
    ("dados", "Dados do formulário", _at("data")),
    ("pdf-html", "PDF (HTML)", _at("pdf", "html")),
    ("pdf-css", "PDF (CSS)", _at("pdf", "css")),
    ("pdf-notas", "Notas que o PDF e a prévia podem mostrar", _at("pdf", "scores")),
    ("previa", "Prévia na página de resultado", _at("preview")),
    ("respostas-de-exemplo", "Respostas de exemplo", _at("examples", "answers")),
    ("plano-de-exemplo", "Plano de exemplo", _at("examples", "plan")),
)
COVERED = frozenset(
    {
        "title",
        "intro",
        "opening_logo",
        "minutes",
        "steps",
        "identity",
        "consent",
        "scoring",
        "instructions",
    }
    | {
        "prompt",
        "plan",
        "phases",
        "data",
        "pdf",
        "preview",
        "examples",
        "notices",
        "completion",
        "release",
    }
)


def _others(document: Mapping[str, Any]) -> Any:
    return {key: value for key, value in document.items() if key not in COVERED}


def _text(value: Any) -> str:
    """Um template como está escrito; o resto em JSON, com as chaves em ordem (o JSONB não guarda
    a ordem delas, e ela não conta)."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _diff(old: str, new: str) -> tuple[DiffLine, ...]:
    kinds = {"+": "add", "-": "del", " ": "ctx", "@": "hunk"}
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2))
    # Os dois cabeçalhos (`---` e `+++`) e mais nada: uma linha do texto pode começar igual.
    return tuple(DiffLine(kinds.get(line[:1], "ctx"), line) for line in lines[2:])


def compare(old: Mapping[str, Any], new: Mapping[str, Any]) -> tuple[SectionChange, ...]:
    """As seções das duas definições, uma a uma, com as linhas que mudaram de `old` para `new`.
    Compara a forma normal das duas (`app.forms.definition.normal`): o padrão do formato escrito
    por extenso numa e ausente na outra não é mudança."""
    old, new = normalized(old), normalized(new)
    sections = [*SECTIONS, ("outras", "Outras partes", _others)]
    found = []
    for key, title, part in sections:
        before, after = _text(part(old)), _text(part(new))
        lines = _diff(before, after) if before != after else ()
        found.append(SectionChange(key, title, before != after, lines))
    return tuple(found)
