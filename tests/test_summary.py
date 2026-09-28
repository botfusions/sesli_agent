"""summary.py testleri (ağ yok; genai istemcisi sahte)."""

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from server import summary as summary_mod
from server.summary import summarize

TRANSCRIPT = [
    {"role": "user", "text": "Merhaba, numaram 0532 123 45 67, fiyat teklifi istiyorum."},
    {"role": "agent", "text": "Tabii, ekibimiz sizi arayacak."},
]


class FakeModels:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    async def generate_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return await self.behavior()


def fake_client(behavior):
    models = FakeModels(behavior)
    return SimpleNamespace(aio=SimpleNamespace(models=models)), models


def run(coro):
    return asyncio.run(coro)


def test_success_and_prompt(monkeypatch):
    async def ok():
        return SimpleNamespace(text="- Konu: fiyat\n- Talep: teklif, e-posta a@b.com\n- Sonraki adım: arama\n")

    client, models = fake_client(ok)
    monkeypatch.setattr(summary_mod, "_make_client", lambda: client)
    out = run(summarize(TRANSCRIPT))
    assert out == "- Konu: fiyat\n- Talep: teklif, e-posta [e-posta]\n- Sonraki adım: arama"
    call = models.calls[0]
    # Modele giden transkript de maskeli
    assert "0532" not in call["contents"] and "[telefon]" in call["contents"]
    assert "Müşteri:" in call["contents"] and "Asistan:" in call["contents"]
    assert "Kişisel veri YAZMA" in call["config"].system_instruction


def test_empty_transcript_returns_empty(monkeypatch):
    called = []
    monkeypatch.setattr(summary_mod, "_make_client", lambda: called.append(1))
    assert run(summarize([])) == ""
    assert run(summarize([{"role": "user", "text": "   "}])) == ""
    assert not called  # istemci hiç oluşturulmaz


def test_exception_returns_empty(monkeypatch):
    async def boom():
        raise RuntimeError("503 UNAVAILABLE")

    client, _ = fake_client(boom)
    monkeypatch.setattr(summary_mod, "_make_client", lambda: client)
    assert run(summarize(TRANSCRIPT)) == ""


def test_client_creation_error_returns_empty(monkeypatch):
    def bad():
        raise ValueError("bad config")

    monkeypatch.setattr(summary_mod, "_make_client", bad)
    assert run(summarize(TRANSCRIPT)) == ""


def test_empty_response_returns_empty(monkeypatch):
    async def empty():
        return SimpleNamespace(text=None)

    client, _ = fake_client(empty)
    monkeypatch.setattr(summary_mod, "_make_client", lambda: client)
    assert run(summarize(TRANSCRIPT)) == ""


def test_timeout_returns_empty(monkeypatch):
    async def slow():
        await asyncio.sleep(5)
        return SimpleNamespace(text="geç")

    client, _ = fake_client(slow)
    monkeypatch.setattr(summary_mod, "_make_client", lambda: client)
    monkeypatch.setattr(summary_mod, "TIMEOUT_S", 0.05)
    assert run(summarize(TRANSCRIPT)) == ""


def test_no_api_key_returns_empty(monkeypatch):
    monkeypatch.setattr(summary_mod, "_setting", lambda name, default=None: default)
    assert summary_mod._make_client() is None
    assert run(summarize(TRANSCRIPT)) == ""


def test_real_client_constructed_with_api_key(monkeypatch):
    # Ağ çağrısı yapılmaz; yalnızca istemci kurulumu doğrulanır
    values = {"GEMINI_API_KEY": "test-key", "GOOGLE_GENAI_USE_VERTEXAI": "false"}
    monkeypatch.setattr(summary_mod, "_setting", lambda name, default=None: values.get(name, default))
    client = summary_mod._make_client()
    assert client is not None and hasattr(client, "aio")


def test_non_turkish_language(monkeypatch):
    async def ok():
        return SimpleNamespace(text="- Topic: pricing")

    client, models = fake_client(ok)
    monkeypatch.setattr(summary_mod, "_make_client", lambda: client)
    assert run(summarize(TRANSCRIPT, language="en")) == "- Topic: pricing"
    assert "'en'" in models.calls[0]["config"].system_instruction
