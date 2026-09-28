"""Telefon kanalı (Asterisk AudioSocket) testleri — ağ yok, gerçek Gemini yok.

Uygulama TestClient ile açılır; AudioSocket sunucusu 127.0.0.1'de rastgele portta dinler ve
sahte Asterisk istemcisi gerçek TCP soketiyle bağlanır. ADK Runner sahte async generator'dır.
"""

from __future__ import annotations

import asyncio
import socket
import statistics
import time
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from google.adk.events import Event
from google.genai import types

from server import crm_supabase, live, telephony_asterisk as tel
from server.app import create_app
from server.config import SupabaseCrmTool
from server.settings import Settings

from tests.conftest import FakeStore, make_agent
from tests.test_crm_supabase import KEY, FakePostgrest, client_for

SECRET = "tel-gizli-anahtar"
CALLER = "05321234567"
CALLER_NORM = "+905321234567"
FRAME_IN = b"\x10\x00\x20\x00" * 80          # 320 bayt = 20 ms, 8 kHz
TONE_24K = b"\x00\x10\x00\xf0" * 6000        # 24 000 bayt = 0.5 s, 24 kHz


def _ev(**kw) -> Event:
    return Event(author="test_agent", **kw)


class PhoneRunner:
    """Runner.run_live taklidi. 1. ses parçası → `first_audio_s` saniyelik ses; 2. → interrupted."""

    instances: list["PhoneRunner"] = []
    first_audio_s = 0.3

    def __init__(self, *, app_name, agent, session_service, **_):
        self.app_name = app_name
        self.agent = agent
        self.session_service = session_service
        self.blobs: list = []
        self.contents: list[str] = []
        PhoneRunner.instances.append(self)

    async def run_live(self, *, user_id, session_id, live_request_queue, run_config):
        while True:
            req = await live_request_queue.get()
            if req.close:
                return
            if req.content is not None:
                self.contents.append(req.content.parts[0].text)
                continue
            if req.blob is None:
                continue
            assert req.blob.mime_type == "audio/pcm;rate=16000"
            self.blobs.append(req.blob.data)
            if len(self.blobs) == 1:
                total = int(24000 * 2 * PhoneRunner.first_audio_s)
                for i in range(0, total, 4800):
                    yield _ev(content=types.Content(role="model", parts=[
                        types.Part(inline_data=types.Blob(data=b"\x00\x10" * (min(4800, total - i) // 2),
                                                          mime_type="audio/pcm;rate=24000"))
                    ]))
                yield _ev(output_transcription=types.Transcription(text="Merhaba.", finished=True))
                yield _ev(turn_complete=True)
            elif len(self.blobs) == 2:
                yield _ev(interrupted=True)


async def fake_summarize(transcript, language="tr"):
    return "özet"


def _agents():
    phone_agent = make_agent(
        id="tel-agent",
        telephony={"enabled": True, "greeting": "Telefon selamı", "max_daily_calls_per_caller": 5,
                   "instructions": "Ek telefon kuralı."},
    )
    web_only = make_agent(id="web-agent")
    return {phone_agent.id: phone_agent, web_only.id: web_only}


@pytest.fixture
def env(monkeypatch, test_settings):
    PhoneRunner.instances = []
    PhoneRunner.first_audio_s = 0.3
    monkeypatch.setattr(live, "Runner", PhoneRunner)
    monkeypatch.setattr(live.summary, "summarize", fake_summarize)
    monkeypatch.setattr(live.tools, "build_adk_tools", lambda agent, on_event, **kw: [])
    settings = replace(test_settings, telephony_enabled=True, audiosocket_host="127.0.0.1",
                       audiosocket_port=0, telephony_secret=SECRET)
    store = FakeStore()
    app = create_app(settings=settings, store=store, agents=_agents())
    with TestClient(app) as client:
        yield client, store


# --- yardımcılar ----------------------------------------------------------------


def _register(client, call_id, caller=CALLER, agent="tel-agent", token=SECRET):
    return client.post("/telephony/asterisk/call",
                       data={"uuid": call_id, "caller": caller, "agent": agent, "token": token})


def _connect(client) -> socket.socket:
    port = client.app.state.audiosocket.port
    sock = socket.create_connection(("127.0.0.1", port), timeout=3)
    sock.settimeout(3)
    return sock


def _send(sock, kind, payload=b""):
    sock.sendall(tel.encode_frame(kind, payload))


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _read_frame(sock):
    header = _recv_exact(sock, 3)
    if header is None:
        return None
    length = int.from_bytes(header[1:3], "big")
    payload = _recv_exact(sock, length) if length else b""
    return header[0], payload


def _drain_until_eof(sock):
    frames = []
    while True:
        f = _read_frame(sock)
        if f is None:
            return frames
        frames.append(f)


def _wait(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _start_call(client, caller=CALLER):
    call_id = str(uuid.uuid4())
    assert _register(client, call_id, caller=caller).text == "OK"
    sock = _connect(client)
    _send(sock, tel.KIND_UUID, uuid.UUID(call_id).bytes)
    return sock


# --- kayıt ucu ----------------------------------------------------------------


def test_register_ok_and_errors(env):
    client, _ = env
    registry = client.app.state.call_registry
    call_id = str(uuid.uuid4())
    r = _register(client, call_id)
    assert r.status_code == 200 and r.text == "OK"
    item = registry.pop(call_id)
    assert item.agent_id == "tel-agent" and item.caller == CALLER

    assert _register(client, call_id, token="yanlis").status_code == 403
    assert client.post("/telephony/asterisk/call", data={"uuid": call_id, "agent": "tel-agent"}).status_code == 403
    assert _register(client, "uuid-degil").status_code == 400
    assert _register(client, call_id, agent="olmayan").status_code == 404
    assert _register(client, call_id, agent="web-agent").status_code == 404  # telephony kapalı
    assert len(registry) == 0


def test_register_disabled_or_no_secret(monkeypatch, test_settings):
    for settings in (test_settings, replace(test_settings, telephony_enabled=True, telephony_secret=None)):
        app = create_app(settings=replace(settings, audiosocket_port=0, audiosocket_host="127.0.0.1"),
                         store=FakeStore(), agents=_agents())
        with TestClient(app) as client:
            assert _register(client, str(uuid.uuid4()), token=SECRET).status_code == 404
            if not settings.telephony_enabled:
                assert client.app.state.audiosocket is None


def test_registry_ttl_single_use_and_capacity():
    now = [100.0]
    reg = tel.CallRegistry(ttl_s=30, max_entries=3, clock=lambda: now[0])
    reg.put("a", "ag", "1")
    assert reg.pop("a").caller == "1"
    assert reg.pop("a") is None                      # tek kullanımlık
    reg.put("b", "ag", "2")
    now[0] += 30.5
    assert reg.pop("b") is None                      # süresi doldu
    for k in "cdef":
        reg.put(k, "ag", k)
    assert len(reg) == 3 and reg.pop("c") is None     # en eski atıldı
    assert reg.pop("f").caller == "f"


# --- AudioSocket uçtan uca ----------------------------------------------------


def test_call_flow_pacing_and_hangup(env):
    client, store = env
    sock = _start_call(client)
    for _ in range(5):                                # 100 ms 8 kHz → tek 16 kHz parça
        _send(sock, tel.KIND_AUDIO, FRAME_IN)

    audio_times = []
    while len(audio_times) < 15:                      # 0.3 s @ 8 kHz = 15 × 320 bayt
        kind, payload = _read_frame(sock)
        assert kind == tel.KIND_AUDIO
        assert len(payload) == tel.FRAME_BYTES
        audio_times.append(time.monotonic())
    gaps = [b - a for a, b in zip(audio_times, audio_times[1:])]
    assert 0.015 <= statistics.median(gaps) <= 0.026
    assert 0.2 <= audio_times[-1] - audio_times[0] <= 0.45

    _send(sock, tel.KIND_DTMF, b"5")                  # yok sayılır
    _send(sock, tel.KIND_HANGUP)
    frames = _drain_until_eof(sock)
    assert frames and frames[-1] == (tel.KIND_HANGUP, b"")
    sock.close()

    sid = next(iter(store.sessions))
    assert _wait(lambda: store.sessions[sid]["ended"] is not None)
    ended = store.sessions[sid]["ended"]
    assert ended["reason"] == "caller_hangup"
    assert ended["usage"]["audio_in_s"] == pytest.approx(0.1, abs=0.001)   # 3200 bayt @ 16 kHz
    assert store.sessions[sid]["origin"].startswith("tel:+90532***4567|")
    assert len(store.sessions[sid]["origin"].split("|")[1]) == 8

    runner = PhoneRunner.instances[-1]
    assert len(runner.blobs) == 1 and len(runner.blobs[0]) == tel.INPUT_CHUNK_BYTES
    assert "Telefon selamı" in runner.contents[0]
    instruction = runner.agent.instruction(None)
    assert tel.PHONE_BASE_INSTRUCTION in instruction
    assert tel.PHONE_KNOWN_CALLER in instruction and "Ek telefon kuralı." in instruction
    assert ("agent", "Merhaba.") in [(r, t) for s, r, t in store.transcripts if s == sid]
    assert _wait(lambda: sid not in crm_supabase._session_callers)  # oturum bitince unutuldu


def test_interrupted_clears_buffer(env):
    client, store = env
    PhoneRunner.first_audio_s = 2.0                   # 100 çerçevelik uzun yanıt
    sock = _start_call(client)
    for _ in range(5):
        _send(sock, tel.KIND_AUDIO, FRAME_IN)
    received = 0
    while received < 5:
        kind, _ = _read_frame(sock)
        received += kind == tel.KIND_AUDIO
    for _ in range(5):                                # ikinci parça → interrupted
        _send(sock, tel.KIND_AUDIO, FRAME_IN)
    sock.settimeout(0.5)
    try:
        while True:
            f = _read_frame(sock)
            if f is None:
                break
            received += f[0] == tel.KIND_AUDIO
    except socket.timeout:
        pass
    assert received < 40                              # tampon temizlendi (aksi halde 100)
    sock.settimeout(3)
    _send(sock, tel.KIND_HANGUP)
    _drain_until_eof(sock)
    sock.close()
    sid = next(iter(store.sessions))
    assert _wait(lambda: store.sessions[sid]["ended"] is not None)
    assert store.sessions[sid]["ended"]["reason"] == "caller_hangup"


def test_eof_is_caller_hangup(env):
    client, store = env
    sock = _start_call(client)
    assert _wait(lambda: len(store.sessions) == 1)
    sock.shutdown(socket.SHUT_WR)                     # arayan kapattı (EOF)
    _drain_until_eof(sock)
    sock.close()
    sid = next(iter(store.sessions))
    assert _wait(lambda: store.sessions[sid]["ended"] is not None)
    assert store.sessions[sid]["ended"]["reason"] == "caller_hangup"


def test_unregistered_uuid_rejected(env):
    client, store = env
    sock = _connect(client)
    _send(sock, tel.KIND_UUID, uuid.uuid4().bytes)
    assert _drain_until_eof(sock) == []
    sock.close()
    # İlk çerçeve UUID değilse de reddedilir
    sock = _connect(client)
    _send(sock, tel.KIND_AUDIO, FRAME_IN)
    assert _drain_until_eof(sock) == []
    sock.close()
    # Aşırı uzun çerçeve
    sock = _connect(client)
    sock.sendall(bytes([tel.KIND_UUID]) + (60000).to_bytes(2, "big"))
    assert _drain_until_eof(sock) == []
    sock.close()
    assert store.sessions == {}


def test_uuid_single_use_over_socket(env):
    client, store = env
    call_id = str(uuid.uuid4())
    _register(client, call_id)
    sock = _connect(client)
    _send(sock, tel.KIND_UUID, uuid.UUID(call_id).bytes)
    assert _wait(lambda: len(store.sessions) == 1)
    sock2 = _connect(client)
    _send(sock2, tel.KIND_UUID, uuid.UUID(call_id).bytes)
    assert _drain_until_eof(sock2) == []
    sock2.close()
    _send(sock, tel.KIND_HANGUP)
    _drain_until_eof(sock)
    sock.close()
    assert len(store.sessions) == 1


def test_per_caller_daily_limit(env):
    client, store = env
    origin = tel.caller_origin(CALLER_NORM, SECRET)
    for i in range(5):
        store.sessions[f"eski{i}"] = {"agent_id": "tel-agent", "origin": origin, "ended": None}
    sock = _start_call(client)
    assert _drain_until_eof(sock) == [(tel.KIND_HANGUP, b"")]
    sock.close()
    assert len(store.sessions) == 5

    # Gizli numara için arayan başına limit uygulanmaz
    for i in range(5):
        store.sessions[f"anon{i}"] = {"agent_id": "tel-agent", "origin": "tel:anonymous", "ended": None}
    sock = _start_call(client, caller="anonymous")
    assert _wait(lambda: len(store.sessions) == 11)
    _send(sock, tel.KIND_HANGUP)
    _drain_until_eof(sock)
    sock.close()
    runner = PhoneRunner.instances[-1]
    assert tel.PHONE_HIDDEN_CALLER in runner.agent.instruction(None)
    assert tel.PHONE_KNOWN_CALLER not in runner.agent.instruction(None)


def test_general_daily_limit_applies(env):
    client, store = env
    store.usage = {"sessions": 200, "seconds": 0}
    sock = _start_call(client)
    assert _drain_until_eof(sock) == [(tel.KIND_HANGUP, b"")]
    sock.close()
    assert store.sessions == {}


# --- yardımcı fonksiyonlar ------------------------------------------------------


def test_phone_instruction_by_caller_state():
    agent = make_agent(telephony={"enabled": True})
    known = tel.build_phone_instruction(agent, has_caller=True)
    hidden = tel.build_phone_instruction(agent, has_caller=False)
    assert known.startswith("Bu bir telefon görüşmesi; cümleler kısa olsun; e-posta isteme.")
    assert "numarayı tekrar sorma" in known and "Numara görünmüyor" not in known
    assert "Numara görünmüyor; randevu için telefon numarasını sor." in hidden
    extra = make_agent(telephony={"enabled": True, "instructions": "Mesai 9-18."})
    assert tel.build_phone_instruction(extra, has_caller=False).endswith("Mesai 9-18.")
    # Ek talimat sistem talimatının sonuna eklenir; widget talimatı değişmez
    assert live.build_instruction(agent, known).endswith(known)
    assert known not in live.build_instruction(agent)


@pytest.mark.parametrize("raw", ["", "  ", "anonymous", "Anonymous", "unknown", "abc"])
def test_hidden_callers(raw):
    assert tel.normalize_caller(raw) is None
    assert tel.caller_origin(tel.normalize_caller(raw), SECRET) == "tel:anonymous"


def test_caller_normalize_mask_and_origin():
    for raw in ("05321234567", "+90 532 123 45 67", "905321234567", "5321234567"):
        assert tel.normalize_caller(raw) == CALLER_NORM
    assert tel.mask_phone(CALLER_NORM) == "+90532***4567"
    o1 = tel.caller_origin(CALLER_NORM, SECRET)
    assert o1 == tel.caller_origin(CALLER_NORM, SECRET)
    assert o1 != tel.caller_origin(CALLER_NORM, "baska-anahtar")
    assert o1 != tel.caller_origin("+905321234568", SECRET)
    assert "1234567" not in o1


def test_frame_roundtrip():
    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(tel.encode_frame(tel.KIND_AUDIO, b"\x01\x02") + tel.encode_frame(tel.KIND_HANGUP))
        reader.feed_eof()
        assert await tel.read_frame(reader) == (tel.KIND_AUDIO, b"\x01\x02")
        assert await tel.read_frame(reader) == (tel.KIND_HANGUP, b"")
    asyncio.run(go())


# --- CRM: arayan numarasının book() içine enjeksiyonu -------------------------


@pytest.fixture
def crm_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", KEY)
    crm_supabase._session_leads.clear()
    crm_supabase._session_callers.clear()
    yield
    crm_supabase._session_callers.clear()


def test_book_uses_session_caller(crm_env):
    tool = SupabaseCrmTool(type="supabase_crm")
    args = {"name": "Ali Veli", "date": "2026-10-02", "time": "14:00"}
    fake = FakePostgrest()
    assert asyncio.run(crm_supabase.book(tool, args, session_id="t0", client=client_for(fake)))["error"] == "invalid_args"

    crm_supabase.set_session_caller("t1", "0532 123 45 67")
    res = asyncio.run(crm_supabase.book(tool, args, session_id="t1", client=client_for(fake)))
    assert res["ok"] is True
    assert fake.tables["crm_leads"][0]["phone"] == CALLER_NORM

    # Model açıkça numara verdiyse o kullanılır
    fake2 = FakePostgrest()
    crm_supabase.set_session_caller("t2", CALLER)
    asyncio.run(crm_supabase.book(tool, dict(args, phone="0555 000 11 22"), session_id="t2",
                                  client=client_for(fake2)))
    assert fake2.tables["crm_leads"][0]["phone"] == "+905550001122"

    crm_supabase.forget_session_caller("t1")
    assert "t1" not in crm_supabase._session_callers
    crm_supabase.set_session_caller("t3", "anonymous")  # geçersiz numara hatırlanmaz
    assert "t3" not in crm_supabase._session_callers


# --- ayarlar --------------------------------------------------------------------


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("TELEPHONY_ENABLED", "true")
    monkeypatch.setenv("AUDIOSOCKET_HOST", "127.0.0.1")
    monkeypatch.setenv("AUDIOSOCKET_PORT", "9999")
    monkeypatch.setenv("TELEPHONY_SECRET", "cok-gizli")
    s = Settings.from_env()
    assert s.telephony_enabled is True and s.audiosocket_host == "127.0.0.1" and s.audiosocket_port == 9999
    assert s.telephony_secret == "cok-gizli" and "cok-gizli" not in repr(s)
    for name in ("TELEPHONY_ENABLED", "AUDIOSOCKET_HOST", "AUDIOSOCKET_PORT", "TELEPHONY_SECRET"):
        monkeypatch.delenv(name)
    d = Settings.from_env()
    assert (d.telephony_enabled, d.audiosocket_host, d.audiosocket_port, d.telephony_secret) == \
        (False, "0.0.0.0", 9092, None)


def test_store_count_sessions_today(tmp_path):
    from server.storage import Store

    store = Store(tmp_path / "db.sqlite")
    origin = tel.caller_origin(CALLER_NORM, SECRET)
    store.create_session("tel-agent", origin)
    store.create_session("tel-agent", origin)
    store.create_session("tel-agent", "tel:anonymous")
    store.create_session("baska", origin)
    assert store.count_sessions_today("tel-agent", origin) == 2
    assert store.count_sessions_today("tel-agent", "tel:anonymous") == 1
    assert store.get_session(store.list_sessions("baska")[0]["id"])["origin"] == origin
    store.close()
