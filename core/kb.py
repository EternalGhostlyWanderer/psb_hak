"""База знаний: читаем 21 документ и режем на чанки (куски) с метаданными.

Зачем: модель отвечает только по найденным кускам. Чем точнее кусок и его
подпись (документ, пункт, статус), тем точнее ссылка в ответе. Неверная ссылка
для автосудьи = выдуманный факт = 0 баллов за вопрос.
"""
from __future__ import annotations

import json
import re
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

# Пункт/часть целиком идёт одним чанком, если короче этого порога:
# список «1) 2) 3)» не рвётся и модель видит условие полностью.
UNIT_MAX_CHARS = 2200
# Если пункт длиннее — режем на части примерно такого размера.
PIECE_MAX_CHARS = 1100
# Абзацы-гиганты (в НК есть по 7000 символов) заранее делим по предложениям.
PARA_MAX_CHARS = 1600
# Короткий документ без нумерации (записка, письмо, допсоглашение) не режем вовсе.
SMALL_DOC_CHARS = 3000

# Тема документа — подсказка ТОЛЬКО для поиска (модели не показывается).
# Зачем: три статьи НК называются одинаково «Налоговые ставки», а сам текст
# почти не называет налог. Без подсказки вопрос про НДФЛ находит налог на прибыль.
DOC_TOPICS = {
    "НК_ст164": "НДС налог на добавленную стоимость ставка НДС",
    "НК_ст145": "НДС налог на добавленную стоимость освобождение от НДС",
    "НК_ст224": "НДФЛ налог на доходы физических лиц подоходный налог нерезидент резидент",
    "НК_ст284": "налог на прибыль организаций ставка налога на прибыль",
    "НК_ст112": "налоговое правонарушение смягчающие отягчающие обстоятельства штраф",
    "НК_ст114": "налоговое правонарушение налоговая санкция штраф размер штрафа",
    "ТК_ст59": "срочный трудовой договор временная сезонная работа",
    "ТК_ст70": "испытательный срок испытание при приеме на работу",
    "ТК_ст99": "сверхурочная работа переработка сверхурочные",
    "ТК_ст152": "оплата сверхурочной работы переработка сверхурочные",
    "152ФЗ_ст9": "персональные данные согласие на обработку ПДн отзыв согласия",
    "КоАП_ст13.11": "персональные данные штраф административная ответственность утечка",
    "МРОТ_ст1": "МРОТ минимальный размер оплаты труда минимальная зарплата",
    "Письмо комплаенс": "санкции санкционная проверка иностранный контрагент комплаенс",
    "Служебная записка": "архив хранение выдача оригиналов договоров",
    "Регламент согласования": "согласование договора одобрение лимит порог сумма",
}

# Контрагент <-> номер договора. Нужно, чтобы не путать «Альфа-Трейд»
# и «Альфа-Трейдинг» и находить договор по номеру из вопроса.
KNOWN_PARTIES = {
    "Альфа-Трейдинг": "ПТ-05/2023",   # техника
    "Альфа-Трейд": "П-17/2024",       # мебель
    "Северный Склад": "А-08/2024",    # аренда склада
}

# Как называется «N.» и «N)» в разных законах: в НК это пункт/подпункт,
# в КоАП и 152-ФЗ — часть/пункт. От этого зависит корректность ссылки.
LAW_NAMES = {
    "НК":    ("НК РФ",     "п.", "пп."),
    "КоАП":  ("КоАП РФ",   "ч.", "п."),
    "152ФЗ": ("152-ФЗ",    "ч.", "п."),
    "ТК":    ("ТК РФ",     "ч.", "п."),
    "МРОТ":  ("ФЗ о МРОТ", "ч.", "п."),
}


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str            # имя файла без расширения
    doc_title: str         # название для ссылки: «НК РФ, ст. 164»
    doc_type: str          # «закон» | «документ компании»
    status: str            # «действующая» | «утратил силу» | ""
    party: str             # контрагент (для договоров)
    contract_no: str       # номер договора
    clause: str            # «п. 2.4», «ч. 4», «п. 1, пп. 2» или ""
    text: str
    doc_header: str = ""   # шапка документа: даты, редакция, стороны
    section: str = ""      # заголовок раздела договора («3. Порядок поставки»)
    lead: str = ""         # вводная фраза пункта для подпунктов («…ставка 0% при реализации:»)
    topic: str = ""        # поисковая подсказка из DOC_TOPICS

    def ref(self) -> str:
        return f"{self.doc_title}, {self.clause}" if self.clause else self.doc_title


# ---------------- метаданные из имени файла ----------------

def _fix_zip_name(name: str) -> str:
    """Если архив собран без UTF-8-флага, кириллица в именах превращается в мусор.
    Возвращаем её обратно, иначе не определим тип и статус документа."""
    if re.search(r"[а-яА-Я]", name):
        return name
    for enc in ("utf-8", "cp866"):
        try:
            fixed = name.encode("cp437").decode(enc)
            if re.search(r"[а-яА-Я]", fixed):
                return fixed
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name


def parse_filename(name: str) -> dict:
    """Организаторы зашили метаданные в имя файла:
    «законы__ТК_ст70_действующая», «…Регламент … ред 2023 (утратил силу)»."""
    stem = Path(name).stem
    category, _, rest = stem.partition("__")
    is_law = category.strip().lower().startswith("закон")

    status = ""
    if "утратил силу" in rest:
        status = "утратил силу"
    elif "действующ" in rest:
        status = "действующая"

    title = rest.replace("_действующая", "").replace("(утратил силу)", "").strip()
    law_code = ""
    m = re.match(r"(ТК|НК|КоАП|МРОТ|152ФЗ)_ст(\d+(?:\.\d+)?)", title)
    if m:
        law_code = m.group(1)
        title = f"{LAW_NAMES[law_code][0]}, ст. {m.group(2)}"

    # Номер договора приводим к виду «П-17/2024» (в имени файла он «П-17-2024»).
    contract_no, party = "", ""
    m = re.search(r"([А-ЯA-Z]{1,3})-(\d{2})[-/](\d{4})", rest)
    if m:
        contract_no = f"{m.group(1)}-{m.group(2)}/{m.group(3)}"
        # в ссылке номер должен выглядеть как в самом договоре: «№ П-17/2024»
        title = title.replace(m.group(0), f"№ {contract_no}")
    for known, number in KNOWN_PARTIES.items():
        # допсоглашение не содержит имени контрагента — находим его по номеру договора
        if known in rest or number == contract_no:
            party, contract_no = known, number
            break

    topic = next((t for key, t in DOC_TOPICS.items() if key in stem), "")
    return {"topic": topic,
            "doc_id": stem, "doc_type": "закон" if is_law else "документ компании",
            "status": status, "doc_title": title, "party": party,
            "contract_no": contract_no, "law_code": law_code}


# ---------------- разбор текста на абзацы ----------------

POINT_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,2}){0,2})\.\s+\S")   # «1. », «2.4. », «1.1. »
SUB_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2})?)\)\s")            # «1) », «1.1) »
ARTICLE_RE = re.compile(r"^Статья\s+\d+(?:\.\d+)?\.\s")
# Пометки «Гаранта» о редакции: «Пункт 1 изменен с 1 января 2026 г. - Федеральный закон…»
NOTE_RE = re.compile(
    r"^(Пункт|Подпункт|Часть|Статья|Примечани[ея])\s.{0,60}?"
    r"(изменен|дополнен|утратил|применя|вступа|распространя)", re.IGNORECASE)
SENT_SPLIT_RE = re.compile(r"(?<=[.;])\s+(?=[А-ЯЁ0-9«(])")


def _split_long(par: str) -> list[str]:
    """Гигантский абзац делим по предложениям, чтобы чанк не занял весь контекст модели."""
    if len(par) <= PARA_MAX_CHARS:
        return [par]
    parts, cur = [], ""
    for sent in SENT_SPLIT_RE.split(par):
        if cur and len(cur) + len(sent) > PIECE_MAX_CHARS:
            parts.append(cur.strip())
            cur = ""
        cur += sent + " "
    if cur.strip():
        parts.append(cur.strip())
    return parts


@dataclass
class _Unit:
    """Один пункт (часть) документа со всеми его абзацами."""
    label: str                 # «п. 2.4» / «ч. 4» / "" для преамбулы
    section: str
    paras: list                # [(текст, номер_подпункта | "")]

    def size(self) -> int:
        return sum(len(t) for t, _ in self.paras)


def _parse_units(meta: dict, text: str) -> tuple[list[_Unit], str]:
    """Режем документ на пункты. Возвращаем пункты и шапку документа."""
    is_law = meta["doc_type"] == "закон"
    point_name, _sub_name = LAW_NAMES.get(meta["law_code"], ("", "п.", "пп."))[1:]

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Ссылка на сайт модели не нужна. А «Источник» (полное название закона) и
    # «Дата выгрузки» (на какую дату редакция актуальна) оставляем в шапке.
    lines = [ln for ln in lines if not ln.startswith("URL:")]

    # Заголовки разделов («3. Порядок поставки») бывают только там, где есть пункты «3.1.».
    # В документе с одноуровневой нумерацией «3. …» — это само условие, терять его нельзя.
    two_level = any(re.match(r"^\d{1,2}\.\d{1,2}\.", ln) for ln in lines)

    units = [_Unit("", "", [])]
    header: list[str] = []
    section = ""
    pending_notes: list[str] = []   # пометки редакции ждут «свой» пункт
    in_notes = False                # блок «Примечания:» в конце статьи КоАП
    body_started = False

    for ln in lines:
        m_point, m_sub = POINT_RE.match(ln), SUB_RE.match(ln)

        if not body_started and not m_point and not ARTICLE_RE.match(ln):
            header.append(ln)       # всё до первого пункта/статьи — шапка
            if not is_law:
                units[-1].paras.append((ln, ""))
            continue
        body_started = True

        # Пометка относится к СЛЕДУЮЩЕМУ пункту, поэтому откладываем её.
        # Иначе «Часть 2 изменена с…» прилипнет к части 1 и модель ошибётся в редакции.
        if is_law and NOTE_RE.match(ln) and len(ln) < 500:
            pending_notes.append(f"[Примечание о редакции: {ln}]")
            continue
        if is_law and ln.startswith("Примечани"):
            in_notes = True         # дальше «1.» — это уже примечание, а не часть 1

        # «3. Порядок поставки» в договоре — заголовок раздела, а не условие.
        if (not is_law and two_level and m_point and "." not in m_point.group(1) and len(ln) < 80):
            section = ln
            continue

        if m_point:
            num = m_point.group(1)
            label = f"примечание {num}" if in_notes else f"{point_name} {num}"
            units.append(_Unit(label, section, []))

        sub_no = m_sub.group(1) if (m_sub and is_law) else ""
        pieces = _split_long(ln)
        first = "\n".join(pending_notes + [pieces[0]])
        pending_notes = []
        units[-1].paras.append((first, sub_no))
        units[-1].paras.extend((p, "") for p in pieces[1:])

    return [u for u in units if u.paras], " | ".join(header)[:500]


# ---------------- сборка чанков ----------------

def chunk_document(meta: dict, text: str) -> list[Chunk]:
    units, doc_header = _parse_units(meta, text)
    is_law = meta["doc_type"] == "закон"
    sub_name = LAW_NAMES.get(meta["law_code"], ("", "п.", "пп."))[2]
    chunks: list[Chunk] = []

    def emit(label: str, section: str, body: list[str], lead: str = "") -> None:
        joined = "\n".join(body).strip()
        if not joined:
            return
        if lead and lead[:60] in joined:
            lead = ""               # вводная фраза уже есть в тексте — не дублируем
        chunks.append(Chunk(
            chunk_id=f"{meta['doc_id']}::{len(chunks) + 1}",
            doc_id=meta["doc_id"], doc_title=meta["doc_title"],
            doc_type=meta["doc_type"], status=meta["status"],
            party=meta["party"], contract_no=meta["contract_no"],
            clause=label, text=joined, doc_header=doc_header,
            section=section, lead=lead, topic=meta["topic"]))

    # Маленький документ без пунктов (записка, письмо) — один чанк: контекст важнее дробления.
    if len(units) == 1 and not units[0].label and units[0].size() <= SMALL_DOC_CHARS:
        emit("", "", [t for t, _ in units[0].paras])
        return chunks

    for u in units:
        if u.size() <= UNIT_MAX_CHARS:
            emit(u.label, u.section, [t for t, _ in u.paras])
            continue

        # Длинный пункт режем. Вводную фразу («…ставка 0 процентов при реализации:»)
        # запоминаем и приклеиваем к каждому подпункту — без неё подпункт бессмыслен.
        # [-1]: пометки о редакции стоят строками выше самого текста — во вводную фразу их не берём
        unit_intro = u.paras[0][0].split("\n")[-1][:350] if u.label else ""
        list_intro = unit_intro
        body: list[str] = []
        piece_label, piece_lead = u.label, ""
        for para, sub_no in u.paras:
            # в законах с пунктами каждый подпункт — отдельный чанк с точной ссылкой
            new_sub = bool(sub_no) and bool(u.label)
            overflow = body and sum(map(len, body)) + len(para) > PIECE_MAX_CHARS
            if body and (new_sub or overflow):
                emit(piece_label, u.section, body, piece_lead)
                body = []
                # подпункту нужна фраза перед списком, продолжению пункта — начало пункта
                piece_lead = list_intro if sub_no else unit_intro
            if new_sub:
                piece_label = f"{u.label}, {sub_name} {sub_no}"
            body.append(para)
            # «1) следующих товаров:» — вводная только для своих абзацев, не для соседних подпунктов
            if para.rstrip().endswith(":") and not sub_no:
                list_intro = para.split("\n")[-1][:350]
        emit(piece_label, u.section, body, piece_lead)

    return chunks


# ---------------- загрузка / сохранение ----------------

def load_kb_dir(kb_dir: str | Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(Path(kb_dir).glob("*.txt")):
        chunks.extend(chunk_document(parse_filename(path.name),
                                     path.read_text(encoding="utf-8-sig")))
    return chunks


def load_kb_zip(zip_path: str | Path) -> list[Chunk]:
    """Платформа кладёт базу в DATA_DIR одним ZIP-архивом."""
    chunks: list[Chunk] = []
    with zipfile.ZipFile(zip_path) as z:
        for info in sorted(z.infolist(), key=lambda i: i.filename):
            name = _fix_zip_name(info.filename)
            if name.endswith("/") or "__MACOSX" in name or not name.lower().endswith((".txt", ".md")):
                continue
            text = z.read(info).decode("utf-8-sig", errors="replace")
            chunks.extend(chunk_document(parse_filename(Path(name).name), text))
    return chunks


def save_chunks(chunks: list[Chunk], path: str | Path) -> None:
    Path(path).write_text(json.dumps([asdict(c) for c in chunks], ensure_ascii=False, indent=1),
                          encoding="utf-8")


def load_chunks(path: str | Path) -> list[Chunk]:
    return [Chunk(**d) for d in json.loads(Path(path).read_text(encoding="utf-8"))]
