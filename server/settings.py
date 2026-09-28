"""Ortam değişkenlerinden okunan sunucu ayarları.

pydantic-settings kullanılmaz; os.environ + dataclass yeterlidir.
Gizli değerler (GEMINI_API_KEY, ADMIN_TOKEN, TELEPHONY_SECRET) repr/log çıktısında görünmez.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DEFAULT_LIVE_MODEL = "gemini-3.8-live"
DEFAULT_SUMMARY_MODEL = "gemini-3.8-flash"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "evet"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # repr=False: gizli anahtarlar yanlışlıkla loglanmasın
    gemini_api_key: str | None = field(default=None, repr=False)
    use_vertexai: bool = False
    google_cloud_project: str | None = None
    google_cloud_location: str | None = None
    live_model: str = DEFAULT_LIVE_MODEL
    summary_model: str = DEFAULT_SUMMARY_MODEL
    agents_dir: Path = Path("./agents")
    database_path: Path = Path("./data/voice-agent.db")
    admin_token: str | None = field(default=None, repr=False)
    public_base_url: str = "http://localhost:8090"
    port: int = 8090
    # Telefon kanalı (Netgsm SIP → Asterisk → AudioSocket → bu sunucu)
    telephony_enabled: bool = False
    audiosocket_host: str = "0.0.0.0"
    audiosocket_port: int = 9092
    telephony_secret: str | None = field(default=None, repr=False)

    @property
    def model_configured(self) -> bool:
        """Canlı modele bağlanmak için gerekli kimlik bilgisi var mı?"""
        if self.use_vertexai:
            return bool(self.google_cloud_project)
        return bool(self.gemini_api_key)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gemini_api_key=os.environ.get("GEMINI_API_KEY") or None,
            use_vertexai=_env_bool("GOOGLE_GENAI_USE_VERTEXAI", False),
            google_cloud_project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None,
            google_cloud_location=os.environ.get("GOOGLE_CLOUD_LOCATION") or None,
            live_model=os.environ.get("LIVE_MODEL") or DEFAULT_LIVE_MODEL,
            summary_model=os.environ.get("SUMMARY_MODEL") or DEFAULT_SUMMARY_MODEL,
            agents_dir=Path(os.environ.get("AGENTS_DIR") or "./agents"),
            database_path=Path(os.environ.get("DATABASE_PATH") or "./data/voice-agent.db"),
            admin_token=os.environ.get("ADMIN_TOKEN") or None,
            public_base_url=(os.environ.get("PUBLIC_BASE_URL") or "http://localhost:8090").rstrip("/"),
            port=_env_int("PORT", 8090),
            telephony_enabled=_env_bool("TELEPHONY_ENABLED", False),
            audiosocket_host=os.environ.get("AUDIOSOCKET_HOST") or "0.0.0.0",
            audiosocket_port=_env_int("AUDIOSOCKET_PORT", 9092),
            telephony_secret=os.environ.get("TELEPHONY_SECRET") or None,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Süreç boyunca tek Settings örneği (testlerde get_settings.cache_clear())."""
    return Settings.from_env()
