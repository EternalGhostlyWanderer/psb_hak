"""Чтение настроек из файла .env без внешних библиотек.

Зачем: ключ и адрес модели лежат в файле .env, а сервер без Docker сам этот файл не читает
(в Docker его подставляет compose). Здесь файл читается при запуске, поэтому достаточно
положить .env в backend/ или в корень проекта.
"""
from __future__ import annotations

import os
from pathlib import Path


def parse_env_line(line: str) -> tuple[str, str] | None:
    """Разбирает одну строку «ИМЯ=значение». Пустые, комментарии и мусор пропускает."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):                 # так файл часто пишут для командной строки
        line = line[len("export "):].lstrip()
    name, sep, value = line.partition("=")
    name = name.strip()
    if not sep or not name or not (name[0].isalpha() or name[0] == "_") \
            or not all(ch.isalnum() or ch == "_" for ch in name):
        return None
    value = value.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)              # значение в кавычках: берём всё до закрывающей
        value = value[1:end] if end != -1 else value[1:]
    elif " #" in value:
        value = value.split(" #", 1)[0].rstrip()   # комментарий в конце строки
    return name, value


def load_env_files(paths: list[Path]) -> list[str]:
    """Читает найденные файлы по порядку и возвращает ИМЕНА загруженных переменных.

    Побеждает самое первое значение: переменная, заданная в системе или в Docker,
    важнее файла, а файл раньше в списке важнее файла позже. Значения не возвращаем
    и не печатаем: это ключи.
    """
    loaded: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8-sig")   # utf-8-sig: Блокнот Windows ставит BOM
        except OSError:
            continue                                      # файла нет — это нормально
        for raw in text.splitlines():
            pair = parse_env_line(raw)
            if not pair:
                continue
            name, value = pair
            if name not in os.environ and value != "":    # пустое значение не затирает «ничего»
                os.environ[name] = value
                loaded.append(name)
    return loaded
