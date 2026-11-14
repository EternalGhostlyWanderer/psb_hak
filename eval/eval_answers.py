"""Полная оценка ответов на 18 демо-вопросах: наш ответ рядом с эталоном.

Требует доступ к LLM (переменные LLM_BASE_URL, LLM_API_KEY, LLM_MODEL).
Локально можно указать любой OpenAI-совместимый сервис; на платформе
переменные подставляются автоматически.

Запуск:  python3 eval/eval_answers.py [--judge]
  --judge  дополнительно просит модель сравнить наш ответ с эталоном (1-5)
"""
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.envfile import load_env_files
from core.kb import load_chunks
from core.llm import LLMClient
from core.pipeline import LegalAssistant

JUDGE_PROMPT = """Сравни ответ ассистента с эталонным ответом юриста.

Вопрос: {question}
Эталонный ответ: {golden}
Ответ ассистента: {answer}

Оцени ответ ассистента от 1 до 5:
5 — совпадает с эталоном по сути, ссылки верные, ничего не выдумано;
4 — верный вывод, мелкие отличия в деталях или ссылках;
3 — частично верно, упущено важное или лишние неподтверждённые детали;
2 — вывод неверный или ссылки неверные;
1 — выдуман факт/документ, или отказ там, где ответ есть в эталоне.

Ответь строго в формате: ОЦЕНКА: <число>. Комментарий одной фразой."""


def main():
    judge = "--judge" in sys.argv
    base = Path(__file__).resolve().parents[1]

    load_env_files([base / "backend" / ".env", base / ".env"])   # ключи из .env, если он есть
    llm = LLMClient()
    if not llm.available:
        print("Нет LLM_BASE_URL/LLM_API_KEY — оценка ответов требует доступ к модели.")
        print("Поиск можно проверить без модели: python3 eval/eval_retrieval.py")
        sys.exit(1)

    chunks = load_chunks(base / "data/chunks.json")
    assistant = LegalAssistant(chunks, llm=llm)   # настройки — в core/config.py

    with open(base / "data/demo_questions.csv", encoding="utf-8-sig") as f:
        questions = {r["id"]: r["question"] for r in csv.DictReader(f)}
    with open(base / "data/demo_output.csv", encoding="utf-8-sig") as f:
        golden = {r["id"]: r["answer"] for r in csv.DictReader(f)}

    scores = []
    t0 = time.time()
    for qid, raw in questions.items():
        try:
            result = assistant.answer(raw)
        except Exception as exc:  # noqa: BLE001 — один сбой не должен прерывать весь прогон
            print("=" * 100, f"\nВОПРОС {qid}: ОШИБКА {type(exc).__name__}: {exc}")
            continue
        print("=" * 100)
        print(f"ВОПРОС {qid}: {raw[:200]}")
        print(f"\n[{result.status}] НАШ ОТВЕТ:\n{result.answer}")
        print(f"\nЭТАЛОН:\n{golden.get(qid, '—')}")
        if judge:
            verdict = llm.generate([{"role": "user", "content": JUDGE_PROMPT.format(
                question=raw, golden=golden.get(qid, ""), answer=result.answer)}],
                max_tokens=200)
            print(f"\nСУДЬЯ: {verdict}")
            try:
                scores.append(int(verdict.split("ОЦЕНКА:")[1].strip()[0]))
            except (IndexError, ValueError):
                pass
    print("=" * 100)
    print(f"Время: {time.time() - t0:.0f} c на {len(questions)} вопросов")
    if scores:
        print(f"Средняя оценка судьи: {sum(scores) / len(scores):.2f} "
              f"({len([s for s in scores if s >= 4])}/{len(scores)} ответов с оценкой 4+)")


if __name__ == "__main__":
    main()
