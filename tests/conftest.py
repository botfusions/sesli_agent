"""Ortak test yardımcıları: sahte store, test asistanı, ayarlar. Ağ erişimi yok."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.config import AgentConfig  # noqa: E402
from server.settings import Settings  # noqa: E402


class FakeStore:
    """storage.Store sözleşmesini taklit eden bellek içi store."""

    def __init__(self, usage: dict | None = None) -> None:
        self.usage = usage or {"sessions": 0, "seconds": 0.0}
        self.sessions: dict[str, dict] = {}
        self.transcripts: list[tuple[str, str, str]] = []
        self.events: list[tuple[str, str, dict]] = []
        self.summaries: dict[str, str] = {}
        self._lock = threading.Lock()
        self._n = 0

    def create_session(self, agent_id, origin):
        with self._lock:
            self._n += 1
            sid = f"sess{self._n:04d}"
            self.sessions[sid] = {"agent_id": agent_id, "origin": origin, "ended": None}
            return sid

    def add_transcript(self, session_id, role, text):
        self.transcripts.append((session_id, role, text))

    def add_event(self, session_id, kind, payload):
        self.events.append((session_id, kind, payload))

    def end_session(self, session_id, reason, usage):
        self.sessions[session_id]["ended"] = {"reason": reason, "usage": usage}

    def set_summary(self, session_id, summary):
        self.summaries[session_id] = summary

    def usage_today(self, agent_id):
        return dict(self.usage)


def make_agent(**overrides) -> AgentConfig:
    data = {
        "id": "test-agent",
        "name": "Test Asistan",
        "instructions": "Kısa yanıt ver.",
        "greeting": "Merhaba!",
        "allowed_origins": ["https://musteri.example.com"],
    }
    data.update(overrides)
    return AgentConfig.model_validate(data)


@pytest.fixture
def fake_store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def test_settings(tmp_path) -> Settings:
    return Settings(
        gemini_api_key="test-key-not-real",
        agents_dir=tmp_path / "agents",
        database_path=tmp_path / "db.sqlite",
        public_base_url="http://testserver",
    )
