import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://mumble:mumble@localhost:5433/mumble")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("MUMBLE_MODEL", "gemma4:e2b-it-qat")
EMBED_MODEL = os.environ.get("MUMBLE_EMBED_MODEL", "embeddinggemma")
EMBED_DIM = int(os.environ.get("MUMBLE_EMBED_DIM", "768"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
TZ = os.environ.get("MUMBLE_TZ", "America/Toronto")
