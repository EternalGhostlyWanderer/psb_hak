"""Точка входа для автопроверки на платформе хакатона.

Платформа запускает этот файл в Docker (без интернета, лимит 20 минут), кладёт в
DATA_DIR файлы questions.csv и knowledge-base.zip и ждёт submission.csv рядом.
Чтение вопросов и запись ответов повторяют пример организаторов — формат проверен ими.
"""
from __future__ import annotations

import csv
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

START = time.monotonic()          # отсчёт 20-минутного лимита идёт с запуска скрипта
HERE = Path(__file__).resolve().parent

sys.path.insert(0, str(HERE))   # модули ищем рядом со скриптом при любом способе запуска
try:
    # В ZIP-сабмите модули ядра лежат рядом как la_*.py: вложенные папки
    # с кодом платформа не переносит (регламент, п. 4).
    from la_kb import load_chunks, load_kb_zip
    from la_llm import LLMClient
    from la_pipeline import LegalAssistant
except ImportError:
    # В репозитории ядро — обычный пакет core/.
    from core.kb import load_chunks, load_kb_zip
    from core.llm import LLMClient
    from core.pipeline import LegalAssistant

# Лимит платформы — 20 минут. Запас нужен на запись файла и медленные последние запросы.
SOFT_LIMIT = 15.0 * 60     # после этого отвечаем без модельной самопроверки
HARD_LIMIT = 18.5 * 60     # после этого записываем то, что успели
MAX_ANSWER_CHARS = 9500    # регламент: не более 10 000 символов в ответе
FALLBACK = "Не удалось сформировать ответ по базе знаний."


def env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.getenv(name, "").strip())))
    except ValueError:
        return default


DATA_DIR = Path(os.getenv("DATA_DIR", "").strip() or "data")
# Число параллельных вопросов задаёт платформа; хардкодить его организаторы не советуют.
# Первый сабмит с 4 потоками шёл 961 c из 1200: почти впритык. Организаторы: лимитов на
# параллельность нет. Поэтому не меньше 16 потоков, даже если в окружении задано меньше;
# при ошибке 429 клиент ждёт и повторяет (core/llm.py, _retry_pause).
MAX_WORKERS = max(16, env_int("MAX_WORKERS", 16, 1, 32))


def find_file(*names: str) -> Path | None:
    for root in (DATA_DIR, HERE / DATA_DIR, Path.cwd(), HERE / "data"):
        for name in names:
            if (root / name).is_file():
                return root / name
    return None


def load_questions(path: Path) -> list[tuple[str, str]]:
    # utf-8-sig: в файле организаторов в начале стоит невидимый BOM-символ.
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = [(str(r["id"]).strip(), str(r["question"]).strip()) for r in csv.DictReader(f)]
    if not rows:
        raise ValueError("questions.csv пуст")
    return rows


def load_knowledge_base():
    """База от платформы, а если с ней что-то не так — готовая нарезка из архива."""
    bundled = HERE / "chunks.json"
    if not bundled.is_file():
        bundled = HERE / "data" / "chunks.json"
    kb_zip = find_file("knowledge-base.zip", "kb.zip", "knowledge_base.zip")
    if kb_zip:
        try:
            chunks = load_kb_zip(kb_zip)
            # Нет ни одного закона = имена файлов не распознались, метаданные потеряны.
            if len(chunks) > 50 and any(c.doc_type == "закон" for c in chunks):
                print(f"База: {kb_zip.name}, чанков {len(chunks)}", flush=True)
                return chunks
            print("База из ZIP разобрана неполно — берём chunks.json", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"База из ZIP не прочиталась ({type(exc).__name__}) — берём chunks.json",
                  file=sys.stderr)
    chunks = load_chunks(bundled)
    print(f"База: {bundled.name}, чанков {len(chunks)}", flush=True)
    return chunks


def write_submission(questions: list[tuple[str, str]], answers: dict[str, str]) -> None:
    with open("submission.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL, lineterminator="\n")
        writer.writerow(["id", "answer"])
        seen = set()
        for qid, _ in questions:
            if qid in seen:            # регламент: каждый ID ровно один раз
                continue
            seen.add(qid)
            # Пустой ответ = «не хватает данных» и ошибка формата, поэтому всегда есть текст.
            text = " ".join((answers.get(qid) or FALLBACK).split())[:MAX_ANSWER_CHARS]
            writer.writerow([qid, text or FALLBACK])


def main() -> None:
    questions_path = find_file("questions.csv")
    if not questions_path:
        raise FileNotFoundError("Не найден questions.csv в DATA_DIR")
    questions = load_questions(questions_path)

    llm = LLMClient()
    llm.deadline = START + HARD_LIMIT           # после дедлайна клиент перестаёт слать запросы
    if not llm.available:
        raise RuntimeError("Нет LLM_BASE_URL / LLM_API_KEY: платформа должна их передать")
    assistant = LegalAssistant(load_knowledge_base(), llm=llm)
    print(f"Вопросов: {len(questions)}; потоков: {MAX_WORKERS}; "
          f"поиск по смыслу: {'да' if assistant.dense_ready else 'нет (только BM25)'}", flush=True)

    answers: dict[str, str] = {}
    lock = threading.Lock()

    def work(qid: str, question: str) -> None:
        elapsed = time.monotonic() - START
        if elapsed > HARD_LIMIT:
            return                              # не успели — останется безопасный FALLBACK
        text = FALLBACK
        # Две попытки на вопрос: запасной ответ = 0 баллов, поэтому при сбое API
        # (например, серия ошибок 429) пробуем ещё раз, пока есть время.
        for attempt in range(2):
            elapsed = time.monotonic() - START
            if elapsed > HARD_LIMIT - 30:
                break
            try:
                # Поджимает время — убираем второй запрос на самопроверку: полный
                # submission.csv важнее, чем идеальные первые ответы.
                result = assistant.answer(question, fast=elapsed > SOFT_LIMIT or attempt > 0)
                text = result.answer
                break
            except Exception as exc:  # noqa: BLE001 — один сбой не должен ронять весь прогон
                print(f"Вопрос {qid}, попытка {attempt + 1}: {type(exc).__name__}: "
                      f"{str(exc)[:150]}", file=sys.stderr)
                time.sleep(3)
        with lock:
            answers[qid] = text
            if len(answers) % 10 == 0 or len(answers) == len(questions):
                print(f"Готово {len(answers)}/{len(questions)} "
                      f"за {time.monotonic() - START:.0f} c", flush=True)

    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    futures = [pool.submit(work, qid, q) for qid, q in questions]
    _, not_done = wait(futures, timeout=max(1.0, HARD_LIMIT - (time.monotonic() - START)))

    with lock:
        write_submission(questions, dict(answers))
    print(f"Создан submission.csv: {len(questions)} строк, "
          f"{time.monotonic() - START:.0f} c", flush=True)
    # Сводка попадёт в логи платформы: по ней видно, на что ушло время из 20 минут.
    print(f"Запросы к моделям — {llm.stats_summary()}", flush=True)
    if not_done:
        # Зависшие запросы не ждём: файл уже записан, иначе получим тайм-аут платформы.
        print(f"Не успели: {len(not_done)} вопросов", file=sys.stderr, flush=True)
        os._exit(0)
    pool.shutdown()


if __name__ == "__main__":
    main()
