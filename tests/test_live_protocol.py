"""WebSocket protokolü uçtan uca testleri — ADK Runner sahte async generator ile değiştirilir."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient
from google.adk.events import Event
from google.genai import types
from starlette.websockets import WebSocketDisconnect

from server import live
from server.app import create_app

from tests.conftest import FakeStore, make_agent

ORIGIN = "https://musteri.example.com"
AUDIO_OUT = b"\x10\x00\x20\x00" * 240  # 24 kHz PCM16 örnek parça


def _ev(**kw) -> Event:
    return Event(author="test_agent", **kw)


class FakeRunner:
    """Runner.run_live'ı taklit eder: kuyruktan okur, senaryoya göre olay üretir."""

    instances: list["FakeRunner"] = []
    tool_callback = None

    def __init__(self, *, app_name, agent, session_service, **_):
        self.app_name = app_name
        self.agent = agent
        self.session_service = session_service
        self.received: list = []
        self.run_kwargs: dict = {}
        FakeRunner.instances.append(self)

    async def run_live(self, *, user_id, session_id, live_request_queue, run_config):
        self.run_kwargs = {"user_id": user_id, "session_id": session_id, "run_config": run_config}
        # SPEC: ADK oturumu önceden oluşturulmuş olmalı
        assert await self.session_service.get_session(
            app_name=self.app_name, user_id=user_id, session_id=session_id
        )
        while True:
            req = await live_request_queue.get()
            if req.close:
                return
            self.received.append(req)
            if req.blob is not None:
                assert req.blob.mime_type == "audio/pcm;rate=16000"
                yield _ev(content=types.Content(role="model", parts=[
                    types.Part(inline_data=types.Blob(data=AUDIO_OUT, mime_type="audio/pcm;rate=24000"))
                ]))
                yield _ev(input_transcription=types.Transcription(text="Mer", finished=False), partial=True)
                yield _ev(input_transcription=types.Transcription(text="haba", finished=False), partial=True)
                yield _ev(input_transcription=types.Transcription(text="Merhaba", finished=True), partial=False)
                yield _ev(output_transcription=types.Transcription(text="Size nasıl yardımcı olabilirim?", finished=True))
                yield _ev(usage_metadata=types.GenerateContentResponseUsageMetadata(
                    prompt_token_count=100, candidates_token_count=40))
                yield _ev(turn_complete=True)
            elif req.content is not None:
                text = req.content.parts[0].text
                if text.startswith("[Sistem]"):
                    yield _ev(output_transcription=types.Transcription(text="Merhaba!", finished=True))
                    yield _ev(turn_complete=True)
                elif text == "kes":
                    yield _ev(interrupted=True)
                elif text == "arac":
                    await FakeRunner.tool_callback({"type": "tool", "name": "book_demo", "status": "ok",
                                                    "summary": "Kaydedildi"})
                elif text == "coz":
                    raise RuntimeError("model bağlantısı koptu")
                else:
                    yield _ev(output_transcription=types.Transcription(text="Tamam.", finished=True))
                    yield _ev(turn_complete=True)


async def fake_summarize(transcript, language="tr"):
    return f"özet: {len(transcript)} satır"


@pytest.fixture
def env(monkeypatch, test_settings):
    FakeRunner.instances = []

    def fake_build_tools(agent, on_event, **kwargs):
        FakeRunner.tool_callback = on_event
        return []

    monkeypatch.setattr(live, "Runner", FakeRunner)
    monkeypatch.setattr(live.summary, "summarize", fake_summarize)
    monkeypatch.setattr(live.tools, "build_adk_tools", fake_build_tools)
    store = FakeStore()
    agent = make_agent()
    local_agent = make_agent(id="yerel", allowed_origins=[], greeting=None)
    app = create_app(settings=test_settings, store=store, agents={agent.id: agent, local_agent.id: local_agent})
    with TestClient(app) as client:
        yield client, store


def _recv(ws):
    """Sonraki çerçeve: ("json", dict) veya ("bytes", bytes)."""
    msg = ws.receive()
    if msg.get("type") == "websocket.close":
        raise WebSocketDisconnect(msg.get("code", 1000))
    if msg.get("bytes") is not None:
        return "bytes", msg["bytes"]
    return "json", json.loads(msg["text"])


def _start(ws):
    ws.send_text(json.dumps({"type": "start"}))
    kind, ready = _recv(ws)
    assert kind == "json" and ready["type"] == "ready"
    return ready


def _wait(predicate, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_full_flow(env):
    client, store = env
    with client.websocket_connect("/ws/live/test-agent", headers={"origin": ORIGIN}) as ws:
        ready = _start(ws)
        sid = ready["session_id"]
        assert ready["agent"] == {
            "id": "test-agent", "name": "Test Asistan", "greeting": "Merhaba!",
            "theme": {"title": "Sesli Asistan", "primary_color": "#A855F7", "position": "bottom-right"},
            "language": "tr-TR",
        }
        # greeting → model selamlar
        assert _recv(ws) == ("json", {"type": "transcript", "role": "agent", "text": "Merhaba!", "final": True})
        assert _recv(ws) == ("json", {"type": "turn_complete"})

        # ses → ses + transkriptler
        ws.send_bytes(b"\x00\x01" * 640)
        assert _recv(ws) == ("bytes", AUDIO_OUT)
        assert _recv(ws) == ("json", {"type": "transcript", "role": "user", "text": "Mer", "final": False})
        assert _recv(ws) == ("json", {"type": "transcript", "role": "user", "text": "Merhaba", "final": False})
        assert _recv(ws) == ("json", {"type": "transcript", "role": "user", "text": "Merhaba", "final": True})
        assert _recv(ws) == ("json", {"type": "transcript", "role": "agent",
                                      "text": "Size nasıl yardımcı olabilirim?", "final": True})
        assert _recv(ws) == ("json", {"type": "turn_complete"})

        # söz kesme
        ws.send_text(json.dumps({"type": "text", "text": "kes"}))
        assert _recv(ws) == ("json", {"type": "interrupted"})

        # araç olayı istemciye iletilir
        ws.send_text(json.dumps({"type": "text", "text": "arac"}))
        assert _recv(ws) == ("json", {"type": "tool", "name": "book_demo", "status": "ok", "summary": "Kaydedildi"})

        ws.send_text(json.dumps({"type": "end"}))
        with pytest.raises(WebSocketDisconnect) as exc:
            _recv(ws)
        assert exc.value.code == 1000

    runner = FakeRunner.instances[-1]
    assert runner.run_kwargs["session_id"] == sid
    rc = runner.run_kwargs["run_config"]
    assert rc.response_modalities == [types.Modality.AUDIO]
    assert rc.speech_config.voice_config.prebuilt_voice_config.voice_name == "Kore"
    assert rc.context_window_compression is not None
    assert rc.input_audio_transcription is not None and rc.output_audio_transcription is not None

    assert _wait(lambda: store.sessions[sid]["ended"] is not None)
    ended = store.sessions[sid]["ended"]
    assert ended["reason"] == "client_end"
    assert ended["usage"]["input_tokens"] == 100 and ended["usage"]["output_tokens"] == 40
    assert ended["usage"]["audio_in_s"] == pytest.approx(0.04, abs=0.01)
    assert ended["usage"]["audio_out_s"] == pytest.approx(0.02, abs=0.01)
    roles = [(r, t) for s, r, t in store.transcripts if s == sid]
    assert ("user", "Merhaba") in roles and ("agent", "Merhaba!") in roles and ("user", "kes") in roles
    assert any(k == "tool" for s, k, _ in store.events if s == sid)
    assert _wait(lambda: sid in store.summaries)
    assert store.summaries[sid].startswith("özet:")


def test_instruction_contains_knowledge_and_language(env):
    agent = make_agent(knowledge="Fiyatlar {degisken} demo görüşmesinde konuşulur.")
    text = live.build_instruction(agent)
    assert "Kısa yanıt ver." in text and "{degisken}" in text and "Türkçe" in text
    llm = live.build_llm_agent(agent, "gemini-test", [])
    assert callable(llm.instruction)
    assert llm.instruction(None) == text


def test_wrong_origin_closes_1008(env):
    client, store = env
    with client.websocket_connect("/ws/live/test-agent", headers={"origin": "https://kotu.example.com"}) as ws:
        kind, msg = _recv(ws)
        assert msg["type"] == "error" and msg["code"] == "origin_not_allowed"
        with pytest.raises(WebSocketDisconnect) as exc:
            _recv(ws)
        assert exc.value.code == 1008
    assert store.sessions == {}


def test_missing_origin_closes_1008(env):
    client, _ = env
    with client.websocket_connect("/ws/live/test-agent") as ws:
        assert _recv(ws)[1]["code"] == "origin_not_allowed"
        with pytest.raises(WebSocketDisconnect) as exc:
            _recv(ws)
        assert exc.value.code == 1008


def test_localhost_allowed_when_list_empty(env):
    client, _ = env
    with client.websocket_connect("/ws/live/yerel", headers={"origin": "http://localhost:8090"}) as ws:
        _start(ws)
        ws.send_text(json.dumps({"type": "end"}))


def test_unknown_agent(env):
    client, _ = env
    with client.websocket_connect("/ws/live/olmayan", headers={"origin": ORIGIN}) as ws:
        assert _recv(ws)[1]["code"] == "agent_not_found"
        with pytest.raises(WebSocketDisconnect) as exc:
            _recv(ws)
        assert exc.value.code == 1008


def test_first_message_must_be_start(env):
    client, _ = env
    with client.websocket_connect("/ws/live/test-agent", headers={"origin": ORIGIN}) as ws:
        ws.send_text(json.dumps({"type": "text", "text": "selam"}))
        assert _recv(ws)[1]["code"] == "start_required"
        with pytest.raises(WebSocketDisconnect) as exc:
            _recv(ws)
        assert exc.value.code == 1008


def test_daily_limit(env):
    client, store = env
    store.usage = {"sessions": 200, "seconds": 0}
    with client.websocket_connect("/ws/live/test-agent", headers={"origin": ORIGIN}) as ws:
        ws.send_text(json.dumps({"type": "start"}))
        assert _recv(ws) == ("json", {"type": "limit", "reason": "daily_sessions"})
        with pytest.raises(WebSocketDisconnect):
            _recv(ws)
    assert store.sessions == {}


def test_session_time_limit(env, monkeypatch):
    client, store = env
    monkeypatch.setattr(live.limits, "session_budget", lambda usage, agent: (0.3, "session_time"))
    with client.websocket_connect("/ws/live/test-agent", headers={"origin": ORIGIN}) as ws:
        sid = _start(ws)["session_id"]
        seen = []
        with pytest.raises(WebSocketDisconnect):
            while True:
                seen.append(_recv(ws))
        assert ("json", {"type": "limit", "reason": "session_time"}) in seen
    assert _wait(lambda: store.sessions[sid]["ended"] is not None)
    assert store.sessions[sid]["ended"]["reason"] == "session_time"


def test_frame_limits(env):
    client, _ = env
    with client.websocket_connect("/ws/live/test-agent", headers={"origin": ORIGIN}) as ws:
        _start(ws)
        _recv(ws), _recv(ws)  # selamlama + turn_complete
        ws.send_bytes(b"\x00" * (32 * 1024 + 2))
        assert _recv(ws)[1]["code"] == "frame_too_large"
        ws.send_text(json.dumps({"type": "text", "text": "a" * 2001}))
        assert _recv(ws)[1]["code"] == "text_too_long"
        ws.send_text("{bozuk json")
        assert _recv(ws)[1]["code"] == "bad_message"
        ws.send_text(json.dumps({"type": "bilinmeyen"}))
        err = _recv(ws)[1]
        assert err["type"] == "error" and err["code"] == "bad_message" and err["message"]
        # bağlantı hâlâ açık
        ws.send_text(json.dumps({"type": "text", "text": "merhaba"}))
        assert _recv(ws)[1]["type"] == "transcript"
        ws.send_text(json.dumps({"type": "end"}))


def test_model_failure_sends_turkish_error(env):
    client, store = env
    with client.websocket_connect("/ws/live/test-agent", headers={"origin": ORIGIN}) as ws:
        sid = _start(ws)["session_id"]
        _recv(ws), _recv(ws)
        ws.send_text(json.dumps({"type": "text", "text": "coz"}))
        kind, msg = _recv(ws)
        assert msg["type"] == "error" and msg["code"] == "model_error"
        with pytest.raises(WebSocketDisconnect) as exc:
            _recv(ws)
        assert exc.value.code == 1011
    assert _wait(lambda: store.sessions[sid]["ended"] is not None)
    assert store.sessions[sid]["ended"]["reason"] == "error"


def test_not_configured(env, monkeypatch):
    client, store = env
    from dataclasses import replace

    client.app.state.settings = replace(client.app.state.settings, gemini_api_key=None)
    with client.websocket_connect("/ws/live/test-agent", headers={"origin": ORIGIN}) as ws:
        ws.send_text(json.dumps({"type": "start"}))
        assert _recv(ws)[1]["code"] == "server_not_configured"
    assert store.sessions == {}


# --- HTTP rotaları -------------------------------------------------------------


def test_http_routes(env):
    client, _ = env
    assert client.get("/health").json() == {"ok": True}

    r = client.get("/api/agents/test-agent", headers={"origin": ORIGIN})
    assert r.status_code == 200
    assert r.json()["id"] == "test-agent"
    assert r.headers["access-control-allow-origin"] == ORIGIN
    r = client.get("/api/agents/test-agent", headers={"origin": "https://kotu.example.com"})
    assert "access-control-allow-origin" not in r.headers
    assert client.get("/api/agents/olmayan").status_code == 404


def test_demo_page(env, monkeypatch, tmp_path):
    from server import app as app_module

    client, _ = env
    widget = tmp_path / "widget"
    widget.mkdir()
    monkeypatch.setattr(app_module, "WIDGET_DIR", widget)
    assert client.get("/demo/test-agent").status_code == 404  # demo.html yok
    (widget / "demo.html").write_text(
        '<script src="{{BASE_URL}}/widget/voice-agent.js" data-agent="{{AGENT_ID}}"></script>', encoding="utf-8"
    )
    r = client.get("/demo/test-agent")
    assert r.status_code == 200
    assert 'src="http://testserver/widget/voice-agent.js"' in r.text
    assert 'data-agent="test-agent"' in r.text
    assert client.get("/demo/olmayan").status_code == 404


def test_date_directive_uses_turkey_date():
    now = live.dt.datetime(2026, 9, 29, 1, 30, tzinfo=live._TR_TZ)
    text = live._date_directive(now)
    assert "2026-09-29" in text and "Salı" in text and "01:30" in text
