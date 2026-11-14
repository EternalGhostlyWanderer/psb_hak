# Образ веб-чата. Автопроверке на платформе он не нужен — туда уходит ZIP (scripts/build_submission.py).
FROM python:3.12-slim

WORKDIR /app

# Зависимости ставим отдельным слоем: при правках кода Docker не переустанавливает их заново.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core core
COPY backend backend
COPY frontend frontend
COPY data/kb data/kb
COPY data/chunks.json data/chunks.json

EXPOSE 8000
# 0.0.0.0 — чтобы сайт был доступен снаружи контейнера, а не только внутри него.
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
