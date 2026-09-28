"""Öğrenme döngüsü: filtre, dosyaya ekleme (tekrar/sınır) ve talimata ekleme."""

import asyncio
import json
from types import SimpleNamespace

from server import learning, live, summary
from server.config import AgentConfig


def test_clean_drops_facts_and_limits_to_two():
    items = ["Tek soru sor.", "Fiyat 15000 TL de.", "Mail a@b.co gönder", "Kısa konuş.", "Onay al."]
    assert learning._clean(items) == ["Tek soru sor.", "Kısa konuş."]
    assert learning._clean("liste değil") == []


def test_append_dedupes_and_caps(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNING_DIR", str(tmp_path))
    assert learning.append("a", "lessons", ["Kısa konuş."]) == 1
    assert learning.append("a", "lessons", ["kısa konuş."]) == 0          # tekrar
    for i in range(5):
        learning.append("a", "lessons", [f"Kural {chr(65 + i)}."], max_items=3)
    lines = learning._read(tmp_path / "a" / "lessons.md")
    assert len(lines) == 3 and lines[-1].endswith("Kural E.")


def test_block_in_instruction_only_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNING_DIR", str(tmp_path))
    learning.append("xa", "lessons", ["Bilgileri tekrar edip onay al."])
    learning.append("xa", "errors", ["Dilleri karıştırma."])
    base = dict(id="xa", name="X", instructions="Talimat.")
    on = live.build_instruction(AgentConfig(**base, learning={"enabled": True}))
    assert "Uygula:\n- Bilgileri tekrar edip onay al." in on and "Kaçın" in on
    assert "öğrenilenler" not in live.build_instruction(AgentConfig(**base))


def test_reflect_writes_model_output(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNING_DIR", str(tmp_path))
    out = json.dumps({"lessons": ["Tek soru sor."], "errors": ["Başka dilden kelime katma."]})

    async def gen(**_):
        return SimpleNamespace(text=out)

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=gen)))
    monkeypatch.setattr(summary, "_make_client", lambda: client)
    transcript = [{"role": r, "text": f"cümle {i}"} for i, r in enumerate(["agent", "user"] * 3)]
    asyncio.run(learning.reflect("r", transcript))
    assert "Tek soru sor." in (tmp_path / "r" / "lessons.md").read_text()
    assert "Başka dilden" in (tmp_path / "r" / "errors.md").read_text()
