import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    api_key: str = os.getenv("OPENAI_API_KEY", "").strip()
    model: str = os.getenv("MAKMA_MODEL", "gpt-6-astra").strip()
    reasoning: str = os.getenv("MAKMA_REASONING", "high").strip()
    host: str = os.getenv("MAKMA_HOST", "127.0.0.1").strip()
    port: int = int(os.getenv("MAKMA_PORT", "8765"))
    speak: bool = _bool("MAKMA_SPEAK", True)


settings = Settings()
