"""Supabase CRM bağlantısı (BOTCRm crm_* şeması: crm_leads, crm_tasks; supabase.turklawai.com).

Asistanın `type: supabase_crm` aracı çağrıldığında:
  1. Aday e-postaya (yoksa telefona) göre aranır; varsa durumu/etiketleri güncellenir,
     yoksa yeni aday eklenir (mükerrer kayıt açılmaz).
  2. İsteğe bağlı olarak `crm_tasks` tablosuna demo görevi eklenir.
  3. Oturum bitince (özet üretilirse) `notes_column` tanımlıysa özet adayın kaydına eklenir.

Supabase REST (PostgREST) doğrudan httpx ile çağrılır; ek bağımlılık yok.
Service role anahtarı yalnızca bu sunucuda kullanılır, loglanmaz, modele/istemciye gitmez.
Modül istisna fırlatmaz; her çağrı {"ok": bool, ...} döner.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from server.config import SupabaseCrmTool

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_SESSIONS = 5_000

# Oturum → (araç yapılandırması, aday id) — özetin hangi adaya ekleneceğini bilmek için.
_session_leads: "OrderedDict[str, tuple[SupabaseCrmTool, str]]" = OrderedDict()
# Oturum → arayanın normalize telefon numarası (telefon kanalı; model numarayı tekrar sormasın).
_session_callers: "OrderedDict[str, str]" = OrderedDict()


def _normalize_email(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip().lower().replace(" ", "")
    return v if _EMAIL_RE.match(v) else None


def _normalize_phone(v: str | None) -> str | None:
    """TR telefonlarını +90XXXXXXXXXX biçimine getirir; tanınmazsa rakamları döndürür."""
    if not v:
        return None
    digits = re.sub(r"\D", "", v)
    if not digits:
        return None
    if digits.startswith("0090"):
        digits = digits[4:]
    elif digits.startswith("90") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10 and digits.startswith("5"):
        return "+90" + digits
    return digits if len(digits) >= 7 else None


class CrmError(Exception):
    """İç hata; `code` modele/arayüze gidecek kısa koddur."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SupabaseCrm:
    def __init__(self, tool: "SupabaseCrmTool", *, client: httpx.AsyncClient | None = None):
        self.tool = tool
        self._client = client
        url = (os.environ.get(tool.url_env) or "").strip().rstrip("/")
        key = (os.environ.get(tool.key_env) or "").strip()
        if not url or not key:
            raise CrmError("crm_not_configured")
        if not url.startswith("https://") and not url.startswith("http://127.0.0.1") and not url.startswith("http://localhost"):
            raise CrmError("crm_not_configured")
        self.base = f"{url}/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       json_body: Any = None, prefer: str | None = None) -> Any:
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        own = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.tool.timeout_s, follow_redirects=False)
        try:
            resp = await client.request(method, f"{self.base}/{path}", params=params,
                                        json=json_body, headers=headers, timeout=self.tool.timeout_s)
        except httpx.TimeoutException as exc:
            raise CrmError("timeout") from exc
        except httpx.HTTPError as exc:
            raise CrmError("network_error") from exc
        finally:
            if own:
                await client.aclose()
        if resp.status_code >= 400:
            # Gövde loglanır ama kısaltılarak (anahtar içermez; PostgREST hata mesajı)
            logger.warning("Supabase %s %s → %s %s", method, path, resp.status_code, resp.text[:300])
            if resp.status_code in (401, 403):
                raise CrmError("crm_unauthorized")
            if resp.status_code == 404:
                raise CrmError("crm_table_missing")
            raise CrmError("crm_error")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise CrmError("crm_error") from exc

    async def find_lead(self, email: str | None, phone: str | None) -> dict | None:
        t = self.tool.leads_table
        for col, val in (("email", email), ("phone", phone)):
            if not val:
                continue
            rows = await self._request("GET", t, params={"select": "id,tags", col: f"eq.{val}", "limit": "1"})
            if rows:
                return rows[0]
        return None

    async def upsert_lead(self, *, name: str, email: str | None, phone: str | None,
                          note: str | None) -> tuple[str, bool]:
        """(aday_id, yeni_mi) döner."""
        t = self.tool.leads_table
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        existing = await self.find_lead(email, phone)
        if existing:
            tags = list(dict.fromkeys((existing.get("tags") or []) + list(self.tool.lead_tags)))
            patch: dict[str, Any] = {"status": self.tool.lead_status, "last_activity": now, "tags": tags}
            await self._request("PATCH", t, params={"id": f"eq.{existing['id']}"}, json_body=patch,
                                prefer="return=minimal")
            return str(existing["id"]), False
        row: dict[str, Any] = {
            "lead_name": name,
            "email": email,
            "phone": phone,
            "source": self.tool.lead_source,
            "status": self.tool.lead_status,
            "budget": 0,
            "tags": list(self.tool.lead_tags),
        }
        if note and self.tool.notes_column:
            row[self.tool.notes_column] = f"[Sesli asistan notu] {note}"
        data = await self._request("POST", t, json_body=[row], prefer="return=representation")
        if not data or not isinstance(data, list) or "id" not in data[0]:
            raise CrmError("crm_error")
        return str(data[0]["id"]), True

    async def create_task(self, *, lead_id: str, name: str, preferred_time: str) -> None:
        if not self.tool.tasks_table:
            return
        body = [{
            "title": f"Demo görüşmesi: {name} — tercih: {preferred_time}"[:200],
            "completed": False,
            # Tercih edilen zaman serbest metin ("cuma 10:00"); güvenilir tarih çıkarılamadığı için
            # görev bugüne tarihlenir, asıl zaman başlıkta durur.
            "due_date": dt.date.today().isoformat(),
            "assigned_to": self.tool.task_assigned_to,
            "lead_id": lead_id,
        }]
        await self._request("POST", self.tool.tasks_table, json_body=body, prefer="return=minimal")

    async def append_note(self, lead_id: str, text: str) -> None:
        col = self.tool.notes_column
        if not col:
            return
        rows = await self._request("GET", self.tool.leads_table,
                                   params={"select": col, "id": f"eq.{lead_id}", "limit": "1"})
        old = (rows[0].get(col) if rows else None) or ""
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        new = (old + "\n\n" if old else "") + f"[Sesli asistan görüşme özeti · {stamp}]\n{text}"
        await self._request("PATCH", self.tool.leads_table, params={"id": f"eq.{lead_id}"},
                            json_body={col: new[-20000:]}, prefer="return=minimal")


def set_session_caller(session_id: str, phone: str | None) -> None:
    """Telefon oturumunda arayanın numarasını hatırlar; `book()` args'ta phone yoksa bunu kullanır."""
    normalized = _normalize_phone(phone)
    if not normalized:
        return
    _session_callers[session_id] = normalized
    _session_callers.move_to_end(session_id)
    while len(_session_callers) > _MAX_SESSIONS:
        _session_callers.popitem(last=False)


def forget_session_caller(session_id: str) -> None:
    """Oturum bitince arayan numarasını bırakır."""
    _session_callers.pop(session_id, None)


def _remember(session_id: str, tool: "SupabaseCrmTool", lead_id: str) -> None:
    _session_leads[session_id] = (tool, lead_id)
    _session_leads.move_to_end(session_id)
    while len(_session_leads) > _MAX_SESSIONS:
        _session_leads.popitem(last=False)


async def book(tool: "SupabaseCrmTool", args: dict, *, session_id: str,
               client: httpx.AsyncClient | None = None) -> dict:
    """Aracın asıl işi. Dönüş: {"ok": True, "data": {...}} | {"ok": False, "error": kod}."""
    name = (args.get("name") or "").strip()
    email = _normalize_email(args.get("email"))
    # Telefon kanalında numara zaten biliniyor: model sormadıysa arayanın numarası kullanılır
    phone = _normalize_phone(args.get("phone")) or _session_callers.get(session_id)
    preferred_time = (args.get("preferred_time") or "").strip()
    note = (args.get("note") or "").strip() or None
    if not name or not preferred_time or not (email or phone):
        return {"ok": False, "error": "invalid_args"}
    try:
        crm = SupabaseCrm(tool, client=client)
        lead_id, is_new = await crm.upsert_lead(name=name, email=email, phone=phone, note=note)
        _remember(session_id, tool, lead_id)
        task_created = False
        try:
            await crm.create_task(lead_id=lead_id, name=name, preferred_time=preferred_time)
            task_created = bool(tool.tasks_table)
        except CrmError as exc:
            # Aday kaydedildi; görev açılamaması randevu talebini bozmaz
            logger.warning("CRM task creation failed: %s", exc.code)
        return {"ok": True, "data": {"lead": "created" if is_new else "updated", "task_created": task_created}}
    except CrmError as exc:
        return {"ok": False, "error": exc.code}
    except Exception:  # noqa: BLE001
        logger.exception("CRM booking failed")
        return {"ok": False, "error": "internal_error"}


async def attach_summary(session_id: str, text: str, *, client: httpx.AsyncClient | None = None) -> bool:
    """Oturum özeti hazır olunca çağrılır; bu oturumda CRM'e aday yazıldıysa özeti ekler."""
    item = _session_leads.pop(session_id, None)
    if not item or not text:
        return False
    tool, lead_id = item
    if not tool.notes_column:
        return False
    try:
        await SupabaseCrm(tool, client=client).append_note(lead_id, text)
        return True
    except CrmError as exc:
        logger.warning("CRM summary attach failed: %s", exc.code)
    except Exception:  # noqa: BLE001
        logger.exception("CRM summary attach failed")
    return False
