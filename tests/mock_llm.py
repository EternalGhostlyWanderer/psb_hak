"""Заглушка API моделей для локальной проверки БЕЗ ключей.

Зачем: организаторы не дают ключи для разработки, а убедиться, что solution.py
и сайт запускаются и пишут правильный формат, нужно до сабмита.
Заглушка НЕ умная: «ответ» — это цитата первого найденного пункта. Качество
ответов ею проверить нельзя — только то, что всё соединено и не падает.

Запуск:  python3 tests/mock_llm.py            (порт 8089)
Затем:   LLM_BASE_URL=http://127.0.0.1:8089 LLM_API_KEY=test python3 solution.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WORD_RE = re.compile(r"[а-яёa-z0-9]{3,}")
# Для проверки аварийных веток: MOCK_DELAY=2 — «медленная модель» (секунды на ответ),
# MOCK_FAIL_EMBED=1 — эмбеддинги отвечают ошибкой 500,
# MOCK_NO_STREAM=1 — потоковая выдача не поддерживается (ошибка 400).
DELAY = float(os.getenv("MOCK_DELAY", "0"))
FAIL_EMBED = os.getenv("MOCK_FAIL_EMBED", "") == "1"
NO_STREAM = os.getenv("MOCK_NO_STREAM", "") == "1"
WORD_DELAY = float(os.getenv("MOCK_WORD_DELAY", "0.03"))   # пауза между словами в потоке

# Готовые ответы на вопросы-примеры сайта: по ним видно оформление частичного ответа
# и отказа. Тексты взяты из эталонов организаторов (data/demo_output.csv).
CANNED = {
    "Как согласовать договор": "По действующему Регламенту согласования договоров (ред. от 01.03.2025): "
               "1) Согласование запускает руководитель подразделения, в интересах которого заключается "
               "договор (п. 1.3), в СЭД (п. 1.4). 2) Проект согласуют юридический отдел — 3 рабочих дня "
               "и главный бухгалтер — 2 рабочих дня (п. 2.1, 2.2). 3) Договор на сумму свыше 3 000 000 "
               "рублей дополнительно согласуется с финансовым директором (п. 2.3). 4) Подписанный оригинал "
               "передаётся в юридический отдел в течение 3 рабочих дней с даты подписания (п. 3.2).",
    "500 000": "По договору аренды № А-08/2024 с ООО «Северный Склад» арендная плата составляет "
               "450 000 рублей в месяц, включая НДС, а не 500 000 (п. 3.1). Арендодатель вправе "
               "не чаще раза в год увеличить плату не более чем на 7% с письменным уведомлением "
               "(п. 3.4), но документов о таком увеличении в базе нет.",
    "пособи": "В базе знаний нет документов о пособиях по беременности и родам, поэтому ответить "
              "по документам я не могу. По памяти отвечать не буду, так как нормы могли измениться. "
              "Для ответа обратитесь в отдел кадров или к юристу.",
    "12 млн": "Да. В августе 2024 года действовал Регламент согласования договоров в редакции от "
              "10.01.2023: договоры на сумму свыше 10 000 000 рублей подлежали одобрению генеральным "
              "директором до подписания (п. 2.4). В действующей редакции от 01.03.2025 порог — "
              "15 000 000 рублей, но к договору 2024 года она не применяется.",
}


def fake_embedding(text: str, dim: int = 256) -> list[float]:
    # Каждое слово попадает в свою ячейку вектора: похожие тексты дают похожие векторы.
    vec = [0.0] * dim
    for word in WORD_RE.findall(text.lower()):
        vec[int(hashlib.md5(word[:6].encode()).hexdigest(), 16) % dim] += 1.0
    return vec


def fake_answer(messages: list[dict]) -> str:
    user = messages[-1]["content"]
    if "Проверь черновик" in user:
        return "OK"                                   # самопроверка всегда «без замечаний»
    question = re.search(r"<вопрос>\n(.*?)\n</вопрос>", user, flags=re.DOTALL)
    for key, text in CANNED.items():
        if question and key in question.group(1):
            return "[заглушка] " + text
    doc = re.search(r"=== Документ: (.+?) ===", user)
    clause = re.search(r"^\[(.+?)\] (.+)$", user, flags=re.MULTILINE)
    if not doc:
        return "В базе знаний нет документов по этому вопросу."
    if clause:
        return f"[заглушка] {clause.group(2)[:300]} (Источник: {doc.group(1)}, {clause.group(1)})"
    return f"[заглушка] Найден документ: {doc.group(1)}."


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 — имя задано библиотекой
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path.endswith("/embeddings") and FAIL_EMBED:
            self.send_error(500)
            return
        if self.path.endswith("/chat/completions") and body.get("stream"):
            self.stream_answer(fake_answer(body["messages"]))
            return
        if self.path.endswith("/chat/completions"):
            time.sleep(DELAY)
            out = {"choices": [{"message": {"role": "assistant",
                                            "content": fake_answer(body["messages"])}}]}
        elif self.path.endswith("/embeddings"):
            texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
            out = {"data": [{"index": i, "embedding": fake_embedding(t)}
                            for i, t in enumerate(texts)]}
        elif self.path.endswith("/rerank"):
            query = set(WORD_RE.findall(body["query"].lower()))
            scores = [len(query & set(WORD_RE.findall(d.lower()))) for d in body["documents"]]
            order = sorted(range(len(scores)), key=lambda i: -scores[i])[:body.get("top_n", 5)]
            out = {"results": [{"index": i, "relevance_score": scores[i]} for i in order]}
        else:
            self.send_error(404)
            return
        data = json.dumps(out, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def stream_answer(self, text: str) -> None:
        """Отдаём ответ по словам в формате OpenAI: строки «data: {…}» и «data: [DONE]»."""
        if NO_STREAM:
            self.send_error(400, "stream is not supported")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()                            # без Content-Length: конец потока = закрытие
        for word in re.findall(r"\S+\s*", text):
            chunk = {"choices": [{"delta": {"content": word}}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            time.sleep(WORD_DELAY)
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *args) -> None:             # не засоряем консоль
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8089
    print(f"Заглушка моделей: http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
