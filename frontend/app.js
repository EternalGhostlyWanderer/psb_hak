// Логика сайта без библиотек: сборка (npm, React) здесь не нужна, файл открывается как есть.
// Весь текст вставляется через textContent, а не innerHTML: ответ модели и документы —
// это данные, они не должны исполняться как разметка.

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

const STATUS_LABELS = {
  full: "Ответ по базе",
  partial: "Частичный ответ",
  refusal: "В базе нет данных для ответа",
  clarify: "Нужно уточнение",
  no_llm: "Модель не подключена",
  error: "Ошибка",
};
const STAGE_LABELS = {
  search: "Ищу в документах",
  generate: "Формулирую ответ",
  verify: "Сверяю ответ с документами",
};
const EXAMPLES = [
  ["Факт из договора", "Какая арендная плата по договору с ООО «Северный Склад» и до какого числа её вносить?"],
  ["Редакция на дату события", "Договор на 12 млн рублей заключали в августе 2024 года. Нужно ли было одобрение генерального директора?"],
  ["Ошибка в вопросе", "Почему по договору с ООО «Северный Склад» арендная плата 500 000 рублей в месяц?"],
  ["Ответа в базе нет", "Какой размер пособия по беременности и родам?"],
];

// ---------------- история диалогов (в браузере) ----------------
// Сервер между запросами ничего не помнит, как и solution.py на платформе.
// Поэтому диалоги лежат в localStorage этого браузера и переживают перезагрузку страницы.

const STORE_KEY = "la.dialogs.v1";
const MAX_DIALOGS = 30;

function loadDialogs() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || "[]");
    return Array.isArray(saved) ? saved : [];
  } catch {
    return []; // приватное окно или повреждённые данные — начинаем с пустой истории
  }
}

function saveDialogs() {
  try {
    const kept = dialogs.filter((d) => d.messages.length).slice(0, MAX_DIALOGS);
    localStorage.setItem(STORE_KEY, JSON.stringify(kept));
  } catch {
    // хранилище недоступно или переполнено: сайт работает, просто без сохранения
  }
}

let dialogs = loadDialogs();
let current = null; // открытый диалог: { id, title, updated, messages: [...] }
let busy = false;   // идёт ответ: новые вопросы и переключение диалогов заблокированы

function newDialog() {
  current = { id: String(Date.now()), title: "", updated: Date.now(), messages: [] };
}

// Серверу нужны только настоящие реплики: «модель недоступна» и ошибки в историю не идут.
function historyForServer() {
  const turns = [];
  const msgs = current.messages;
  for (let i = 0; i + 1 < msgs.length; i += 1) {
    const q = msgs[i];
    const a = msgs[i + 1];
    if (q.role === "user" && a.role === "assistant" && !["no_llm", "error"].includes(a.status)) {
      turns.push({ role: "user", content: q.text }, { role: "assistant", content: a.text });
    }
  }
  return turns.slice(-10); // последние пять пар: длинная история только зашумляет поиск
}

// ---------------- отрисовка ----------------

function renderSidebar() {
  const list = $("dialogs");
  list.replaceChildren();
  for (const d of dialogs.filter((x) => x.messages.length)) {
    const item = el("li", d.id === current.id ? "current" : "");
    const open = el("button", "open");
    open.type = "button";
    open.append(el("span", "", d.title || "Без названия"));
    open.append(el("small", "", new Date(d.updated).toLocaleString("ru-RU", {
      day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })));
    open.onclick = () => { if (!busy) openDialog(d.id); };
    const del = el("button", "del", "×");
    del.type = "button";
    del.title = "Удалить диалог";
    del.setAttribute("aria-label", `Удалить диалог «${d.title}»`);
    del.onclick = () => { if (!busy) deleteDialog(d.id); };
    item.append(open, del);
    list.append(item);
  }
  if (!list.children.length) list.append(el("li", "hint", "Пока нет сохранённых диалогов."));
}

function renderWelcome() {
  const box = el("div", "welcome");
  box.append(el("h1", "", "Спросите по документам базы"));
  box.append(el("p", "", "Ассистент отвечает только по 21 документу базы: договоры и регламенты " +
    "ООО «Меридиан» и статьи законов. Каждый вывод подтверждён ссылкой на пункт, а если сведений " +
    "в базе нет — он скажет об этом прямо."));
  const grid = el("div", "examples");
  for (const [kind, question] of EXAMPLES) {
    const btn = el("button");
    btn.type = "button";
    btn.append(el("small", "", kind), question);
    btn.onclick = () => { if (!busy) ask(question); };
    grid.append(btn);
  }
  box.append(grid);
  return box;
}

// Кусок текста ответа. Если ответ дан по шагам («1) … 2) …»), каждый шаг — с новой строки.
function appendText(node, text, steps) {
  if (!steps) { node.append(text); return; }
  text.split(/(?=(?:^|\s)\d{1,2}\)\s)/).forEach((part, i) => {
    if (i > 0) node.append(document.createElement("br"));
    node.append(part.trimStart());
  });
}

// Текст ответа; предложения о том, чего нет в базе, выделяются цветом.
function fillAnswerText(node, text, gaps, steps) {
  node.replaceChildren();
  let rest = text;
  for (const gap of gaps || []) {
    const at = rest.indexOf(gap);
    if (at === -1) continue;
    if (at > 0) appendText(node, rest.slice(0, at), steps);
    node.append(el("span", "gap", gap));
    rest = rest.slice(at + gap.length);
  }
  appendText(node, rest, steps);
}

function sourceChip(c) {
  const label = c.clause ? `${c.title}, ${c.clause}` : c.title;
  const chip = el("button", "chip" + (c.cited ? " cited" : "") + (c.status === "утратил силу" ? " old" : ""), label);
  chip.type = "button";
  chip.title = (c.status === "утратил силу" ? "Редакция утратила силу. " : "") +
    (c.suspicious ? "Во фрагменте спрятана инструкция для ИИ: она не выполняется. " : "") + (c.text || "");
  chip.onclick = () => openDocument(c.doc_id, c.chunk_id);
  return chip;
}

function seconds(n) {
  return n.toFixed(1).replace(".", ",") + " с";
}

function messageNode(m) {
  if (m.role === "user") return el("div", "msg user", m.text);

  const box = el("div", "msg assistant");
  const status = el("div", "status");
  status.append(el("span", `pill ${m.status}`, STATUS_LABELS[m.status] || m.status));
  if (m.status === "partial") status.append(el("span", "note", "Выделено то, чего в базе нет."));
  if (m.revised) status.append(el("span", "note", "Черновик исправлен после сверки с документами."));
  // Пометки защиты (core/safety.py): что ассистент заметил и не выполнил
  const flags = m.flags || [];
  if (flags.includes("attack")) status.append(el("span", "note", "Попытка изменить правила ассистента проигнорирована."));
  if (flags.includes("pressure")) status.append(el("span", "note", "Прежний ответ перепроверен по документам."));
  if (flags.includes("leak_blocked")) status.append(el("span", "note", "Ответ с пересказом инструкций заблокирован."));
  if (flags.includes("steps")) status.append(el("span", "note", "Ответ дан по шагам."));
  box.append(status);

  const text = el("div", "text");
  fillAnswerText(text, m.text, m.gaps, flags.includes("steps"));
  box.append(text);

  const cited = (m.citations || []).filter((c) => c.cited);
  const other = (m.citations || []).filter((c) => !c.cited);
  if (cited.length || other.length) {
    const sources = el("div", "sources");
    if (cited.length) {
      sources.append(el("p", "label", "Источники ответа"));
      const chips = el("div", "chips");
      cited.forEach((c) => chips.append(sourceChip(c)));
      sources.append(chips);
    }
    if (other.length) {
      // Остальное найденное — не подтверждение ответа, а то, что ещё видела модель.
      // Прячем под спойлер, но юрист может проверить и это.
      const details = el("details");
      details.append(el("summary", "", `Другие найденные фрагменты: ${other.length}`));
      const chips = el("div", "chips");
      other.forEach((c) => chips.append(sourceChip(c)));
      details.append(chips);
      sources.append(details);
    }
    box.append(sources);
  }

  const t = m.timing;
  if (t && t.total) {
    box.append(el("p", "timing", `Ответ за ${seconds(t.total)}: поиск ${seconds(t.search)}, ` +
      `модель ${seconds(t.generate)}, проверка ${seconds(t.verify)}`));
  }
  return box;
}

function renderMessages() {
  const area = $("messages");
  area.replaceChildren();
  if (!current.messages.length) {
    area.append(renderWelcome());
    return;
  }
  current.messages.forEach((m) => area.append(messageNode(m)));
  area.scrollTop = area.scrollHeight;
}

function openDialog(id) {
  current = dialogs.find((d) => d.id === id) || current;
  closeViewer();
  $("sidebar").classList.remove("open");
  renderSidebar();
  renderMessages();
}

function deleteDialog(id) {
  const d = dialogs.find((x) => x.id === id);
  if (!d || !confirm(`Удалить диалог «${d.title}»?`)) return;
  dialogs = dialogs.filter((x) => x.id !== id);
  if (current.id === id) newDialog();
  saveDialogs();
  renderSidebar();
  renderMessages();
}

// ---------------- вопрос и ответ ----------------

// Читает поток событий сервера (Server-Sent Events) и вызывает onEvent(имя, данные).
// EventSource из браузера не подходит: он умеет только GET, а вопрос уходит в теле POST.
async function readEventStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) !== -1) { // событие заканчивается пустой строкой
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let name = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) name = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) onEvent(name, JSON.parse(data));
    }
  }
}

async function ask(question) {
  busy = true;
  $("send").disabled = true;
  if (!current.messages.length) {
    current.title = question.slice(0, 70);
    if (!dialogs.includes(current)) dialogs.unshift(current);
  }
  const body = JSON.stringify({ question, history: historyForServer() });
  current.messages.push({ role: "user", text: question });
  renderMessages();

  // Временный блок: этап работы и черновик, который печатается по мере генерации.
  const area = $("messages");
  const live = el("div", "msg assistant");
  const stage = el("div", "stage");
  const stageText = el("span", "", STAGE_LABELS.search);
  const dots = el("span", "dots");
  dots.append(el("i"), el("i"), el("i"));
  stage.append(dots, stageText);
  const draft = el("div", "text draft");
  live.append(stage, draft);
  area.append(live);
  area.scrollTop = area.scrollHeight;

  let final = null;
  const onEvent = (name, data) => {
    if (name === "stage") stageText.textContent = STAGE_LABELS[data.stage] || data.stage;
    else if (name === "delta") {
      // Прокручиваем вниз, только если пользователь сам не ушёл читать выше.
      const pinned = area.scrollHeight - area.scrollTop - area.clientHeight < 80;
      draft.textContent += data.text;
      if (pinned) area.scrollTop = area.scrollHeight;
    } else if (name === "final") final = data;
    else if (name === "error") throw new Error(data.message);
  };

  const options = { method: "POST", headers: { "Content-Type": "application/json" }, body };
  try {
    const res = await fetch("/api/chat/stream", options);
    if (!res.ok || !res.body) throw new Error(`сервер ответил ${res.status}`);
    await readEventStream(res, onEvent);
    if (!final) throw new Error("поток оборвался до ответа");
  } catch (streamError) {
    // Поток не сработал (старый прокси, обрыв связи) — спрашиваем обычным запросом.
    try {
      const res = await fetch("/api/chat", options);
      if (!res.ok) throw new Error(`сервер ответил ${res.status}`);
      final = await res.json();
    } catch (err) {
      final = { answer: `Не удалось получить ответ: ${err.message}`, status: "error" };
    }
  }

  // Итог заменяет черновик: после проверки текст мог измениться.
  current.messages.push({
    role: "assistant",
    text: final.answer,
    status: final.status,
    gaps: final.gaps || [],
    timing: final.timing || null,
    revised: Boolean(final.revised),
    flags: final.flags || [],
    // В хранилище браузера кладём только начало фрагмента: документ целиком сайт запросит сам.
    citations: (final.citations || []).map((c) => ({ ...c, text: (c.text || "").slice(0, 200) })),
  });
  current.updated = Date.now();
  dialogs.sort((a, b) => b.updated - a.updated);
  saveDialogs();
  busy = false;
  $("send").disabled = false;
  renderSidebar();
  renderMessages();
  $("question").focus();
}

// ---------------- документ с выделенным фрагментом ----------------

function showViewer(kind, title) {
  $("viewer-kind").textContent = kind;
  $("viewer-title").textContent = title;
  $("viewer").hidden = false;
  $("layout").classList.add("with-viewer");
}

function closeViewer() {
  $("viewer").hidden = true;
  $("layout").classList.remove("with-viewer");
}

async function openDocument(docId, chunkId) {
  // encodeURIComponent: в названиях документов есть пробелы, кириллица и скобки.
  const res = await fetch(`/api/documents/${encodeURIComponent(docId)}`);
  if (!res.ok) return;
  const doc = await res.json();
  showViewer(doc.type === "закон" ? "Закон" : "Документ компании", doc.title);

  const body = $("viewer-body");
  body.replaceChildren();
  const meta = el("div", "doc-meta");
  if (doc.status) meta.append(el("div", doc.status === "утратил силу" ? "tag-old" : "", `Статус: ${doc.status}`));
  if (doc.header) meta.append(el("div", "", doc.header));
  body.append(meta);

  let target = null;
  let section = "";
  for (const c of doc.chunks) {
    if (c.section && c.section !== section) { // заголовок раздела печатаем один раз
      section = c.section;
      body.append(el("div", "doc-section", section));
    }
    const block = el("div", "chunk");
    const textBox = el("div", "body");
    const trapped = c.suspicious || [];
    if (!trapped.length) {
      textBox.append(c.text);
    } else {
      // Строка с командой для ИИ (ловушка в базе): показываем как есть, но зачёркнутой
      // и с предупреждением прямо перед ней. Модели этот текст не передаётся.
      block.classList.add("suspicious");
      c.text.split("\n").forEach((line, i, all) => {
        if (trapped.includes(line)) {
          textBox.append(el("div", "inject-note",
            "Ниже спрятана инструкция для ИИ-ассистента. Ассистент её не выполняет и не использует в ответах."));
          textBox.append(el("span", "injected", line));
        } else {
          textBox.append(line);
        }
        if (i < all.length - 1) textBox.append("\n");
      });
    }
    block.append(el("div", "clause", c.clause), textBox);
    if (c.chunk_id === chunkId) {
      block.classList.add("hit");
      target = block;
    }
    body.append(block);
  }
  if (target) target.scrollIntoView({ block: "center" });
  else body.scrollTop = 0;
}

async function showDocuments() {
  const docs = await (await fetch("/api/documents")).json();
  showViewer("База знаний", `Документы: ${docs.length}`);
  const body = $("viewer-body");
  body.replaceChildren();
  for (const [type, title] of [["документ компании", "Документы компании"], ["закон", "Законы"]]) {
    body.append(el("p", "label", title));
    const list = el("div", "doc-list");
    for (const d of docs.filter((x) => x.type === type)) {
      const btn = el("button", "", d.title + (d.status === "утратил силу" ? " — утратил силу" : ""));
      btn.type = "button";
      btn.onclick = () => openDocument(d.doc_id, null);
      list.append(btn);
    }
    body.append(list);
  }
}

// ---------------- кнопки и запуск ----------------

$("ask").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("question").value.trim();
  if (!q || busy) return;
  $("question").value = "";
  $("question").style.height = "auto";
  ask(q);
});

// Enter отправляет, Shift+Enter переносит строку — как в привычных мессенджерах.
$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("ask").requestSubmit();
  }
});

// Поле растёт вместе с текстом, чтобы длинный вопрос был виден целиком.
$("question").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = `${e.target.scrollHeight + 2}px`;
});

$("new-chat").onclick = () => {
  if (busy) return;
  newDialog();
  closeViewer();
  $("sidebar").classList.remove("open");
  renderSidebar();
  renderMessages();
  $("question").focus();
};
$("show-docs").onclick = showDocuments;
$("viewer-close").onclick = closeViewer;
$("toggle-sidebar").onclick = () => {
  const open = $("sidebar").classList.toggle("open");
  $("toggle-sidebar").setAttribute("aria-expanded", String(open));
};

// Сразу видно, почему нет ответов: не задан ключ модели или не работает сервер.
fetch("/api/health")
  .then((r) => r.json())
  .then((h) => {
    $("health").textContent = h.llm
      ? `Модель подключена · документов: ${h.documents} · поиск по смыслу: ${h.dense ? "да" : "нет"}`
      : "Модель не подключена (нет LLM_API_KEY): работает только поиск";
    $("health").classList.toggle("off", !h.llm);
  })
  .catch(() => {
    $("health").textContent = "Сервер недоступен";
    $("health").classList.add("off");
  });

newDialog();
renderSidebar();
renderMessages();
