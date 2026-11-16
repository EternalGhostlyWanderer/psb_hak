"""Главная функция ассистента: вопрос -> ответ + источники.

Одна и та же логика вызывается из solution.py (автопроверка на платформе)
и из сервера веб-чата — это требование организаторов к финальному прототипу.
"""
from __future__ import annotations

import datetime
import re
import time
from dataclasses import dataclass, field
from typing import Iterator

from . import config
from .dialogue import asks_for_steps, history_as_text, parse_question
from .kb import KNOWN_PARTIES, Chunk
from .llm import LLMClient, LLMError
from .prompts import (ATTACK_NOTE, LEAK_ANSWER, PRESSURE_NOTE, REVISE_PROMPT, RULES_TEXT, STEPS_NOTE,
                      SELF_CHECK_PROMPT, SELF_CHECK_SYSTEM, build_answer_messages, format_context)
from .safety import clean_invisible, injected_lines, is_attack, is_pressure, leaks_instructions
from .search import HybridSearcher, ambiguous_party, detect_party, normalize, tokenize


@dataclass
class AssistantAnswer:
    answer: str
    status: str                       # full | partial | refusal | clarify
    citations: list[dict] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)     # предложения о том, чего нет в базе
    timing: dict = field(default_factory=dict)        # секунды по этапам: поиск, модель, проверка
    revised: bool = False                             # проверка изменила черновик
    flags: list[str] = field(default_factory=list)    # attack | pressure | leak_blocked | steps


# ---------------- чего не хватает в базе ----------------

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z«\"(])")
# Отказ: в базе нет вообще ничего по вопросу или вопрос не по теме.
_NO_DATA = ("в базе знаний нет", "в базе нет документов", "нет документов о", "не могу ответить",
            "по памяти отвечать не буду", "помогаю только", "не относится к юридическ")
# Пробел: ответ есть, но каких-то сведений, документов или редакций не хватает.
_GAP = ("в базе нет", "в базе знаний нет", "базе отсутств", "нет в базе", "не хватает",
        "нет сведений", "нет данных", "отсутствуют в предоставленн", "отсутствует в предоставленн",
        "в предоставленной базе", "редакции в базе", "уточните", "нужно уточнить",
        "необходимо уточнить", "следует уточнить", "нужно уточнять", "обратитесь")


def find_gaps(answer: str) -> list[str]:
    """Предложения ответа, где сказано, чего в базе нет.

    Зачем: по условию кейса ассистент обязан прямо назвать недостающие сведения.
    Сайт выделяет эти предложения, чтобы юрист не пропустил оговорку в тексте.
    Определяем по словам, без модели: это подсказка для глаз, на оценку не влияет.
    """
    return [s for s in _SENTENCE_RE.split(answer) if any(m in s.lower() for m in _GAP)]


# ---------------- программная сверка фактов ----------------

# Пробел между разрядами: «450 000» -> «450000», чтобы сравнивать числа целиком.
_THOUSANDS_RE = re.compile(r"(?<=\d)[   ](?=\d{3}(?!\d))")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _numbers(text: str) -> set[str]:
    found = set()
    for token in _NUMBER_RE.findall(_THOUSANDS_RE.sub("", text)):
        token = token.replace(",", ".")
        found.add(token)
        found.update(token.split("."))            # «10.01.2023» даёт ещё 10, 01, 2023
        if token.isdigit():
            n = int(token)
            found.add(str(n))                     # «05» и «5» — одно число
            # модель может написать «15 млн» вместо «15 000 000» — это не выдумка
            if n and n % 1_000_000 == 0:
                found.add(str(n // 1_000_000))
            if n and n % 1_000 == 0:
                found.add(str(n // 1_000))
    return found


def unsupported_numbers(answer: str, allowed_text: str) -> list[str]:
    """Числа из ответа, которых нет ни во фрагментах, ни в вопросе.

    Зачем: выдуманная сумма, срок или номер пункта обнуляет балл за вопрос.
    Проверка бесплатная (без модели) и ловит самый частый вид выдумки.
    """
    allowed = _numbers(allowed_text)
    bad = []
    for token in _NUMBER_RE.findall(_THOUSANDS_RE.sub("", answer)):
        token = token.replace(",", ".")
        parts = {token, *token.split(".")}
        if token.isdigit():
            parts.add(str(int(token)))
        if not parts & allowed and token not in bad:
            bad.append(token)
    return bad


# ---------------- какие фрагменты реально процитированы ----------------

def _mentioned_docs(answer_low: str, numbers: set[str], chunks: list[Chunk]) -> set[str]:
    """Документы, которые ответ называет. Нужно, чтобы «п. 3.1» из ответа связать
    с правильным договором: пункт 3.1 есть почти в каждом документе базы."""
    found = set()
    for c in chunks:
        title = normalize(c.doc_title)
        if c.doc_type == "закон":
            article = re.search(r"ст\. (\d+(?:\.\d+)?)", c.doc_title)
            hit = bool(article) and article.group(1) in numbers
        elif c.contract_no and "соглашение" not in title:
            party_hit = {"Альфа-Трейдинг": "трейдинг" in answer_low,
                         "Альфа-Трейд": bool(re.search(r"трейд(?!инг)", answer_low)),
                         "Северный Склад": "северн" in answer_low}.get(c.party, False)
            hit = normalize(c.contract_no) in answer_low or party_hit
        elif "регламент" in title:
            old = c.status == "утратил силу"
            hit = "регламент" in answer_low and (("2023" in numbers) if old else
                                                  ("2023" not in numbers or "2025" in numbers))
        else:
            hit = any(w in title and w in answer_low for w in ("записк", "письм", "соглашени"))
        if hit:
            found.add(c.doc_id)
    if found:
        return found
    # Ответ не назвал документ явно («согласует финансовый директор (п. 2.3)»). Узнаём
    # документ по двухуровневым номерам пунктов из ответа: «2.3», «3.2» есть не везде.
    # Утратившую силу редакцию берём, только если в ответе есть её год.
    scores: dict[str, int] = {}
    for c in chunks:
        no = re.search(r"\d+\.\d+(?:\.\d+)*", c.clause)
        if no and no.group(0) in numbers:
            scores[c.doc_id] = scores.get(c.doc_id, 0) + 1
    old = {c.doc_id for c in chunks if c.status == "утратил силу"}
    if "2023" not in numbers:
        scores = {d: s for d, s in scores.items() if d not in old} or scores
    if scores:
        best = max(scores.values())
        return {d for d, s in scores.items() if s == best}
    # Совсем не узнали — самый релевантный фрагмент.
    return {chunks[0].doc_id} if chunks else set()


def _is_cited(c: Chunk, numbers: set[str], docs: set[str], best_plain: dict[str, str]) -> bool:
    if c.doc_id not in docs:
        return False
    clause_no = re.search(r"\d+(?:\.\d+)*", c.clause)
    if clause_no:
        return clause_no.group(0) in numbers
    # Фрагмент без номера пункта (преамбула договора, абзацы статьи ТК): в ответе нет
    # номера, по которому его узнать, — берём самый похожий на ответ по словам.
    return best_plain.get(c.doc_id) == c.chunk_id


class LegalAssistant:
    def __init__(self, chunks: list[Chunk], llm: LLMClient | None = None):
        self.llm = llm or LLMClient()
        use_dense = config.USE_DENSE and self.llm.available
        if config.ALL_COMPANY_DOCS:
            # Документы компании идут в контекст целиком, искать нужно только по законам.
            self.company_chunks = [c for c in chunks if c.doc_type != "закон"]
            search_chunks = [c for c in chunks if c.doc_type == "закон"]
        else:
            self.company_chunks, search_chunks = [], chunks
        self.searcher = HybridSearcher(search_chunks, embed_fn=self.llm.embed if use_dense else None)
        self.dense_ready = self.searcher.build_dense_index()

    # ---------- поиск ----------

    def retrieve(self, question: str, history: list[dict]) -> list[Chunk]:
        """Найденные статьи законов + все документы компании целиком (см. config)."""
        found = self._search(question, history)
        seen = {c.chunk_id for c in found}
        return found + [c for c in self.company_chunks if c.chunk_id not in seen]

    def _search(self, question: str, history: list[dict]) -> list[Chunk]:
        # В диалоге контрагент и номер договора часто названы в прошлых репликах
        # («А за сколько дней проверить качество?») — ищем по истории + вопросу.
        context = " ".join(t["content"] for t in history[-4:])
        query = f"{context} {question}".strip()

        rerank_fn = None
        if config.USE_RERANKER and self.llm.available and self.llm.rerank_model:
            rerank_fn = self.llm.rerank

        if ambiguous_party(question) and detect_party(context) is None:
            # «Альфа» без уточнения: собираем условия обоих договоров, чтобы
            # ответить по каждому, а не гадать.
            chunks, seen = [], set()
            for party in ("Альфа-Трейд", "Альфа-Трейдинг"):
                for c in self.searcher.search(f"{query} {party} {KNOWN_PARTIES[party]}",
                                              config.TOP_K, rerank_fn):
                    if c.chunk_id not in seen:
                        seen.add(c.chunk_id)
                        chunks.append(c)
            return chunks
        return self.searcher.search(query, config.TOP_K, rerank_fn)

    # ---------- ответ ----------

    def answer(self, raw_question: str, history: list[dict] | None = None,
               fast: bool = False) -> AssistantAnswer:
        """raw_question — текст вопроса; на платформе история диалога зашита в него же.
        fast=True — без модельной самопроверки (когда поджимает лимит времени)."""
        result = None
        for event, payload in self.answer_events(raw_question, history, fast, stream=False):
            if event == "final":
                result = payload
        return result

    def answer_events(self, raw_question: str, history: list[dict] | None = None,
                      fast: bool = False, stream: bool = True) -> Iterator[tuple[str, object]]:
        """Тот же расчёт ответа, но по шагам. Отдаёт пары (событие, данные):
        ("stage", "search" | "generate" | "verify"), ("delta", кусок текста),
        ("final", AssistantAnswer).

        Зачем: сайт показывает ход работы и текст по мере генерации, а solution.py
        берёт только итог. Код один, поэтому ответы сайта и сабмита совпадают.
        """
        started = time.monotonic()
        if history is None:
            question, history = parse_question(raw_question)
        else:
            question = raw_question.strip()

        # Невидимые символы прячут текст от человека, но не от модели — убираем их сразу.
        question = clean_invisible(question)
        history = [{**t, "content": clean_invisible(t["content"])} for t in history]
        flags: list[str] = []
        if is_attack(question) or any(t["role"] == "user" and is_attack(t["content"]) for t in history):
            flags.append("attack")
        if any(t["role"] == "assistant" for t in history) and is_pressure(question):
            flags.append("pressure")

        yield "stage", "search"
        chunks = self.retrieve(question, history)
        t_search = time.monotonic()
        unclear_party = ambiguous_party(question) and \
            detect_party(history_as_text(history)) is None

        messages = build_answer_messages(question, chunks, history)
        if unclear_party:
            # Организаторы: при неразличимом контрагенте нужно уточнение, но голый
            # встречный вопрос автосудья считает отсутствием ответа. Даём и то и другое.
            messages[-1]["content"] += (
                "\n\nВ вопросе не указано, о каком контрагенте речь: ООО «Альфа-Трейд» или "
                "ООО «Альфа-Трейдинг». Кратко ответь по каждому договору отдельно со "
                "ссылками на пункты и попроси уточнить контрагента.")
        if "attack" in flags:
            messages[-1]["content"] += ATTACK_NOTE
        if "pressure" in flags:
            messages[-1]["content"] += PRESSURE_NOTE
        if config.STEPS_ANSWERS and asks_for_steps(question):
            flags.append("steps")
            messages[-1]["content"] += STEPS_NOTE
        yield "stage", "generate"
        draft = ""
        # При атаке черновик потоком не показываем: на экран не должно попасть ничего,
        # что не прошло проверку (например, пересказ инструкций).
        if stream and "attack" not in flags:
            try:
                for piece in self.llm.generate_stream(messages):
                    draft += piece
                    yield "delta", piece
            except LLMError:
                draft = ""                    # поток не поддерживается или оборвался
        if not draft.strip():
            draft = self.llm.generate(messages)
        t_generate = time.monotonic()

        # Показанный текст — черновик. Итог приходит после проверки и может отличаться.
        yield "stage", "verify"
        checked = self._verify(question, history, chunks, messages, draft, fast)
        t_verify = time.monotonic()

        answer = " ".join(checked.split())    # одна строка: так требует формат CSV-сабмита
        if leaks_instructions(answer, RULES_TEXT):
            answer = LEAK_ANSWER              # модель начала пересказывать свои правила
            flags.append("leak_blocked")
        gaps = find_gaps(answer)
        if unclear_party:
            status = "clarify"
        elif "leak_blocked" in flags:
            status = "refusal"
        else:
            status = self._status(answer, gaps)
        citations = self.citations(answer, chunks)
        if status == "refusal":
            # Отказ ни на что не опирается: не выдаём найденное «рядом» за источник ответа.
            for item in citations:
                item["cited"] = False
            gaps = []                         # весь ответ — «данных нет», выделять нечего
        timing = {"search": round(t_search - started, 2),
                  "generate": round(t_generate - t_search, 2),
                  "verify": round(t_verify - t_generate, 2),
                  "total": round(time.monotonic() - started, 2)}
        yield "final", AssistantAnswer(answer, status, citations, gaps, timing,
                                       revised=" ".join(draft.split()) != answer, flags=flags)

    def _verify(self, question: str, history: list[dict], chunks: list[Chunk],
                messages: list[dict], draft: str, fast: bool) -> str:
        """Проверяем черновик и, если есть замечания, один раз просим переписать."""
        context = format_context(chunks)
        problems = []

        # Числа из вопроса и сегодняшняя дата — не выдумка, их тоже разрешаем.
        allowed = "\n".join((context, question, history_as_text(history),
                             f"{datetime.date.today():%d.%m.%Y}"))
        bad = unsupported_numbers(draft, allowed)
        if bad:
            problems.append(
                "ПРОБЛЕМА: в ответе есть числа, которых нет во фрагментах и в вопросе: "
                + ", ".join(bad) + ". Если число получено расчётом из данных фрагментов — "
                "оставь его и покажи расчёт; иначе убери.")

        if config.USE_LLM_SELF_CHECK and not fast:
            try:
                verdict = self.llm.generate([
                    {"role": "system", "content": SELF_CHECK_SYSTEM},
                    {"role": "user", "content": SELF_CHECK_PROMPT.format(
                        context=context, question=question, draft=draft)}])
                # Переписываем только при явных замечаниях: расплывчатый вердикт
                # не повод портить уже хороший ответ.
                if "ПРОБЛЕМА" in verdict.upper():
                    problems.append(verdict.strip())
            except LLMError:
                pass                              # проверка — улучшение, а не точка отказа

        if not problems:
            return draft
        try:
            revised = self.llm.generate(messages + [
                {"role": "assistant", "content": draft},
                {"role": "user", "content": REVISE_PROMPT.format(problems="\n".join(problems))}])
        except LLMError:
            return draft
        # Если после правки непроверяемых чисел стало больше — правка хуже черновика.
        return revised if len(unsupported_numbers(revised, allowed)) <= len(bad) else draft

    @staticmethod
    def _status(answer: str, gaps: list[str]) -> str:
        """Грубая метка для сайта (цвет плашки). На оценку автосудьи не влияет."""
        low = answer.lower()
        # Отказ короткий: длинный текст с такой фразой — это ответ с оговоркой, а не отказ.
        if any(m in low for m in _NO_DATA) and len(answer) < 450:
            return "refusal"
        return "partial" if gaps else "full"

    @staticmethod
    def citations(answer: str, chunks: list[Chunk]) -> list[dict]:
        nums = _numbers(answer)
        docs = _mentioned_docs(normalize(answer), nums, chunks)
        # Для документов, где ни один пункт из ответа не опознан по номеру,
        # выбираем один фрагмент с наибольшим числом общих с ответом слов.
        answer_words = set(tokenize(answer))
        numbered = {c.doc_id for c in chunks if c.doc_id in docs and c.clause
                    and re.search(r"\d+(?:\.\d+)*", c.clause).group(0) in nums}
        best_plain: dict[str, str] = {}
        for doc_id in docs - numbered:
            plain = [c for c in chunks if c.doc_id == doc_id and not c.clause]
            if plain:
                best_plain[doc_id] = max(
                    plain, key=lambda c: len(answer_words & set(tokenize(c.text)))).chunk_id
        items = [{"chunk_id": c.chunk_id, "doc_id": c.doc_id, "title": c.doc_title,
                  "clause": c.clause, "status": c.status, "text": c.text,
                  # во фрагменте спрятана инструкция для ИИ: сайт предупредит об этом
                  "suspicious": bool(injected_lines(c.text)),
                  "cited": _is_cited(c, nums, docs, best_plain)} for c in chunks]
        items.sort(key=lambda it: not it["cited"])   # процитированные — первыми
        return items[:14]
