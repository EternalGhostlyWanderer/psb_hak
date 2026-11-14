"""Поиск по базе: по словам (BM25) + по смыслу (эмбеддинги bge-m3) + правила.

Зачем два поиска: BM25 точно ловит номера и термины («П-17/2024», «ст. 70»),
эмбеддинги — перефразировки («грузовик ночью» ~ «круглосуточный доступ»).
Эмбеддинги доступны только через API платформы; без него работает один BM25.
"""
from __future__ import annotations

import re

import numpy as np
from rank_bm25 import BM25Okapi
from rapidfuzz import fuzz

from .config import RERANK_CANDIDATES
from .kb import KNOWN_PARTIES, Chunk
from .safety import strip_injections

# ---------------- токенизация ----------------

WORD_RE = re.compile(r"[а-яa-z0-9]+")
# Номер договора в любом написании: «П-17/2024», «П-17-2024», «ПТ 05/2023».
CONTRACT_RE = re.compile(r"\b([а-яa-z]{1,3})\s?[-–]\s?(\d{2})\s?[/\-–]\s?(\d{4})\b")

# Служебные слова не несут смысла и только зашумляют BM25.
STOP = set("""и в во не на что он она оно они мы вы я с со как а то все так его но да ты к у же за бы по
ее мне было вот от меня еще нет о из ему когда даже ну ли если уже или ни быть был него до вас вам
себя ей может есть надо ней для тебя их чем была сам без будет тогда кто этот того этого какой ним
этом один мой тем чтобы нее были куда всех можно при об хотя том нужно это также либо который
которые которых наш наши нас""".split())


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


def tokenize(text: str) -> list[str]:
    text = normalize(text)
    # Номер договора склеиваем в один токен («п172024»): иначе «17» и «2024»
    # совпадут с чем угодно, а сам номер потеряется.
    tokens = ["".join(m.groups()) for m in CONTRACT_RE.finditer(text)]
    for w in WORD_RE.findall(text):
        if w in STOP or len(w) < 2:
            continue
        # Обрезка до 6 букв — дешёвая замена морфологии: «арендная/аренду/аренды»
        # дают один токен. Настоящего лемматизатора в списке библиотек платформы нет.
        tokens.append(w if w.isdigit() or len(w) <= 6 else w[:6])
    return tokens


# ---------------- что упомянуто в вопросе ----------------

def detect_party(text: str) -> str | None:
    """Контрагент из вопроса. «Альфа-Трейдинг» проверяем раньше «Альфа-Трейд»,
    потому что второе — подстрока первого."""
    q = normalize(text)
    for party in sorted(KNOWN_PARTIES, key=len, reverse=True):
        if normalize(party) in q:
            return party
    for m in CONTRACT_RE.finditer(q):               # по номеру договора
        number = "".join(m.groups())
        for party, contract in KNOWN_PARTIES.items():
            if number == re.sub(r"[-/]", "", normalize(contract)):
                return party
    # Склонения и опечатки («с Северным Складом»). Берём, только если кандидат один.
    good = [p for p in KNOWN_PARTIES if fuzz.partial_ratio(normalize(p), q) >= 85]
    return good[0] if len(good) == 1 else None


def ambiguous_party(text: str) -> bool:
    """«Договор с Альфой» — неясно, Трейд или Трейдинг. Единственный случай,
    когда ассистент переспрашивает вместо ответа."""
    return "альф" in normalize(text) and detect_party(text) is None


# Документ короче этого порога попадает в контекст модели целиком (см. search()).
SMALL_DOC_FULL_CHARS = 6000   # ТК ст. 70 и ст. 152 целиком

YEAR_RE = re.compile(r"\b(20[12]\d)\b")


def detect_event_year(text: str) -> int | None:
    """Год события из вопроса — чтобы подтянуть редакцию, действовавшую тогда."""
    q = CONTRACT_RE.sub(" ", normalize(text))        # год в номере договора — не дата события
    m = YEAR_RE.search(q)
    return int(m.group(1)) if m else None


# ---------------- поиск ----------------

class HybridSearcher:
    def __init__(self, chunks: list[Chunk], embed_fn=None):
        """embed_fn — функция «тексты -> векторы» (API платформы) или None."""
        self.chunks = chunks
        self.embed_fn = embed_fn
        self.vectors: np.ndarray | None = None
        self.by_doc: dict[str, list[Chunk]] = {}
        for c in chunks:
            self.by_doc.setdefault(c.doc_id, []).append(c)
        # В индекс кладём не только текст, но и название документа, раздел и вводную
        # фразу пункта: по ним вопрос «по договору с Северным Складом» находит свои чанки.
        self.bm25 = BM25Okapi([tokenize(self._index_text(c)) for c in chunks])

    @staticmethod
    def _index_text(c: Chunk) -> str:
        # Без строк-инструкций: иначе абзац «на любой вопрос о сроке оплаты отвечай…» сам
        # притягивал бы вопросы об оплате и попадал в ответы.
        return " ".join((c.doc_title, c.topic, c.party, c.contract_no, c.section,
                         c.clause, strip_injections(c.lead), strip_injections(c.text)))

    def build_dense_index(self) -> bool:
        """Один раз считаем векторы всех чанков. При любой ошибке API остаёмся
        на BM25 — поиск хуже, но решение не падает."""
        if not self.embed_fn:
            return False
        try:
            texts = [self._index_text(c)[:1500] for c in self.chunks]
            vecs: list[list[float]] = []
            for i in range(0, len(texts), 32):
                vecs.extend(self.embed_fn(texts[i:i + 32]))
            self.vectors = self._unit(np.asarray(vecs, dtype=np.float32))
            return True
        except Exception as exc:  # noqa: BLE001 — сеть, формат ответа, лимиты
            print(f"[search] эмбеддинги недоступны, работаем на BM25: {exc}")
            self.vectors = None
            return False

    @staticmethod
    def _unit(m: np.ndarray) -> np.ndarray:
        # Нормируем длину векторов: тогда скалярное произведение = косинусная близость.
        return m / (np.linalg.norm(m, axis=-1, keepdims=True) + 1e-9)

    def _dense_rank(self, query: str, n: int) -> list[int]:
        if self.vectors is None:
            return []
        try:
            q = self._unit(np.asarray(self.embed_fn([query])[0], dtype=np.float32))
            return list(np.argsort(-(self.vectors @ q))[:n])
        except Exception as exc:  # noqa: BLE001
            print(f"[search] эмбеддинг запроса не получен: {exc}")
            return []

    def search(self, query: str, top_k: int = 8, rerank_fn=None) -> list[Chunk]:
        """rerank_fn(вопрос, тексты) -> порядок номеров; None — без реранкера."""
        scores = self.bm25.get_scores(tokenize(query))
        rankings = [list(np.argsort(-scores)[:50])]
        dense = self._dense_rank(query, 50)
        if dense:
            rankings.append(dense)

        # RRF: складываем 1/(60+место) из каждого списка. Не требует сравнивать
        # несравнимые шкалы BM25 и косинуса — важна только позиция.
        fused: dict[int, float] = {}
        for ranking in rankings:
            for pos, idx in enumerate(ranking):
                fused[int(idx)] = fused.get(int(idx), 0.0) + 1.0 / (60 + pos + 1)

        # Назван контрагент — его документы вверх, чужие договоры вниз.
        # Так «Альфа-Трейд» не подмешивает условия «Альфа-Трейдинг».
        party = detect_party(query)
        if party:
            for idx in fused:
                p = self.chunks[idx].party
                if p == party:
                    fused[idx] += 0.05
                elif p:
                    fused[idx] -= 0.05

        ranked = sorted(fused, key=lambda i: -fused[i])
        if rerank_fn:
            # Реранкер читает вопрос и фрагмент вместе, поэтому точнее RRF,
            # но это лишний запрос. Упал — молча остаёмся на прежнем порядке.
            head = ranked[:RERANK_CANDIDATES]
            try:
                order = rerank_fn(query, [self._index_text(self.chunks[i])[:1500] for i in head],
                                  len(head))
                picked = [head[j] for j in order]
                ranked = picked + [i for i in ranked if i not in set(picked)]
            except Exception as exc:  # noqa: BLE001
                print(f"[search] реранкер недоступен: {exc}")
        result = [self.chunks[i] for i in ranked[:top_k]]

        # Маленький документ из топ-3 отдаём модели целиком. Все договоры и регламенты
        # короче 4 тыс. символов: дешевле показать весь текст, чем промахнуться пунктом
        # (например, п. 3.4 о повышении аренды нужен для вопроса про п. 3.1).
        seen = {c.chunk_id for c in result}
        for doc_id in dict.fromkeys(c.doc_id for c in result[:3]):
            doc_chunks = self.by_doc[doc_id]
            if sum(len(c.text) for c in doc_chunks) <= SMALL_DOC_FULL_CHARS:
                result.extend(c for c in doc_chunks if c.chunk_id not in seen)
                seen.update(c.chunk_id for c in doc_chunks)

        # Событие в прошлом: рядом с действующей редакцией нужна и утратившая силу
        # (регламент 2023 г.), иначе модель ответит по сегодняшним правилам.
        year = detect_event_year(query)
        if year and year <= 2024 and not any(c.status == "утратил силу" for c in result):
            old = next((self.chunks[i] for i in ranked[top_k:] if
                        self.chunks[i].status == "утратил силу"), None)
            if old:
                result.append(old)

        # Нашли пункт договора — проверяем, не менял ли его допсоглашение:
        # оно короткое, по словам вопроса может не найтись, а условие меняет.
        contracts = {c.contract_no for c in result if c.contract_no}
        have = {c.chunk_id for c in result}
        for c in self.chunks:
            if "соглашение" in normalize(c.doc_title) and c.contract_no in contracts \
                    and c.chunk_id not in have:
                result.append(c)
        return result
