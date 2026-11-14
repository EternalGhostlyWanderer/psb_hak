"""Собрать ZIP для загрузки на платформу: dist/submission.zip.

Платформа берёт только файлы из КОРНЯ архива, папку core/ она не увидит.
Поэтому модули ядра копируются в корень как la_<имя>.py, а импорты вида
«from .kb import …» переписываются в «from la_kb import …».

Запуск:  python3 scripts/build_submission.py
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.kb import load_kb_dir, save_chunks  # noqa: E402

# Только библиотеки из списка организаторов и в их версиях: другие на платформе не встанут.
REQUIREMENTS = "httpx==0.28.1\nnumpy==2.2.6\nrank-bm25==0.2.2\nrapidfuzz==3.14.1\n"


def flatten(source: str) -> str:
    source = re.sub(r"from \. import (\w+)", r"import la_\1 as \1", source)
    return re.sub(r"from \.(\w+) import", r"from la_\1 import", source)


def main() -> None:
    # Нарезку пересобираем из исходных документов: в архив не попадёт устаревший chunks.json.
    chunks = load_kb_dir(ROOT / "data" / "kb")
    save_chunks(chunks, ROOT / "data" / "chunks.json")

    out = ROOT / "dist" / "submission.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(ROOT / "solution.py", "solution.py")
        for module in sorted((ROOT / "core").glob("*.py")):
            if module.name != "__init__.py":
                z.writestr(f"la_{module.name}", flatten(module.read_text(encoding="utf-8")))
        z.writestr("requirements.txt", REQUIREMENTS)
        z.write(ROOT / "data" / "chunks.json", "chunks.json")   # запасная нарезка базы
        names = z.namelist()

    assert all("/" not in n for n in names), "в архиве не должно быть папок"
    print(f"{out}  ({out.stat().st_size / 1024:.0f} КБ, чанков: {len(chunks)})")
    for n in names:
        print("  ", n)


if __name__ == "__main__":
    main()
