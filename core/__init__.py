"""Ядро ассистента. Снаружи нужны только эти имена."""
from .kb import Chunk, load_chunks, load_kb_dir, load_kb_zip, save_chunks
from .llm import LLMClient, LLMError
from .pipeline import AssistantAnswer, LegalAssistant
