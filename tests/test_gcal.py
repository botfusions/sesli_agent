"""Google Takvim testleri — sahte Calendar REST + sahte Supabase (ağ yok)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import httpx
import pytest

from server import crm_supabase, gcal, tools
from server.config import AgentConfig, CalendarConfig, SupabaseCrmTool
from tests.test_crm_supabase import KEY, FakePostgrest

CAL_ID = "takvim@group.calendar.google.com"
TZ = dt.timezone(dt.timedelta(hours=3))
DATE = dt.date(2026, 10, 9)  # Cuma
NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=TZ)


def run(coro):
    return asyncio.run(coro)


class FakeCalendar:
    """freeBusy + events.insert; Supabase isteklerini FakePostgrest'e devreder."""

    def __init__(self, busy: list[tuple[str, str]] = (), postgrest: FakePostgrest | None = None,
                 fail: int | None = None):
        self.busy = list(busy)
        self.pg = postgrest or FakePostgrest()
        self.fail = fail
        self.events: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "www.googleapis.com":
            return self.pg.handler(request)
        assert request.headers["Authorization"] == "Bearer sahte-token"
        if self.fail:
            return httpx.Response(self.fail, json={"error": {"message": "hata"}})
        if request.url.path.endswith("/freeBusy"):
            body = json.loads(request.content)
            assert body["items"] == [{"id": CAL_ID}]
            day = body["timeMin"][:10]  # meşgul aralıklar sorulan güne yazılır
            busy = [{"start": f"{day}T{a}:00+03:00", "end": f"{day}T{b}:00+03:00"} for a, b in self.busy]
            return httpx.Response(200, json={"calendars": {CAL_ID: {"busy": busy}}})
        if request.url.path.endswith(f"/calendars/{CAL_ID}/events"):
            ev = json.loads(request.content)
            self.events.append(ev)
            return httpx.Response(200, json={"id": "ev1", "htmlLink": "https://calendar.google.com/event?eid=ev1"})
        return httpx.Response(404, json={})


def client_for(fake) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://supabase.example.com")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", KEY)
    monkeypatch.setenv("GOOGLE_CALENDAR_ID", CAL_ID)

    async def fake_token():
        return "sahte-token"

    monkeypatch.setattr(gcal, "access_token", fake_token)
    tools.reset_call_counts()


CFG = CalendarConfig(work_start="09:00", work_end="12:00", duration_min=30, min_notice_min=60)
TOOL = SupabaseCrmTool(type="supabase_crm", calendar=CFG)
ARGS = {"name": "Ayşe Yılmaz", "email": "ayse@example.com", "date": DATE.isoformat(), "time": "10:00",
        "topic": "Sesli asistan"}


def test_free_slots_excludes_busy_and_past(env):
    fake = FakeCalendar(busy=[("10:00", "10:45")])
    slots = run(gcal.free_slots(CFG, DATE, now=NOW, client=client_for(fake)))
    # 10:00 ve 10:30 meşgul aralıkla çakışır; kalanlar boş
    assert slots == ["09:00", "09:30", "11:00", "11:30"]
    # aynı gün, 10:20'den sonra: min_notice 60 dk → 11:30'dan önce yok
    late = dt.datetime.combine(DATE, dt.time(10, 20), TZ)
    assert run(gcal.free_slots(CFG, DATE, now=late, client=client_for(fake))) == ["11:30"]


def test_free_slots_empty_on_weekend(env):
    fake = FakeCalendar()
    assert run(gcal.free_slots(CFG, dt.date(2026, 10, 10), now=NOW, client=client_for(fake))) == []


def test_book_writes_event_then_crm(env, monkeypatch):
    fake = FakeCalendar()
    res = run(crm_supabase.book(TOOL, ARGS, session_id="g1", client=client_for(fake)))
    assert res["ok"] is True and res["data"]["calendar_event"] is True and res["data"]["task_created"] is True
    ev = fake.events[0]
    assert ev["summary"] == "Sesli asistan — Ayşe Yılmaz"
    assert ev["start"] == {"dateTime": f"{DATE}T10:00:00+03:00", "timeZone": "Europe/Istanbul"}
    assert ev["end"]["dateTime"] == f"{DATE}T10:30:00+03:00"
    assert "ayse@example.com" in ev["description"] and "attendees" not in ev
    assert len(fake.pg.tables["crm_leads"]) == 1 and len(fake.pg.tables["crm_tasks"]) == 1


def test_book_rejects_busy_slot_with_alternatives(env):
    fake = FakeCalendar(busy=[("10:00", "10:30")])
    res = run(crm_supabase.book(TOOL, ARGS, session_id="g2", client=client_for(fake)))
    assert res["ok"] is False and res["error"] == "slot_busy"
    assert "10:00" not in res["free_slots"] and "10:30" in res["free_slots"]
    assert fake.events == [] and fake.pg.tables["crm_leads"] == []


def test_book_calendar_unauthorized_is_safe(env):
    fake = FakeCalendar(fail=403)
    res = run(crm_supabase.book(TOOL, ARGS, session_id="g3", client=client_for(fake)))
    assert res == {"ok": False, "error": "calendar_unauthorized"}
    assert fake.pg.tables["crm_leads"] == []


def test_book_without_calendar_id_env_skips_calendar(env, monkeypatch):
    """Kimlik girilmeden canlıya çıkarsa randevu kırılmaz: takvim adımı atlanır, CRM yazılır."""
    monkeypatch.delenv("GOOGLE_CALENDAR_ID")
    fake = FakeCalendar()
    res = run(crm_supabase.book(TOOL, ARGS, session_id="g4", client=client_for(fake)))
    assert res["ok"] is True and "calendar_event" not in res["data"]
    assert fake.events == [] and len(fake.pg.tables["crm_leads"]) == 1
    # check_availability ise açıkça "yapılandırılmamış" der
    agent = AgentConfig(id="a1", name="A", instructions="i", tools=[{"type": "supabase_crm", "calendar": {}}])

    async def on_event(e):
        pass

    avail = tools.build_adk_tools(agent, on_event, session_id="g4b", client=client_for(fake))[1]
    assert run(avail.run_async(args={"date": DATE.isoformat()}, tool_context=None))["error"] == "calendar_not_configured"


def test_adk_tools_include_check_availability(env):
    fake = FakeCalendar(busy=[("09:00", "09:30")])
    agent = AgentConfig(id="a1", name="A", instructions="i",
                        tools=[{"type": "supabase_crm", "calendar": {"work_start": "09:00", "work_end": "12:00"}}])
    events: list[dict] = []

    async def on_event(e):
        events.append(e)

    built = tools.build_adk_tools(agent, on_event, session_id="g5", client=client_for(fake))
    assert [t.name for t in built] == ["book_demo", "check_availability"]
    avail = built[1]
    assert avail._get_declaration().parameters.required == ["date"]
    # geçmişe düşmesin: ileri bir tarih
    far = dt.date.today() + dt.timedelta(days=7)
    while far.weekday() > 4:
        far += dt.timedelta(days=1)
    res = run(avail.run_async(args={"date": far.isoformat()}, tool_context=None))
    assert res["ok"] is True and res["data"]["duration_min"] == 30
    assert "09:00" not in res["data"]["free_slots"] and "10:00" in res["data"]["free_slots"]
    assert run(avail.run_async(args={"date": "yarın"}, tool_context=None))["error"] == "invalid_args"
    # book_demo slot_busy → free_slots modele iletilir
    busy_args = dict(ARGS, date=far.isoformat(), time="09:00")
    res = run(built[0].run_async(args=busy_args, tool_context=None))
    assert res["error"] == "slot_busy" and "10:00" in res["free_slots"]
    assert events[-1]["status"] == "error"


def test_no_calendar_means_no_extra_tool(env):
    agent = AgentConfig(id="a1", name="A", instructions="i", tools=[{"type": "supabase_crm"}])

    async def on_event(e):
        pass

    assert [t.name for t in tools.build_adk_tools(agent, on_event)] == ["book_demo"]


def test_calendar_config_validation():
    with pytest.raises(Exception):
        CalendarConfig(work_start="9am")
    with pytest.raises(Exception):
        CalendarConfig(timezone="Mars/Olympus")
    with pytest.raises(Exception):
        CalendarConfig(workdays=[7])
    assert CalendarConfig(workdays=[4, 0, 0]).workdays == [0, 4]
