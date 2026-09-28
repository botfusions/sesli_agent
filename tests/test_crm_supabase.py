"""Supabase CRM aracı testleri — bellek içi sahte PostgREST (ağ yok)."""

from __future__ import annotations

import asyncio
import json
import uuid
from urllib.parse import parse_qs

import httpx
import pytest

from server import crm_supabase, tools
from server.config import AgentConfig, SupabaseCrmTool

KEY = "service-role-GIZLI-anahtar"


class FakePostgrest:
    """crm_leads / crm_tasks için eq filtresi, POST, PATCH destekleyen sahte REST."""

    def __init__(self, fail: dict[str, int] | None = None):
        self.tables: dict[str, list[dict]] = {"crm_leads": [], "crm_tasks": []}
        self.fail = fail or {}          # {"crm_tasks:POST": 500}
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == KEY
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        table = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((request.method, table))
        code = self.fail.get(f"{table}:{request.method}")
        if code:
            return httpx.Response(code, json={"message": "hata"})
        if table not in self.tables:
            return httpx.Response(404, json={"code": "PGRST205"})
        rows = self.tables[table]
        q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        filters = {k: v[3:] for k, v in q.items() if v.startswith("eq.")}

        def match(r):
            return all(str(r.get(k)) == v for k, v in filters.items())

        if request.method == "GET":
            sel = q.get("select", "*")
            found = [r for r in rows if match(r)][: int(q.get("limit", "1000"))]
            if sel != "*":
                cols = sel.split(",")
                found = [{c: r.get(c) for c in cols} for r in found]
            return httpx.Response(200, json=found)
        if request.method == "POST":
            new = []
            for item in json.loads(request.content):
                item = dict(item, id=str(uuid.uuid4()))
                rows.append(item)
                new.append(item)
            prefer = request.headers.get("Prefer", "")
            return httpx.Response(201, json=new) if "representation" in prefer else httpx.Response(201)
        if request.method == "PATCH":
            body = json.loads(request.content)
            for r in rows:
                if match(r):
                    r.update(body)
            return httpx.Response(204)
        return httpx.Response(405)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", KEY)
    crm_supabase._session_leads.clear()
    tools.reset_call_counts()


def client_for(fake: FakePostgrest) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))


def run(coro):
    return asyncio.run(coro)


ARGS = {"name": "Ayşe Yılmaz", "email": " Ayse@Example.com ", "preferred_time": "cuma 10:00"}


def test_new_lead_and_task(env):
    fake = FakePostgrest()
    tool = SupabaseCrmTool(type="supabase_crm")
    res = run(crm_supabase.book(tool, ARGS, session_id="s1", client=client_for(fake)))
    assert res == {"ok": True, "data": {"lead": "created", "task_created": True}}
    lead = fake.tables["crm_leads"][0]
    assert lead["email"] == "ayse@example.com"
    assert lead["source"] == "Sesli Asistan" and lead["status"] == "Meeting Scheduled"
    assert lead["tags"] == ["sesli-asistan"]
    assert lead["lead_name"] == ARGS["name"] and lead["budget"] == 0 and "full_name" not in lead
    task = fake.tables["crm_tasks"][0]
    assert task["lead_id"] == lead["id"] and "cuma 10:00" in task["title"]
    assert KEY not in json.dumps(res)


def test_existing_lead_updated_not_duplicated(env):
    fake = FakePostgrest()
    fake.tables["crm_leads"].append({"id": "L1", "email": "ayse@example.com", "tags": ["vip"], "status": "New Lead"})
    tool = SupabaseCrmTool(type="supabase_crm")
    res = run(crm_supabase.book(tool, ARGS, session_id="s1", client=client_for(fake)))
    assert res["ok"] and res["data"]["lead"] == "updated"
    assert len(fake.tables["crm_leads"]) == 1
    lead = fake.tables["crm_leads"][0]
    assert lead["status"] == "Meeting Scheduled" and lead["tags"] == ["vip", "sesli-asistan"]


def test_phone_fallback_and_normalization(env):
    fake = FakePostgrest()
    fake.tables["crm_leads"].append({"id": "L2", "phone": "+905321234567", "tags": []})
    tool = SupabaseCrmTool(type="supabase_crm")
    args = {"name": "Mehmet", "phone": "0532 123 45 67", "preferred_time": "yarın"}
    res = run(crm_supabase.book(tool, args, session_id="s2", client=client_for(fake)))
    assert res["ok"] and res["data"]["lead"] == "updated"


def test_requires_contact(env):
    tool = SupabaseCrmTool(type="supabase_crm")
    res = run(crm_supabase.book(tool, {"name": "X", "preferred_time": "yarın", "email": "gecersiz"},
                                session_id="s", client=client_for(FakePostgrest())))
    assert res == {"ok": False, "error": "invalid_args"}


def test_not_configured(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    tool = SupabaseCrmTool(type="supabase_crm")
    assert run(crm_supabase.book(tool, ARGS, session_id="s"))["error"] == "crm_not_configured"


def test_plain_http_url_rejected(env, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://kotu.example.com")
    tool = SupabaseCrmTool(type="supabase_crm")
    assert run(crm_supabase.book(tool, ARGS, session_id="s"))["error"] == "crm_not_configured"


@pytest.mark.parametrize("status,code", [(401, "crm_unauthorized"), (500, "crm_error")])
def test_http_errors(env, status, code):
    fake = FakePostgrest(fail={"crm_leads:GET": status})
    tool = SupabaseCrmTool(type="supabase_crm")
    assert run(crm_supabase.book(tool, ARGS, session_id="s", client=client_for(fake)))["error"] == code


def test_task_failure_does_not_fail_booking(env):
    fake = FakePostgrest(fail={"crm_tasks:POST": 500})
    tool = SupabaseCrmTool(type="supabase_crm")
    res = run(crm_supabase.book(tool, ARGS, session_id="s", client=client_for(fake)))
    assert res["ok"] and res["data"]["task_created"] is False and len(fake.tables["crm_leads"]) == 1


def test_no_tasks_table(env):
    fake = FakePostgrest()
    tool = SupabaseCrmTool(type="supabase_crm", tasks_table=None)
    res = run(crm_supabase.book(tool, ARGS, session_id="s", client=client_for(fake)))
    assert res["data"]["task_created"] is False and fake.tables["crm_tasks"] == []


def test_attach_summary_with_notes_column(env):
    fake = FakePostgrest()
    tool = SupabaseCrmTool(type="supabase_crm", notes_column="notes")
    c = client_for(fake)
    run(crm_supabase.book(tool, ARGS, session_id="s9", client=c))
    assert run(crm_supabase.attach_summary("s9", "- Demo istendi", client=c)) is True
    assert "- Demo istendi" in fake.tables["crm_leads"][0]["notes"]
    # ikinci kez eklenmez (oturum eşlemesi tüketildi)
    assert run(crm_supabase.attach_summary("s9", "tekrar", client=c)) is False


def test_attach_summary_skipped_without_notes_column(env):
    fake = FakePostgrest()
    tool = SupabaseCrmTool(type="supabase_crm")
    c = client_for(fake)
    run(crm_supabase.book(tool, ARGS, session_id="s8", client=c))
    assert run(crm_supabase.attach_summary("s8", "özet", client=c)) is False


def test_adk_tool_dispatch_and_events(env):
    fake = FakePostgrest()
    agent = AgentConfig(id="a1", name="A", instructions="i", tools=[{"type": "supabase_crm"}])
    events: list[dict] = []

    async def on_event(e):
        events.append(e)

    built = tools.build_adk_tools(agent, on_event, session_id="s5", client=client_for(fake))
    assert isinstance(built[0], tools.SupabaseCrmAdkTool)
    # kesin onay: bloklayıcı çalışır (NON_BLOCKING değil)
    assert built[0].behavior is None
    decl = built[0]._get_declaration()
    assert decl.name == "book_demo" and set(decl.parameters.required) == {"name", "preferred_time"}
    res = run(built[0].run_async(args=ARGS, tool_context=None))
    assert res["ok"] is True
    assert [e["status"] for e in events] == ["start", "ok"]


def test_adk_tool_error_is_safe(env):
    fake = FakePostgrest(fail={"crm_leads:GET": 401})
    agent = AgentConfig(id="a1", name="A", instructions="i", tools=[{"type": "supabase_crm"}])

    async def on_event(e):
        pass

    t = tools.build_adk_tools(agent, on_event, session_id="s6", client=client_for(fake))[0]
    res = run(t.run_async(args=ARGS, tool_context=None))
    assert res == {"ok": False, "error": "crm_unauthorized", "message": tools.error_message("crm_unauthorized")}
    assert "supabase" not in json.dumps(res).lower()


def test_yaml_rejects_secret_values():
    with pytest.raises(Exception):
        SupabaseCrmTool(type="supabase_crm", key_env="eyJhbGciOiJIUzI1NiJ9.gizli")
