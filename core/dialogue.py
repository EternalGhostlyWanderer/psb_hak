"""Разбор вопросов-диалогов.

Платформа передаёт историю диалога ТЕКСТОМ внутри поля question:
    Пользователь: ...
    Ассистент: ...
    Пользователь: ...
Отвечать нужно на последнюю реплику пользователя с учётом истории.
"""
from __future__ import annotations

import re

TURN_RE = re.compile(r"^(Пользователь|Ассистент)\s*:\s*", re.MULTILINE)


def parse_question(raw: str) -> tuple[str, list[dict]]:
    """Возвращает (последняя_реплика_пользователя, история).

    История — список {"role": "user"|"assistant", "content": str} БЕЗ последней
    реплики. Если меток диалога нет — вопрос одиночный, история пустая.
    """
    raw = raw.strip()
    matches = list(TURN_RE.finditer(raw))
    if not matches:
        return raw, []

    turns: list[dict] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        role = "user" if m.group(1) == "Пользователь" else "assistant"
        content = raw[start:end].strip()
        if content:
            turns.append({"role": role, "content": content})

    # отвечаем на последнюю реплику пользователя
    last_user_idx = max(
        (i for i, t in enumerate(turns) if t["role"] == "user"),
        default=len(turns) - 1,
    )
    question = turns[last_user_idx]["content"]
    history = turns[:last_user_idx]
    return question, history


def history_as_text(history: list[dict]) -> str:
    if not history:
        return ""
    names = {"user": "Пользователь", "assistant": "Ассистент"}
    return "\n".join(f"{names[t['role']]}: {t['content']}" for t in history)


# Вопрос о порядке действий: «как согласовать договор», «какие шаги», «что нужно сделать до…».
# Кейс ПСБ (раздел «Дополнительно можно»): на такие вопросы предлагать последовательность шагов.
_STEPS_RE = re.compile(
    r"\bкаки[еx]\s+(шаги|действия|этапы)|\bпо\s+шагам\b|пошагов|последовательност|"
    r"\bпорядок\s+(действий|согласовани|оформлени|подписани|заключени)|"
    r"\bкак\s+(нам\s+|мне\s+)?(согласовать|оформить|заключить|подготовить|пройти)\b|"
    r"\bс\s+чего\s+начать\b|"
    r"\bчто\s+(нам\s+|мне\s+)?(нужно|надо|необходимо|требуется|следует)\s+(сделать|пройти|подготовить)",
    re.IGNORECASE)


def asks_for_steps(question: str) -> bool:
    """Спрашивают ли о порядке действий (а не об одном факте)."""
    return bool(_STEPS_RE.search(question.lower().replace("ё", "е")))
