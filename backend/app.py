"""Сервер веб-чата: отдаёт сайт и отвечает на его запросы.

Логика ответов — та же, что в solution.py (core.pipeline): организаторы требуют,
чтобы прототип показывал то же решение, что и автопроверка.

Запуск:  uvicorn backend.app:app --port 8000     -> http://localhost:8000
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import statistics
import sys
import threading
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.envfile import load_env_files  # noqa: E402

# Настройки из .env: сначала backend/.env, потом .env в корне проекта. Читаем ДО создания
# ассистента: клиент модели берёт адрес и ключ из окружения в момент создания.
ENV_FILES = [ROOT / "backend" / ".env", ROOT / ".env"]
ENV_LOADED = load_env_files(ENV_FILES)

from core import LegalAssistant, LLMError, load_chunks, load_kb_dir  # noqa: E402
from core.safety import injected_lines  # noqa: E402

# Запросы к моделям (вид и время) пишутся в общий журнал сервера — см. core/llm.py.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
# В журнал идут только имена переменных и пути файлов, значения (ключи) не пишем никогда.
_found = [str(p.relative_to(ROOT)) for p in ENV_FILES if p.is_file()]
if _found:
    logging.getLogger("la.env").info("Файлы настроек: %s. Загружено переменных: %d (%s)",
                                     ", ".join(_found), len(ENV_LOADED), ", ".join(ENV_LOADED) or "—")
else:
    logging.getLogger("la.env").warning(
        "Файл .env не найден. Положите его в папку backend или в корень проекта: %s", ROOT)

app = FastAPI(title="Юридический ассистент")

# Базу читаем один раз при старте: она не меняется, а разбор 21 документа небесплатен.
_chunks_file = ROOT / "data" / "chunks.json"
CHUNKS = load_chunks(_chunks_file) if _chunks_file.is_file() else load_kb_dir(ROOT / "data" / "kb")
assistant = LegalAssistant(CHUNKS)

DOCS: dict[str, list] = {}
for _c in CHUNKS:
    DOCS.setdefault(_c.doc_id, []).append(_c)


# ---------------- журнал запросов ----------------

# Зачем: скорость — 15% экспертной оценки. По журналу видно, сколько длится ответ
# и на каком этапе теряется время (поиск, модель, проверка), и какие запросы упали.
LOG_FILE = Path(os.getenv("LA_LOG_DIR", "").strip() or ROOT / "logs") / "requests.jsonl"
_recent: deque = deque(maxlen=500)        # последние записи в памяти — для /api/stats
_journal_lock = threading.Lock()


def journal(question: str, status: str, timing: dict | None = None, error: str = "",
            flags: list | None = None) -> None:
    entry = {"time": datetime.datetime.now().isoformat(timespec="seconds"),
             "question": question[:300], "status": status,
             "timing": timing or {}, "error": error[:300],
             "flags": flags or []}            # attack / pressure / leak_blocked — см. core/safety.py
    with _journal_lock:
        _recent.append(entry)
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass                          # диск только для чтения — журнал не повод ронять ответ


# ---------------- запросы сайта ----------------

class Turn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=10_000)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4_000)
    history: list[Turn] = Field(default_factory=list, max_length=20)


def final_payload(result) -> dict:
    return {"answer": result.answer, "status": result.status, "citations": result.citations,
            "gaps": result.gaps, "timing": result.timing, "revised": result.revised,
            "flags": result.flags}


def no_llm_payload(question: str, history: list[dict], exc: Exception) -> dict:
    """Модель недоступна (нет ключа, сеть). Не выдумываем ответ, а честно показываем,
    что нашёл поиск: сайт остаётся полезным и без модели."""
    found = assistant.citations("", assistant.retrieve(question, history))
    for item in found:
        item["cited"] = False
    return {"answer": f"Модель недоступна: {exc}. Ниже — фрагменты базы, найденные по вопросу.",
            "status": "no_llm", "citations": found, "gaps": [], "timing": {}, "revised": False}


def sse(event: str, data: dict) -> str:
    # Формат Server-Sent Events: имя события, строка данных, пустая строка-разделитель.
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/health")
def health() -> dict:
    """Сайт показывает по этому ответу, подключена ли модель."""
    return {"llm": assistant.llm.available, "dense": assistant.dense_ready,
            "documents": len(DOCS), "chunks": len(CHUNKS)}


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict:
    """Ответ целиком, одним сообщением. Запасной путь, если поток у клиента не работает."""
    history = [t.model_dump() for t in req.history]
    try:
        result = assistant.answer(req.question, history=history)
        journal(req.question, result.status, result.timing, flags=result.flags)
        return final_payload(result)
    except LLMError as exc:
        journal(req.question, "no_llm", error=str(exc))
        return no_llm_payload(req.question, history, exc)


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Ответ по мере готовности: этап работы, куски текста, в конце — проверенный итог."""
    history = [t.model_dump() for t in req.history]

    def events():
        try:
            for event, payload in assistant.answer_events(req.question, history=history):
                if event == "stage":
                    yield sse("stage", {"stage": payload})
                elif event == "delta":
                    yield sse("delta", {"text": payload})
                else:
                    journal(req.question, payload.status, payload.timing, flags=payload.flags)
                    yield sse("final", final_payload(payload))
        except LLMError as exc:
            journal(req.question, "no_llm", error=str(exc))
            yield sse("final", no_llm_payload(req.question, history, exc))
        except Exception as exc:  # noqa: BLE001 — поток уже начат, код ошибки HTTP не отправить
            journal(req.question, "error", error=f"{type(exc).__name__}: {exc}")
            yield sse("error", {"message": "Внутренняя ошибка сервера. Подробности — в журнале."})

    # no-cache и X-Accel-Buffering: без них прокси копит куски и отдаёт ответ разом.
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/stats")
def stats() -> dict:
    """Сводка журнала: сколько запросов, как быстро отвечаем, что тормозит."""
    with _journal_lock:
        entries = list(_recent)
    totals = [e["timing"]["total"] for e in entries if e["timing"].get("total")]
    return {"requests": len(entries),
            "errors": sum(e["status"] in ("error", "no_llm") for e in entries),
            "answer_seconds": {"median": round(statistics.median(totals), 2),
                               "max": round(max(totals), 2)} if totals else {},
            "model_calls": assistant.llm.stats(),
            "log_file": str(LOG_FILE)}


@app.get("/api/documents")
def documents() -> list[dict]:
    return [{"doc_id": doc_id, "title": cs[0].doc_title, "type": cs[0].doc_type,
             "status": cs[0].status} for doc_id, cs in DOCS.items()]


@app.get("/api/documents/{doc_id:path}")
def document(doc_id: str) -> dict:
    """Документ целиком, по фрагментам: сайт подсвечивает тот, на который сослался ответ."""
    chunks = DOCS.get(doc_id)
    if not chunks:
        raise HTTPException(404, "Документ не найден")
    first = chunks[0]
    return {"doc_id": doc_id, "title": first.doc_title, "type": first.doc_type,
            "status": first.status, "header": first.doc_header,
            # suspicious — строки-инструкции для ИИ: сайт показывает их с предупреждением
            "chunks": [{"chunk_id": c.chunk_id, "clause": c.clause, "section": c.section,
                        "text": c.text, "suspicious": injected_lines(c.text)} for c in chunks]}


# Сайт раздаёт этот же сервер — один контейнер, одна команда запуска.
# Подключаем последним, иначе «/» перехватит запросы к /api.
app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="site")
