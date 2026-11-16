"""Клиент к моделям: генерация, эмбеддинги, реранкер.

На платформе всё это — один OpenAI-совместимый прокси. Адрес, ключ и имена моделей
берём ТОЛЬКО из переменных окружения (требование регламента: ключей в коде нет).
Локально сюда же подставляется любой OpenAI-совместимый сервис или tests/mock_llm.py.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from typing import Iterator

import httpx

from .config import MIN_ANSWER_TOKENS

# Журнал запросов к моделям. Кто запускает (сайт или solution.py), тот и решает,
# куда его писать; в сам журнал попадают только вид запроса и время, без ключей и текстов.
log = logging.getLogger("la.llm")

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def visible_text(raw: str) -> str:
    """Текст без рассуждений модели (<think>…</think>), в том числе недописанных.

    Зачем: при потоковой выдаче тег приходит кусками. Незакрытый блок и обрывок
    тега в конце («<thi») тоже прячем, иначе рассуждения мелькнут на экране.
    """
    text = _THINK_RE.sub("", raw)
    cut = text.find("<think>")
    if cut != -1:
        return text[:cut]
    for size in range(len("<think>") - 1, 0, -1):
        if text.endswith("<think>"[:size]):
            return text[:-size]
    return text


# Адреса OpenAI-совместимых API популярных провайдеров. Нужны, когда в .env есть LLM_PROVIDER,
# но нет LLM_BASE_URL (так устроен конфиг другого проекта команды). На платформе хакатона
# LLM_BASE_URL задан всегда, поэтому для проверки это ничего не меняет.
PROVIDER_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
# Если общего LLM_API_KEY нет, ключ ищем под именем провайдера.
PROVIDER_KEYS = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "groq": ("GROQ_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
}


def _env(*names: str, default: str = "") -> str:
    # Платформа может передать переменную под разными именами — берём первое непустое.
    for name in names:
        value = os.getenv(name, "").strip()
        if value and value != name:
            return value
    return default


class LLMError(RuntimeError):
    """Модель недоступна или вернула мусор. Пайплайн ловит это и отвечает безопасно."""


class LLMClient:
    def __init__(self) -> None:
        provider = _env("LLM_PROVIDER").lower()
        self.base_url = (_env("LLM_BASE_URL", "BASE_URL", "OPENAI_BASE_URL")
                         or PROVIDER_URLS.get(provider, "")).rstrip("/")
        self.api_key = _env("LLM_API_KEY", "OPENAI_API_KEY", *PROVIDER_KEYS.get(provider, ()))
        self.embed_key = _env("EMBEDDER_API_KEY") or self.api_key
        self.rerank_key = _env("RERANKER_API_KEY") or self.api_key
        self.gen_model = _env("LLM_MODEL", "GEN_MODEL", "OPENAI_MODEL",
                              default="deepseek/deepseek-v4.1-flash")
        self.emb_model = _env("EMB_MODEL", default="baai/bge-m3")
        self.rerank_model = _env("RERANKER_MODEL", default="")
        try:
            env_tokens = int(_env("MAX_TOKENS", default="0"))
        except ValueError:
            env_tokens = 0
        self.max_tokens = max(env_tokens, MIN_ANSWER_TOKENS)
        # Момент (time.monotonic), после которого запросы не шлём: лимит прогона 20 минут.
        self.deadline: float | None = None
        # Один клиент на все потоки: соединения переиспользуются, запросы идут быстрее.
        self._http = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
        # Счётчики по видам запросов: {вид: [сколько, секунд всего, ошибок]}.
        # По ним видно, на что уходит время прогона и хватает ли 20 минут.
        self._stats: dict[str, list[float]] = {}
        self._stats_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key)

    # ---------- журнал времени ----------

    def _record(self, kind: str, started: float, ok: bool) -> None:
        seconds = time.monotonic() - started
        with self._stats_lock:                    # вопросы обрабатываются в нескольких потоках
            row = self._stats.setdefault(kind, [0, 0.0, 0])
            row[0] += 1
            row[1] += seconds
            row[2] += 0 if ok else 1
        log.info("%s %.2f c %s", kind, seconds, "ok" if ok else "ОШИБКА")

    def stats(self) -> dict[str, dict]:
        with self._stats_lock:
            return {kind: {"calls": int(n), "seconds": round(total, 1),
                           "avg": round(total / n, 2) if n else 0.0, "errors": int(err)}
                    for kind, (n, total, err) in self._stats.items()}

    def stats_summary(self) -> str:
        parts = [f"{kind}: {s['calls']} шт., в среднем {s['avg']} c, ошибок {s['errors']}"
                 for kind, s in self.stats().items()]
        return "; ".join(parts) or "запросов к моделям не было"

    # ---------- запросы ----------

    def _post(self, path: str, key: str, payload: dict, retries: int = 4) -> dict:
        if not self.available:
            raise LLMError("Модель не подключена: нет LLM_BASE_URL / LLM_API_KEY")
        kind = path.strip("/")
        started = time.monotonic()
        last: Exception | None = None
        for attempt in range(retries):
            if self.deadline is not None and time.monotonic() > self.deadline:
                self._record(kind, started, False)
                raise LLMError("Вышло время прогона")
            try:
                r = self._http.post(self.base_url + path, json=payload,
                                    headers={"Authorization": "Bearer " + key})
                # 4xx (кроме 429) — ошибка в самом запросе, повтор не поможет.
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
                r.raise_for_status()
                data = r.json()
                self._record(kind, started, True)
                return data
            except LLMError:
                self._record(kind, started, False)
                raise
            except (httpx.HTTPError, ValueError) as exc:
                # 429 / 5xx / таймаут — временные сбои: ждём и пробуем ещё (регламент, п. 9).
                last = exc
                if attempt == retries - 1:
                    break
                time.sleep(self._retry_pause(exc, attempt))
        self._record(kind, started, False)
        raise LLMError(f"API не ответил после {retries} попыток: {type(last).__name__}: {last}")

    def _retry_pause(self, exc: Exception, attempt: int) -> float:
        """Пауза перед повтором. При 429 («слишком много запросов») ждём дольше и
        со случайной добавкой: 16 потоков не должны повторять запрос все разом."""
        pause = 2.0 * (attempt + 1)
        response = getattr(exc, "response", None)
        if response is not None and response.status_code == 429:
            try:
                pause = float(response.headers.get("retry-after", ""))
            except ValueError:
                pause = 4.0 * (attempt + 1)
            pause = min(max(pause, 1.0), 20.0) + random.uniform(0, 2)
        if self.deadline is not None:
            pause = max(0.0, min(pause, self.deadline - time.monotonic()))
        return pause

    def generate_stream(self, messages: list[dict]) -> Iterator[str]:
        """Ответ по кускам, по мере генерации — чтобы сайт показывал текст сразу.

        Используется только сайтом. Без повторов: если поток оборвался, пайплайн
        переспросит обычным запросом generate().
        """
        if not self.available:
            raise LLMError("Модель не подключена: нет LLM_BASE_URL / LLM_API_KEY")
        kind = "chat/completions (поток)"
        started = time.monotonic()
        raw, shown = "", 0
        try:
            with self._http.stream(
                    "POST", self.base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.api_key},
                    json={"model": self.gen_model, "messages": messages, "temperature": 0,
                          "max_tokens": self.max_tokens, "stream": True}) as r:
                if r.status_code != 200:
                    r.read()
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
                for line in r.iter_lines():
                    # Формат потока: строки «data: {json}», в конце «data: [DONE]».
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        raw += json.loads(data)["choices"][0].get("delta", {}).get("content") or ""
                    except (ValueError, KeyError, IndexError, TypeError):
                        continue                  # служебная строка без текста
                    text = visible_text(raw)
                    if len(text) > shown:
                        yield text[shown:]
                        shown = len(text)
        except httpx.HTTPError as exc:
            self._record(kind, started, False)
            raise LLMError(f"Поток оборвался: {type(exc).__name__}") from exc
        except LLMError:
            self._record(kind, started, False)
            raise
        self._record(kind, started, True)

    def generate(self, messages: list[dict], max_tokens: int | None = None) -> str:
        tokens = max_tokens or self.max_tokens
        for _ in range(2):
            data = self._post("/chat/completions", self.api_key, {
                "model": self.gen_model, "messages": messages,
                # 0 = без «творчества»: одинаковый вопрос даёт одинаковый ответ, меньше выдумок.
                "temperature": 0, "max_tokens": tokens})
            try:
                content = data["choices"][0]["message"].get("content") or ""
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError(f"Неожиданный формат ответа модели: {exc}") from exc
            # Некоторые модели кладут рассуждения в <think>…</think> — в ответ они не нужны.
            content = visible_text(content).strip()
            if content:
                return content
            tokens *= 2     # пусто — скорее всего лимит съели рассуждения, даём больше
        raise LLMError("Модель дважды вернула пустой ответ")

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post("/embeddings", self.embed_key, {
            "model": self.emb_model, "input": texts, "encoding_format": "float"})
        out: list = [None] * len(texts)
        for pos, item in enumerate(data["data"]):
            out[int(item.get("index", pos))] = item["embedding"]   # порядок не гарантирован
        if any(v is None for v in out):
            raise LLMError("Получены не все эмбеддинги")
        return out

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[int]:
        """Возвращает номера документов от самого подходящего к менее подходящим."""
        data = self._post("/rerank", self.rerank_key, {
            "model": self.rerank_model, "query": query,
            "documents": documents, "top_n": min(top_n, len(documents))})
        items = data.get("results", data.get("data", []))
        return [int(it["index"]) for it in items if 0 <= int(it["index"]) < len(documents)]
