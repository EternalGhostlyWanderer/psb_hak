"""Собрать чанки из базы знаний и сохранить в data/chunks.json."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.kb import load_kb_dir, save_chunks

chunks = load_kb_dir("data/kb")
save_chunks(chunks, "data/chunks.json")
docs = {}
for c in chunks:
    docs.setdefault(c.doc_id, []).append(c)
print(f"Документов: {len(docs)}, чанков: {len(chunks)}")
for doc_id, cs in sorted(docs.items()):
    clauses = sum(1 for c in cs if c.clause)
    print(f"  {len(cs):3d} чанков ({clauses:3d} с пунктами) — {cs[0].doc_title}  [{cs[0].status}]")
