"""Google Takvim: boş saat sorgusu (freeBusy) ve randevu yazma (events.insert).

Kimlik: Vertex için zaten kullanılan servis hesabı (GOOGLE_APPLICATION_CREDENTIALS /
google.auth.default). Takvim o hesabın e-postasıyla "Etkinliklerde değişiklik yapma" yetkisiyle
paylaşılmış olmalı. Takvim kimliği YAML'a değil ortam değişkenine (GOOGLE_CALENDAR_ID) yazılır.

Katılımcı eklenmez: servis hesabı domain yetkisi olmadan davet gönderemez (403). Kişi bilgisi
etkinlik açıklamasına yazılır. Modül istisna olarak yalnızca CalendarError fırlatır.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import zoneinfo
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from server.config import CalendarConfig

logger = logging.getLogger(__name__)

API = "https://www.googleapis.com/calendar/v3"
_SCOPE = "https://www.googleapis.com/auth/calendar"
_creds = None


class CalendarError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _sync_token() -> str:
    global _creds
    import google.auth
    from google.auth.transport.requests import Request

    if _creds is None:
        _creds, _ = google.auth.default(scopes=[_SCOPE])
    if not _creds.valid:
        _creds.refresh(Request())
    return _creds.token


async def access_token() -> str:
    try:
        return await asyncio.to_thread(_sync_token)
    except Exception:  # noqa: BLE001
        logger.exception("Calendar auth failed")
        raise CalendarError("calendar_unauthorized")


def configured(cfg: "CalendarConfig") -> bool:
    """Takvim kimliği ortamda yoksa takvim adımı atlanır (randevu yine CRM'e yazılır)."""
    return bool((os.environ.get(cfg.calendar_id_env) or "").strip())


def calendar_id(cfg: "CalendarConfig") -> str:
    cid = (os.environ.get(cfg.calendar_id_env) or "").strip()
    if not cid:
        raise CalendarError("calendar_not_configured")
    return cid


def tz(cfg: "CalendarConfig") -> dt.tzinfo:
    return zoneinfo.ZoneInfo(cfg.timezone)


async def _request(method: str, path: str, *, json_body: dict | None = None,
                   client: httpx.AsyncClient | None = None, timeout: float = 8.0) -> dict:
    headers = {"Authorization": f"Bearer {await access_token()}"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        resp = await client.request(method, f"{API}/{path}", json=json_body, headers=headers, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise CalendarError("timeout") from exc
    except httpx.HTTPError as exc:
        raise CalendarError("network_error") from exc
    finally:
        if own:
            await client.aclose()
    if resp.status_code >= 400:
        logger.warning("Calendar %s %s → %s %s", method, path, resp.status_code, resp.text[:300])
        if resp.status_code in (401, 403):
            raise CalendarError("calendar_unauthorized")
        if resp.status_code == 404:
            raise CalendarError("calendar_not_found")
        raise CalendarError("calendar_error")
    try:
        return resp.json() if resp.content else {}
    except ValueError as exc:
        raise CalendarError("calendar_error") from exc


def _parse(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _candidates(cfg: "CalendarConfig", date: dt.date) -> list[dt.datetime]:
    """Çalışma saatleri içinde, randevu süresi adımıyla aday başlangıçlar (yerel saat)."""
    if date.weekday() not in cfg.workdays:
        return []
    z = tz(cfg)
    start = dt.datetime.combine(date, dt.time.fromisoformat(cfg.work_start), z)
    end = dt.datetime.combine(date, dt.time.fromisoformat(cfg.work_end), z)
    step = dt.timedelta(minutes=cfg.duration_min)
    out, t = [], start
    while t + step <= end:
        out.append(t)
        t += step
    return out


async def busy_ranges(cfg: "CalendarConfig", date: dt.date, *, client: httpx.AsyncClient | None = None
                      ) -> list[tuple[dt.datetime, dt.datetime]]:
    cid = calendar_id(cfg)
    z = tz(cfg)
    day0 = dt.datetime.combine(date, dt.time.min, z)
    body = {"timeMin": day0.isoformat(), "timeMax": (day0 + dt.timedelta(days=1)).isoformat(),
            "timeZone": cfg.timezone, "items": [{"id": cid}]}
    data = await _request("POST", "freeBusy", json_body=body, client=client, timeout=cfg.timeout_s)
    cal = (data.get("calendars") or {}).get(cid) or {}
    if cal.get("errors"):
        logger.warning("Calendar freeBusy errors: %s", cal["errors"])
        raise CalendarError("calendar_not_found")
    return [(_parse(b["start"]), _parse(b["end"])) for b in cal.get("busy", [])]


async def free_slots(cfg: "CalendarConfig", date: dt.date, *, now: dt.datetime | None = None,
                     client: httpx.AsyncClient | None = None) -> list[str]:
    """Verilen günde boş başlangıç saatleri ("HH:MM"); geçmiş saatler ve meşgul aralıklar düşer."""
    cands = _candidates(cfg, date)
    if not cands:
        return []
    busy = await busy_ranges(cfg, date, client=client)
    now = now or dt.datetime.now(dt.timezone.utc)
    step = dt.timedelta(minutes=cfg.duration_min)
    lead = now + dt.timedelta(minutes=cfg.min_notice_min)
    return [t.strftime("%H:%M") for t in cands
            if t >= lead and not any(b0 < t + step and t < b1 for b0, b1 in busy)]


async def insert_event(cfg: "CalendarConfig", *, date: dt.date, time: str, name: str, topic: str,
                       email: str | None, phone: str | None, note: str | None,
                       client: httpx.AsyncClient | None = None) -> str:
    """Etkinliği yazar; etkinlik bağlantısını (htmlLink) döndürür."""
    cid = calendar_id(cfg)
    start = dt.datetime.combine(date, dt.time.fromisoformat(time), tz(cfg))
    end = start + dt.timedelta(minutes=cfg.duration_min)
    lines = [f"Ad: {name}", f"E-posta: {email or '-'}", f"Telefon: {phone or '-'}", f"Konu: {topic}"]
    if note:
        lines.append(f"Not: {note}")
    lines.append("Kaynak: Sesli Asistan")
    body = {
        "summary": f"{topic} — {name}"[:200],
        "description": "\n".join(lines)[:4000],
        "start": {"dateTime": start.isoformat(), "timeZone": cfg.timezone},
        "end": {"dateTime": end.isoformat(), "timeZone": cfg.timezone},
    }
    data = await _request("POST", f"calendars/{cid}/events", json_body=body, client=client, timeout=cfg.timeout_s)
    return str(data.get("htmlLink") or data.get("id") or "")
