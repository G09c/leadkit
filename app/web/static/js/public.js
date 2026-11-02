// Formulário público: uma etapa por vez, progresso, perguntas condicionais e rascunho no aparelho
// (localStorage). Sem este arquivo o formulário é uma página única: todas as perguntas aparecem e o
// servidor valida tudo, inclusive o que as condições escondem (descarta a resposta da pergunta
// escondida e não a exige). Dados de contato nunca vão para o rascunho (o servidor marca com
// data-draft só o que pode ser salvo). Na prévia do admin (data-preview) nada é enviado nem guardado
// no aparelho. O avaliador das condições é o conditions.js (window.FormConditions), carregado antes.
(() => {
  "use strict";
  const PREFIX = "rascunho:";
  const WEEK = 7 * 24 * 60 * 60 * 1000;

  const form = document.querySelector("[data-steps]");
  const preview = Boolean(form && form.hasAttribute("data-preview"));

  let store = null;
  if (!preview) {
    try {
      store = window.localStorage;
      store.setItem(`${PREFIX}teste`, "1");
      store.removeItem(`${PREFIX}teste`);
    } catch (error) {
      store = null; // modo privado ou navegador embutido: segue sem rascunho
    }
  }

  // Página do pedido: o envio deu certo, o rascunho deste formulário sai do aparelho.
  const sent = document.body.dataset.clearDraft;
  if (sent && store) {
    store.removeItem(`partial:${sent}`);
    Object.keys(store)
      .filter((key) => key.startsWith(`${PREFIX}${sent}:`))
      .forEach((key) => store.removeItem(key));
  }

  // O fragmento não vai ao servidor nem ao log de acesso. Remova-o antes de qualquer requisição.
  const partialSlug = /^\/p\/([^/]+)$/.exec(window.location.pathname)?.[1];
  const resumeToken = new URLSearchParams(window.location.hash.slice(1)).get("resume")
    || (partialSlug && store && !form?.elements.namedItem("_partial_token")?.value ? store.getItem(`partial:${partialSlug}`) : null);
  if (resumeToken && !preview && /^\/p\/[^/]+$/.test(window.location.pathname)) {
    if (store && partialSlug) store.removeItem(`partial:${partialSlug}`);
    history.replaceState(null, "", window.location.pathname);
    const exchange = document.createElement("form");
    exchange.method = "post";
    exchange.action = window.location.pathname + "/retomar";
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = "token";
    input.value = resumeToken;
    exchange.append(input);
    document.body.append(exchange);
    exchange.submit();
    return;
  }
  if (!form) return;
  const steps = [...form.querySelectorAll("[data-step]")];
  const bar = form.querySelector("[data-progress]");
  const minutes = Number(form.dataset.minutes) || 0;
  const submit = form.querySelector("[data-submit]");
  const submitNotice = form.querySelector("[data-submit-notice]");
  const submitHome = submit.parentElement; // a última etapa; ela pode ser pulada (veja `paint`)
  const submitLabel = submit.textContent;
  const live = form.querySelector("[data-conditions-status]");
  let current = 0;

  const controls = (scope) =>
    [...scope.querySelectorAll("input:not([type=hidden]), textarea")].filter((f) => f.tabIndex !== -1);

  // --- perguntas e opções condicionais ------------------------------------------------------------
  // O servidor manda as condições num bloco JSON. Sem o bloco (formulário sem condição) ou sem o
  // avaliador, tudo fica como veio: aparece, e o servidor decide.
  let rules = null;
  try {
    const block = form.querySelector("script[data-conditions]");
    rules = block && window.FormConditions ? JSON.parse(block.textContent) : null;
  } catch (error) {
    rules = null;
  }
  const questions = new Map([...form.querySelectorAll("[data-question]")].map((el) => [el.dataset.question, el]));
  const fieldsOf = (question) => [...question.querySelectorAll("input:not([type=hidden]), textarea")];
  let previous = null; // o que o último cálculo mostrou, para avisar o que mudou

  // A obrigatória que tem condição só é exigida quando aparece: a escondida fica desligada (`disabled`),
  // e um campo desligado não entra na conferência. Sem as condições, o servidor decide.
  if (rules) {
    form.querySelectorAll("[data-required-when-shown]").forEach((question) => {
      const fields = fieldsOf(question);
      if (fields[0]?.type === "checkbox") question.setAttribute("data-required-group", "");
      else fields.forEach((field) => { field.required = true; });
    });
  }

  function answers() {
    const found = {};
    for (const [name, question] of questions) {
      const fields = fieldsOf(question);
      if (!fields.length) continue;
      if (fields[0].type === "checkbox") found[name] = fields.filter((f) => f.checked).map((f) => f.value);
      else if (fields[0].type === "radio") {
        const chosen = fields.find((f) => f.checked);
        if (chosen) found[name] = chosen.value;
      } else found[name] = fields[0].value;
    }
    return found;
  }

  function clear(field) {
    if (field.type === "checkbox" || field.type === "radio") field.checked = false;
    else field.value = "";
  }

  // Esconde (`hidden`) e desliga (`disabled`) o que as respostas não pedem: o campo desligado não é
  // enviado nem conta para a obrigatória, e perde o valor.
  function applyConditions(announce) {
    if (!rules) return;
    const active = document.activeElement;
    const seen = window.FormConditions.evaluate(rules, answers());
    const shown = new Set(seen.visible);
    for (const [name, question] of questions) {
      const on = shown.has(name);
      const offered = new Set(seen.options[name] || []);
      question.hidden = !on;
      for (const field of fieldsOf(question)) {
        const option = field.closest("[data-option]");
        const enabled = on && (!option || offered.has(option.dataset.option));
        if (option) option.hidden = !enabled;
        field.disabled = !enabled;
        if (!enabled) clear(field);
      }
    }
    if (announce && previous) say(previous, seen);
    previous = seen;
    // O foco não fica num campo que sumiu: volta ao título da etapa.
    if (active && active !== document.body && (active.disabled || active.closest("[hidden]"))) {
      focusTitle(steps[current]);
    }
  }

  const labelOf = (name) => {
    const heading = questions.get(name).querySelector("legend, label");
    return (heading.firstChild ? heading.firstChild.textContent : "").trim();
  };

  // Avisa o leitor de tela do que mudou na etapa em que a lead está: as perguntas que apareceram ou
  // saíram, e as perguntas cujas opções mudaram.
  function say(before, after) {
    const here = (name) => steps[current].contains(questions.get(name));
    const appeared = after.visible.filter((name) => !before.visible.includes(name) && here(name));
    const left = before.visible.filter((name) => !after.visible.includes(name) && here(name));
    const changed = after.visible.filter((name) => before.visible.includes(name) && here(name)
      && after.options[name] && before.options[name]
      && after.options[name].join("\n") !== before.options[name].join("\n"));
    const messages = [];
    if (appeared.length === 1) messages.push("Apareceu 1 pergunta nova nesta etapa.");
    else if (appeared.length) messages.push(`Apareceram ${appeared.length} perguntas novas nesta etapa.`);
    if (left.length === 1) messages.push("1 pergunta deixou de se aplicar e saiu desta etapa.");
    else if (left.length) messages.push(`${left.length} perguntas deixaram de se aplicar e saíram desta etapa.`);
    changed.forEach((name) => messages.push(`As opções de «${labelOf(name)}» mudaram.`));
    if (!messages.length || !live) return;
    live.textContent = "";
    // Um instante depois de esvaziar, para o leitor de tela anunciar também um aviso repetido.
    window.setTimeout(() => { live.textContent = messages.join(" "); }, 50);
  }

  // --- etapas e progresso -------------------------------------------------------------------------
  // Uma etapa em que nenhuma pergunta aparece é pulada: não conta no progresso nem tem botão.
  const skipped = (step) => [...step.querySelectorAll("[data-questions] > *")].every((el) => el.hidden);
  const openSteps = () => {
    const open = steps.map((step, i) => i).filter((i) => !skipped(steps[i]));
    return open.length ? open : steps.map((step, i) => i);
  };

  function focusTitle(step) {
    const title = step.querySelector("h2");
    title.tabIndex = -1;
    title.focus();
  }

  // O progresso e os botões da etapa atual.
  function paint() {
    const open = openSteps();
    const total = open.length;
    const position = Math.max(0, open.indexOf(current));
    const left = Math.max(1, Math.round((minutes * (total - position)) / total));
    bar.hidden = false;
    bar.querySelector("[data-progress-step]").textContent = `Etapa ${position + 1} de ${total}`;
    bar.querySelector("[data-progress-time]").textContent = minutes ? `Faltam cerca de ${left} min` : "";
    bar.querySelector("[data-progress-bar]").style.width = `${((position + 1) / total) * 100}%`;
    const step = steps[current];
    const last = current === open[total - 1];
    const next = step.querySelector("[data-next]");
    if (next) next.hidden = last;
    const back = step.querySelector("[data-back]");
    if (back) back.hidden = position === 0;
    // O botão de enviar mora na última etapa. Se ela foi pulada, vai para a última que aparece.
    const place = last ? step.querySelector(".actions") : submitHome;
    if (submitNotice && submitNotice.parentElement !== place) place.append(submitNotice);
    if (submit.parentElement !== place) place.append(submit);
  }

  function show(index, focus) {
    const open = openSteps();
    let target = Math.max(0, Math.min(index, steps.length - 1));
    // A etapa pedida pode estar toda escondida: vai para a próxima que aparece (ou a última).
    if (!open.includes(target)) target = open.find((i) => i > target) ?? open[open.length - 1];
    current = target;
    steps.forEach((step, i) => { step.hidden = i !== current; });
    paint();
    if (focus) focusTitle(steps[current]);
  }

  function refresh(announce) {
    applyConditions(announce);
    paint();
  }

  function firstInvalid(scope) {
    // "Marque pelo menos uma": o HTML não tem `required` para grupo de caixas. A conferência fica na
    // primeira caixa que está ligada (as de opção escondida não contam).
    for (const group of scope.querySelectorAll("[data-required-group]")) {
      const boxes = [...group.querySelectorAll("input[type=checkbox]")];
      boxes.forEach((box) => box.setCustomValidity(""));
      const open = boxes.filter((box) => !box.disabled);
      if (open.length) open[0].setCustomValidity(open.some((box) => box.checked) ? "" : "Marque pelo menos uma opção.");
    }
    return controls(scope).find((field) => !field.checkValidity());
  }

  let savingPartial = false;
  let pendingSave = null;
  let partialRevision = Number(form.dataset.partialRevision) || 0;
  function rememberPartial(token) {
    const slug = form.action.split("/").filter(Boolean).pop();
    try { if (store) store.setItem(`partial:${slug}`, token); } catch (error) { /* o link continua disponível na tela */ }
    const link = form.querySelector("[data-resume-link]");
    link.href = form.action.replace(/\/$/, "") + "#resume=" + encodeURIComponent(token);
    form.querySelector("[data-resume-link-wrap]").hidden = false;
  }
  const initialPartial = form.elements.namedItem("_partial_token");
  if (initialPartial?.value) rememberPartial(initialPartial.value);

  async function savePartial() {
    if (!form.dataset.partialUrl || preview) return true;
    const notice = form.querySelector("[data-partial-status]");
    const tokenField = form.elements.namedItem("_partial_token");
    const data = new FormData(form);
    const allowed = new Set(steps.slice(0, current + 1).flatMap((step) =>
      [...step.querySelectorAll("[data-question]")].map((q) => q.dataset.question)));
    const found = Object.fromEntries(Object.entries(answers()).filter(([name]) => allowed.has(name)));
    const tracking = {};
    for (const [name, value] of data) if (name.startsWith("_t_")) tracking[name.slice(3)] = value;
    const payload = {step: current, revision: partialRevision, answers: found, tracking,
      consent: {privacy: data.get("_privacy") === "1", text_version: data.get("_consent_version"),
        marketing_email: data.has("_mkt_email"), marketing_whatsapp: data.has("_mkt_whatsapp")}};
    if (tokenField.value) payload.token = tokenField.value;
    const fingerprint = JSON.stringify(payload);
    if (!pendingSave) pendingSave = {fingerprint, payload: {...payload, request_id: crypto.randomUUID()}};
    const submittedFingerprint = pendingSave.fingerprint;
    notice.textContent = "Salvando suas respostas…";
    try {
      const response = await fetch(form.dataset.partialUrl, {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify(pendingSave.payload)});
      if (!response.ok) throw new Error("save_failed");
      const saved = await response.json();
      tokenField.value = saved.token;
      partialRevision = saved.revision;
      pendingSave = null;
      rememberPartial(saved.token);
      notice.textContent = "Respostas salvas. Guarde o link para retomar em outro aparelho.";
      if (submittedFingerprint !== fingerprint) return savePartial();
      return true;
    } catch (error) {
      notice.textContent = "Não foi possível confirmar o salvamento. Suas respostas continuam nesta tela; tente continuar novamente.";
      return false;
    }
  }

  async function next() {
    if (savingPartial) return;
    if (!preview) { // a prévia anda pelas etapas sem exigir as respostas
      const invalid = firstInvalid(steps[current]);
      if (invalid) {
        invalid.reportValidity();
        return;
      }
    }
    savingPartial = true;
    const saved = await savePartial();
    savingPartial = false;
    if (!saved) return;
    const after = openSteps().find((i) => i > current);
    if (after !== undefined) show(after, true);
  }

  function back() {
    if (savingPartial) return;
    const before = openSteps().filter((i) => i < current).pop();
    if (before !== undefined) show(before, true);
  }

  form.addEventListener("click", (event) => {
    if (event.target.closest("[data-next]")) next();
    if (event.target.closest("[data-back]")) back();
  });

  // As condições reagem antes do rascunho (que escuta o mesmo evento mais abaixo): o rascunho já
  // encontra o campo escondido desligado.
  form.addEventListener("input", () => refresh(true));
  form.addEventListener("change", () => refresh(true));

  form.addEventListener("submit", (event) => {
    if (savingPartial) { event.preventDefault(); return; }
    const open = openSteps();
    if (current !== open[open.length - 1]) { // Enter numa etapa do meio vale como "Continuar"
      event.preventDefault();
      next();
      return;
    }
    const index = steps.findIndex((step) => firstInvalid(step));
    if (index >= 0) {
      event.preventDefault();
      show(index, false);
      firstInvalid(steps[index]).reportValidity();
      return;
    }
    submit.setAttribute("aria-busy", "true");
    submit.textContent = "Enviando…";
  });
  window.addEventListener("pageshow", () => {
    submit.removeAttribute("aria-busy");
    submit.textContent = submitLabel;
    refresh(false); // ao voltar para a página, o navegador pode ter restaurado os campos depois do script
  });

  form.noValidate = true;
  applyConditions(false);
  const flagged = form.querySelector("[aria-invalid='true']");
  show(flagged ? steps.indexOf(flagged.closest("[data-step]")) : Number(form.dataset.startStep) || 0, false);
  document.getElementById("resumo-erros")?.addEventListener("click", (event) => {
    const link = event.target.closest("a[href^='#']");
    const target = link && document.getElementById(link.hash.slice(1));
    if (!target) return;
    event.preventDefault();
    show(steps.indexOf(target.closest("[data-step]")), false);
    // A primeira opção pode estar escondida (desligada): o foco vai para a primeira que a lead vê.
    const question = target.closest("[data-question]");
    const visible = target.disabled && question ? fieldsOf(question).find((f) => !f.disabled) : null;
    (visible || target).focus();
  });

  const key = form.dataset.draftKey;
  if (!store || !key || form.dataset.partialUrl) return;
  const fields = [...form.querySelectorAll("[data-draft]")];
  const status = form.querySelector("[data-save-status]");
  const isBox = (field) => field.type === "radio" || field.type === "checkbox";

  function save() {
    const values = {};
    for (const field of fields) {
      if (field.disabled) continue; // pergunta ou opção que as respostas escondem não vai para o rascunho
      if (!isBox(field)) {
        if (field.value) values[field.name] = field.value;
      } else if (field.checked) {
        (values[field.name] ||= []).push(field.value);
      }
    }
    try {
      store.setItem(key, JSON.stringify({ at: Date.now(), values }));
      const time = new Date().toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
      status.textContent = `Salvo neste aparelho às ${time}`;
    } catch (error) {
      // armazenamento cheio: o formulário continua funcionando
    }
  }

  let saved = null;
  try { saved = JSON.parse(store.getItem(key)); } catch (error) { saved = null; }
  if (saved && Date.now() - saved.at < WEEK) {
    // O que o servidor devolveu preenchido prevalece sobre o rascunho.
    const answered = new Set(fields.filter((f) => (isBox(f) ? f.checked : f.value)).map((f) => f.name));
    for (const field of fields) {
      const value = saved.values[field.name];
      if (value === undefined || answered.has(field.name)) continue;
      if (isBox(field)) field.checked = value.includes(field.value);
      else field.value = value;
    }
    refresh(false); // o rascunho traz respostas que mostram perguntas e escondem outras
  } else {
    store.removeItem(key);
  }

  form.querySelector("[data-savebar]").hidden = false;
  document.querySelector("[data-draft-fact]")?.removeAttribute("hidden");
  form.addEventListener("input", (event) => { if (event.target.hasAttribute("data-draft")) save(); });
  form.querySelector("[data-clear]").addEventListener("click", () => {
    store.removeItem(key);
    fields.forEach((field) => { if (isBox(field)) field.checked = false; else field.value = ""; });
    refresh(false);
    status.textContent = "Respostas apagadas deste aparelho.";
  });
})();
