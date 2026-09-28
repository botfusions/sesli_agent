"""server/tools.py testleri — ağ erişimi yok, httpx.MockTransport kullanılır."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Literal

import httpx
import pytest
from pydantic import BaseModel, HttpUrl

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import tools  # noqa: E402

# Ajan A'nın config.py'si varsa onu kullan; yoksa SPEC'e uygun küçük stub.
try:  # pragma: no cover - ortama bağlı
    from server.config import AgentConfig, ToolParam, WebhookTool  # type: ignore
except Exception:  # noqa: BLE001
    class ToolParam(BaseModel):  # type: ignore[no-redef]
        type: Literal["string", "number", "integer", "boolean"]
        description: str = ""
        enum: list[str] | None = None
        required: bool = True

    class WebhookTool(BaseModel):  # type: ignore[no-redef]
        name: str
        description: str
        parameters: dict[str, ToolParam] = {}
        url: HttpUrl
        method: Literal["POST", "GET"] = "POST"
        timeout_s: float = 8.0
        secret_env: str | None = None
        speak_while_running: bool = True

    class AgentConfig(BaseModel):  # type: ignore[no-redef]
        id: str
        name: str
        instructions: str
        tools: list[WebhookTool] = []


pytestmark = pytest.mark.asyncio

SECRET = "test-secret-123"
URL = "https://customer.example.com/webhooks/book_demo"


def make_tool(**kw) -> WebhookTool:
    data = dict(
        name="book_demo",
        description="Demo toplantısı planlar.",
        url=URL,
        parameters={
            "name": {"type": "string", "description": "Ad soyad"},
            "slot": {"type": "string", "enum": ["sabah", "ogle", "aksam"]},
            "people": {"type": "integer", "required": False},
            "budget": {"type": "number", "required": False},
            "urgent": {"type": "boolean", "required": False},
        },
    )
    data.update(kw)
    return WebhookTool.model_validate(data)


GOOD_ARGS = {"name": "Ayşe", "slot": "sabah"}


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    tools.reset_call_counts()
    monkeypatch.setenv("BF_SECRET", SECRET)
    yield
    tools.reset_call_counts()


# --- call_webhook -------------------------------------------------------------


async def test_success_sends_signed_json():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["req"] = request
        return httpx.Response(200, json={"booked": True})

    tool = make_tool(secret_env="BF_SECRET")
    async with client_for(handler) as c:
        res = await tools.call_webhook(tool, {**GOOD_ARGS, "extra": 1},
                                       session_id="s1", agent_id="a1", client=c)
    assert res == {"ok": True, "data": {"booked": True}}

    req = seen["req"]
    assert req.method == "POST"
    body = json.loads(req.content)
    assert body["tool"] == "book_demo"
    assert body["args"] == GOOD_ARGS  # bilinmeyen "extra" atıldı
    assert body["session_id"] == "s1" and body["agent_id"] == "a1"
    assert isinstance(body["ts"], int)
    ts = req.headers["X-Botfusions-Timestamp"]
    sig = req.headers["X-Botfusions-Signature"]
    expected = "sha256=" + hmac.new(SECRET.encode(), f"{ts}.".encode() + req.content,
                                    hashlib.sha256).hexdigest()
    assert sig == expected
    assert tools.verify_signature(req.content, sig, SECRET, ts)


async def test_no_secret_env_means_no_signature():
    seen = {}

    def handler(request):
        seen["req"] = request
        return httpx.Response(200, json={})

    async with client_for(handler) as c:
        res = await tools.call_webhook(make_tool(), GOOD_ARGS, session_id="s", agent_id="a", client=c)
    assert res["ok"] is True
    assert "X-Botfusions-Signature" not in seen["req"].headers
    assert "X-Botfusions-Timestamp" in seen["req"].headers


async def test_get_method_signs_query_string():
    seen = {}

    def handler(request):
        seen["req"] = request
        return httpx.Response(200, json={"ok": 1})

    tool = make_tool(method="GET", secret_env="BF_SECRET")
    async with client_for(handler) as c:
        res = await tools.call_webhook(tool, GOOD_ARGS, session_id="s", agent_id="a", client=c)
    assert res["ok"] is True
    req = seen["req"]
    assert req.method == "GET" and req.content == b""
    assert req.url.params["name"] == "Ayşe"
    assert tools.verify_signature(req.url.query, req.headers["X-Botfusions-Signature"],
                                  SECRET, req.headers["X-Botfusions-Timestamp"])


async def test_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    async with client_for(handler) as c:
        res = await tools.call_webhook(make_tool(), GOOD_ARGS, session_id="s", agent_id="a", client=c)
    assert res == {"ok": False, "error": "timeout"}


async def test_total_timeout_enforced():
    async def handler(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    tool = make_tool(timeout_s=1.0)
    start = time.monotonic()
    async with client_for(handler) as c:
        res = await tools.call_webhook(tool, GOOD_ARGS, session_id="s", agent_id="a", client=c)
    assert res == {"ok": False, "error": "timeout"}
    assert time.monotonic() - start < 3


async def test_network_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    async with client_for(handler) as c:
        res = await tools.call_webhook(make_tool(), GOOD_ARGS, session_id="s", agent_id="a", client=c)
    assert res == {"ok": False, "error": "network_error"}


async def test_http_500():
    async with client_for(lambda r: httpx.Response(500, text="Traceback: secret stuff")) as c:
        res = await tools.call_webhook(make_tool(), GOOD_ARGS, session_id="s", agent_id="a", client=c)
    assert res == {"ok": False, "error": "http_500"}


async def test_response_too_large():
    big = json.dumps({"x": "a" * (70 * 1024)}).encode()

    def handler(request):
        # content-length başlığı olmadan akış olarak gönder (parça parça sınır kontrolü)
        async def gen():
            for i in range(0, len(big), 8192):
                yield big[i:i + 8192]
        return httpx.Response(200, content=gen())

    async with client_for(handler) as c:
        res = await tools.call_webhook(make_tool(), GOOD_ARGS, session_id="s", agent_id="a", client=c)
    assert res == {"ok": False, "error": "response_too_large"}

    async with client_for(lambda r: httpx.Response(200, content=big)) as c:
        res = await tools.call_webhook(make_tool(), GOOD_ARGS, session_id="s", agent_id="a", client=c)
    assert res == {"ok": False, "error": "response_too_large"}


async def test_invalid_json():
    async with client_for(lambda r: httpx.Response(200, text="<html>ok</html>")) as c:
        res = await tools.call_webhook(make_tool(), GOOD_ARGS, session_id="s", agent_id="a", client=c)
    assert res == {"ok": False, "error": "invalid_json"}


async def test_empty_body_is_ok():
    async with client_for(lambda r: httpx.Response(204)) as c:
        res = await tools.call_webhook(make_tool(), GOOD_ARGS, session_id="s", agent_id="a", client=c)
    assert res == {"ok": True, "data": None}


async def test_missing_secret_makes_no_request(monkeypatch):
    monkeypatch.delenv("BF_SECRET", raising=False)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    async with client_for(handler) as c:
        res = await tools.call_webhook(make_tool(secret_env="BF_SECRET"), GOOD_ARGS,
                                       session_id="s", agent_id="a", client=c)
    assert res == {"ok": False, "error": "missing_secret"}
    assert calls == []


@pytest.mark.parametrize("args", [
    {"slot": "sabah"},                                  # zorunlu eksik
    {"name": "A", "slot": "gece"},                      # enum dışı
    {"name": 5, "slot": "sabah"},                       # string değil
    {"name": "A", "slot": "sabah", "people": "iki"},    # integer değil
    {"name": "A", "slot": "sabah", "people": 2.5},      # tam sayı değil
    {"name": "A", "slot": "sabah", "people": True},     # bool integer sayılmaz
    {"name": "A", "slot": "sabah", "budget": "çok"},    # number değil
    {"name": "A", "slot": "sabah", "urgent": "evet"},   # boolean değil
    "not-a-dict",
])
async def test_invalid_args(args):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    async with client_for(handler) as c:
        res = await tools.call_webhook(make_tool(), args, session_id="s", agent_id="a", client=c)
    assert res == {"ok": False, "error": "invalid_args"}
    assert calls == []


async def test_valid_optional_args_and_int_coercion():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    args = {**GOOD_ARGS, "people": 3.0, "budget": 1500, "urgent": False}
    async with client_for(handler) as c:
        res = await tools.call_webhook(make_tool(), args, session_id="s", agent_id="a", client=c)
    assert res["ok"] is True
    assert seen["body"]["args"]["people"] == 3 and isinstance(seen["body"]["args"]["people"], int)
    assert seen["body"]["args"]["urgent"] is False


async def test_rate_limit_per_session_and_tool():
    async with client_for(lambda r: httpx.Response(200, json={})) as c:
        for _ in range(tools.MAX_CALLS_PER_SESSION_TOOL):
            res = await tools.call_webhook(make_tool(), GOOD_ARGS, session_id="s1", agent_id="a", client=c)
            assert res["ok"] is True
        res = await tools.call_webhook(make_tool(), GOOD_ARGS, session_id="s1", agent_id="a", client=c)
        assert res == {"ok": False, "error": "rate_limited"}
        # başka oturum ve başka araç etkilenmez
        res = await tools.call_webhook(make_tool(), GOOD_ARGS, session_id="s2", agent_id="a", client=c)
        assert res["ok"] is True
        res = await tools.call_webhook(make_tool(name="other_tool"), GOOD_ARGS,
                                       session_id="s1", agent_id="a", client=c)
        assert res["ok"] is True
        # oturum sayacı temizlenince yeniden çağrılabilir
        tools.reset_call_counts("s1")
        res = await tools.call_webhook(make_tool(), GOOD_ARGS, session_id="s1", agent_id="a", client=c)
        assert res["ok"] is True


# --- verify_signature ---------------------------------------------------------


def _sig(body: bytes, ts: str, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


async def test_verify_signature_valid_wrong_and_stale():
    body = b'{"a":1}'
    now = int(time.time())
    ts = str(now)
    assert tools.verify_signature(body, _sig(body, ts), SECRET, ts)
    # yanlış anahtar / değişmiş gövde / değişmiş zaman damgası
    assert not tools.verify_signature(body, _sig(body, ts, "other"), SECRET, ts)
    assert not tools.verify_signature(b'{"a":2}', _sig(body, ts), SECRET, ts)
    assert not tools.verify_signature(body, _sig(body, ts), SECRET, str(now - 1))
    # 5 dakikadan eski
    old = str(now - 301)
    assert not tools.verify_signature(body, _sig(body, old), SECRET, old)
    assert tools.verify_signature(body, _sig(body, str(now - 299)), SECRET, str(now - 299))
    # eksik/bozuk girdiler
    assert not tools.verify_signature(body, "", SECRET, ts)
    assert not tools.verify_signature(body, _sig(body, ts), SECRET, None)
    assert not tools.verify_signature(body, _sig(body, ts), SECRET, "abc")
    assert not tools.verify_signature(body, "garbage", SECRET, ts)


async def test_example_receiver_accepts_our_signature(monkeypatch):
    """examples/webhook_receiver.py bağımsız doğrulayıcısı tools.sign ile uyumlu."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    spec = importlib.util.spec_from_file_location("webhook_receiver", ROOT / "examples" / "webhook_receiver.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("BOTFUSIONS_WEBHOOK_SECRET", SECRET)

    body = json.dumps({"tool": "book_demo", "args": {"name": "Ayşe", "date": "2026-10-01"},
                       "session_id": "s", "agent_id": "a", "ts": 1}).encode()
    ts = str(int(time.time()))
    client = TestClient(mod.app)
    r = client.post("/webhooks/book_demo", content=body, headers={
        "X-Botfusions-Timestamp": ts, "X-Botfusions-Signature": tools.sign(body, SECRET, ts),
        "Content-Type": "application/json"})
    assert r.status_code == 200 and r.json()["booked"] is True
    r = client.post("/webhooks/book_demo", content=body, headers={
        "X-Botfusions-Timestamp": ts, "X-Botfusions-Signature": tools.sign(body, "yanlis", ts)})
    assert r.status_code == 401


# --- build_adk_tools ----------------------------------------------------------


def make_agent(*tool_list) -> AgentConfig:
    return AgentConfig.model_validate({
        "id": "botfusions-satis", "name": "Satış", "instructions": "Yardımcı ol.",
        "tools": [t.model_dump(mode="json") for t in tool_list],
    })


async def _noop(event):
    return None


async def test_build_adk_tools_declaration():
    from google.adk.tools import BaseTool
    from google.genai import types

    agent = make_agent(make_tool(), make_tool(name="ping", description="Ping", parameters={},
                                              speak_while_running=False))
    built = tools.build_adk_tools(agent, _noop)
    assert len(built) == 2 and all(isinstance(t, BaseTool) for t in built)

    decl = built[0]._get_declaration()
    assert isinstance(decl, types.FunctionDeclaration)
    assert decl.name == "book_demo" and decl.description == "Demo toplantısı planlar."
    params = decl.parameters
    assert params.type == types.Type.OBJECT
    assert set(params.properties) == {"name", "slot", "people", "budget", "urgent"}
    assert params.properties["name"].type == types.Type.STRING
    assert params.properties["name"].description == "Ad soyad"
    assert params.properties["slot"].enum == ["sabah", "ogle", "aksam"]
    assert params.properties["people"].type == types.Type.INTEGER
    assert params.properties["budget"].type == types.Type.NUMBER
    assert params.properties["urgent"].type == types.Type.BOOLEAN
    assert sorted(params.required) == ["name", "slot"]

    # canlı modda non-blocking davranışı
    assert built[0].behavior == types.Behavior.NON_BLOCKING
    assert built[0].response_scheduling == types.FunctionResponseScheduling.WHEN_IDLE
    assert built[1].behavior is None and built[1].response_scheduling is None
    assert built[1]._get_declaration().parameters is None


async def test_adk_request_accepts_tools():
    """ADK'nın kendi LlmRequest.append_tools yolu bildirimi eklemeli."""
    from google.adk.models.llm_request import LlmRequest

    built = tools.build_adk_tools(make_agent(make_tool()), _noop)
    req = LlmRequest()
    req.append_tools(built)
    names = [d.name for t in req.config.tools for d in (t.function_declarations or [])]
    assert names == ["book_demo"]
    assert req.tools_dict["book_demo"] is built[0]


class _Ctx:
    class session:  # noqa: N801 — ToolContext.session.id taklidi
        id = "adk-session-1"


async def test_run_async_events_order_and_safe_response():
    events = []

    async def on_event(e):
        events.append(e)

    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"booked": True})

    async with client_for(handler) as c:
        built = tools.build_adk_tools(make_agent(make_tool()), on_event, client=c)
        res = await built[0].run_async(args=GOOD_ARGS, tool_context=_Ctx())
    assert res == {"ok": True, "data": {"booked": True}}
    assert seen["body"]["session_id"] == "adk-session-1"
    assert seen["body"]["agent_id"] == "botfusions-satis"
    assert [e["status"] for e in events] == ["start", "ok"]
    assert all(e["type"] == "tool" and e["name"] == "book_demo" for e in events)
    assert isinstance(events[-1]["summary"], str) and events[-1]["summary"]


async def test_run_async_error_hides_internal_details():
    events = []

    async def on_event(e):
        events.append(e)

    async with client_for(lambda r: httpx.Response(503, text="db password=hunter2")) as c:
        built = tools.build_adk_tools(make_agent(make_tool()), on_event, session_id="store-1", client=c)
        res = await built[0].run_async(args=GOOD_ARGS, tool_context=_Ctx())
    assert res["ok"] is False and res["error"] == "http_503"
    dumped = json.dumps(res, ensure_ascii=False)
    assert "hunter2" not in dumped and "customer.example.com" not in dumped
    assert res["message"]  # Türkçe açıklama
    assert [e["status"] for e in events] == ["start", "error"]
    assert events[-1]["summary"] == res["message"]


async def test_on_event_failure_does_not_break_tool():
    async def bad(e):
        raise RuntimeError("ui down")

    async with client_for(lambda r: httpx.Response(200, json={"x": 1})) as c:
        built = tools.build_adk_tools(make_agent(make_tool()), bad, session_id="s", client=c)
        res = await built[0].run_async(args=GOOD_ARGS, tool_context=None)
    assert res == {"ok": True, "data": {"x": 1}}
