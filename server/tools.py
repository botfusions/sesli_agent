"""Webhook araçları: asistan YAML'ındaki `tools` listesinden ADK araçları üretir.

Akış:
    build_adk_tools(agent, on_event) → her WebhookTool için bir `WebhookAdkTool`
    (google.adk.tools.BaseTool alt sınıfı). Model aracı çağırınca `run_async`
    argümanları doğrular, `call_webhook` ile müşteri sunucusuna imzalı istek atar
    ve sonucu modele güvenli (iç ayrıntı içermeyen) bir sözlük olarak döndürür.

Doğrulanan ADK API'si (google-adk 2.10.0, google-genai 2.25.0):
    - BaseTool.__init__(*, name, description, is_long_running=False,
      custom_metadata=None, behavior=None, response_scheduling=None)
    - _get_declaration() -> types.FunctionDeclaration | None
    - async run_async(*, args, tool_context)
    - Canlı modda `behavior=types.Behavior.NON_BLOCKING` olan araçlar
      `handle_function_calls_live` içinde arka planda çalıştırılır; ADK
      bildirimdeki `behavior` alanını yalnızca canlı istekte kendisi işaretler
      (`mark_live_async_tools_non_blocking`). Sonuç `FunctionResponse.scheduling`
      = `response_scheduling` ile modele geri beslenir.

İmza şeması (müşteri tarafı doğrulaması için bkz. examples/webhook_receiver.py):
    X-Botfusions-Timestamp: <unix saniye>
    X-Botfusions-Signature: sha256=<hex HMAC-SHA256(secret, f"{ts}.".encode() + body)>
    GET isteklerinde `body` yerine ham sorgu dizesi (query string, bayt) imzalanır.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx
from google.adk.tools import BaseTool
from google.genai import types

if TYPE_CHECKING:  # Döngüsel/erken import'u önlemek için yalnızca tip denetiminde
    from google.adk.tools.tool_context import ToolContext

    from server.config import AgentConfig, SupabaseCrmTool, WebhookTool

logger = logging.getLogger(__name__)

ToolEventCallback = Callable[[dict], Awaitable[None]]

# --- Sabitler -----------------------------------------------------------------

MAX_RESPONSE_BYTES = 64 * 1024          # yanıt gövdesi üst sınırı
MAX_CALLS_PER_SESSION_TOOL = 20         # oturum + araç başına çağrı sınırı
SIGNATURE_TOLERANCE_S = 300             # imza zaman damgası en fazla 5 dk eski olabilir
SIGNATURE_HEADER = "X-Botfusions-Signature"
TIMESTAMP_HEADER = "X-Botfusions-Timestamp"
USER_AGENT = "Botfusions-Voice-Agent/1.0"
_MAX_TRACKED_KEYS = 10_000              # sayaç sözlüğü sınırsız büyümesin (LRU)

# Modele ve arayüze giden kısa Türkçe açıklamalar. İç ayrıntı (URL, istisna
# metni, HTTP gövdesi) bilinçli olarak burada yer almaz.
_ERROR_MESSAGES: dict[str, str] = {
    "timeout": "Sistem zamanında yanıt vermedi.",
    "network_error": "Sisteme şu anda ulaşılamıyor.",
    "invalid_json": "Sistemden anlaşılır bir yanıt alınamadı.",
    "response_too_large": "Sistemden gelen yanıt çok büyük.",
    "missing_secret": "Araç yapılandırması eksik.",
    "rate_limited": "Bu işlem bu görüşmede çok fazla denendi.",
    "invalid_args": "İşlem için gerekli bilgiler eksik ya da hatalı.",
    "internal_error": "Beklenmeyen bir hata oluştu.",
    # Supabase CRM aracı
    "crm_not_configured": "Kayıt sistemi henüz yapılandırılmamış.",
    "crm_unauthorized": "Kayıt sistemine erişim izni yok.",
    "crm_table_missing": "Kayıt sisteminde gerekli tablo bulunamadı.",
    "crm_error": "Kayıt sistemi talebi kaydedemedi.",
    # Google Takvim
    "calendar_not_configured": "Takvim henüz yapılandırılmamış.",
    "calendar_unauthorized": "Takvime erişim izni yok.",
    "calendar_not_found": "Takvim bulunamadı.",
    "calendar_error": "Takvim isteği tamamlanamadı.",
    "slot_busy": "Bu saat uygun değil; boş saatlerden birini önerin.",
}
_HTTP_ERROR_MESSAGE = "Sistem isteği kabul etmedi."
_OK_SUMMARY = "İşlem tamamlandı."
_START_SUMMARY = "İşlem yapılıyor…"


def error_message(code: str) -> str:
    """Hata koduna karşılık gelen kısa Türkçe açıklamayı döndürür."""
    if code.startswith("http_"):
        return _HTTP_ERROR_MESSAGE
    return _ERROR_MESSAGES.get(code, _ERROR_MESSAGES["internal_error"])


# --- Çağrı sayacı (oturum + araç başına) --------------------------------------

_call_counts: "OrderedDict[tuple[str, str, str], int]" = OrderedDict()


def _take_call_slot(agent_id: str, session_id: str, tool_name: str) -> bool:
    """Çağrı hakkı varsa sayacı artırıp True döner; limit dolduysa False."""
    key = (agent_id, session_id, tool_name)
    count = _call_counts.get(key, 0)
    if count >= MAX_CALLS_PER_SESSION_TOOL:
        _call_counts.move_to_end(key)
        return False
    _call_counts[key] = count + 1
    _call_counts.move_to_end(key)
    while len(_call_counts) > _MAX_TRACKED_KEYS:
        _call_counts.popitem(last=False)
    return True


def reset_call_counts(session_id: str | None = None) -> None:
    """Oturum bitince sayaçları temizler (session_id=None → hepsi; testler için)."""
    if session_id is None:
        _call_counts.clear()
        return
    for key in [k for k in _call_counts if k[1] == session_id]:
        del _call_counts[key]


# --- Argüman doğrulama --------------------------------------------------------


class ArgumentError(ValueError):
    """Model geçersiz argüman gönderdiğinde fırlatılır (mesaj İngilizce, log içindir)."""


def validate_args(tool: "WebhookTool", args: dict | None) -> dict:
    """Argümanları ToolParam şemasına göre doğrular ve temizlenmiş kopyasını döndürür.

    - Zorunlu parametre eksikse, tip uymuyorsa veya enum dışıysa ArgumentError.
    - Şemada olmayan fazladan argümanlar sessizce atılır (müşteriye gitmez).
    - `integer` için 3.0 gibi tam sayı değerli float'lar int'e çevrilir.
    """
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ArgumentError("args must be an object")

    clean: dict[str, Any] = {}
    for pname, param in (tool.parameters or {}).items():
        if pname not in args or args[pname] is None:
            if param.required:
                raise ArgumentError(f"missing required argument: {pname}")
            continue
        value = args[pname]
        ptype = param.type
        if ptype == "string":
            if not isinstance(value, str):
                raise ArgumentError(f"{pname}: expected string")
        elif ptype == "boolean":
            if not isinstance(value, bool):
                raise ArgumentError(f"{pname}: expected boolean")
        elif ptype == "integer":
            if isinstance(value, bool):
                raise ArgumentError(f"{pname}: expected integer")
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if not isinstance(value, int):
                raise ArgumentError(f"{pname}: expected integer")
        elif ptype == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ArgumentError(f"{pname}: expected number")
        else:  # şema dışı tip; config doğrulaması bunu zaten engellemeli
            raise ArgumentError(f"{pname}: unsupported type {ptype}")
        if param.enum is not None and str(value) not in param.enum:
            raise ArgumentError(f"{pname}: value not in enum")
        clean[pname] = value

    extra = set(args) - set(tool.parameters or {})
    if extra:
        logger.info("tool %s: dropping unknown args %s", tool.name, sorted(extra))
    return clean


# --- İmza ---------------------------------------------------------------------


def sign(body: bytes, secret: str, timestamp: str) -> str:
    """`sha256=<hex>` biçiminde imza üretir: HMAC(secret, f"{timestamp}.".encode() + body)."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify_signature(
    body: bytes,
    header: str,
    secret: str,
    timestamp: str | None = None,
    *,
    now: float | None = None,
    tolerance_s: int = SIGNATURE_TOLERANCE_S,
) -> bool:
    """Gelen isteğin imzasını doğrular (müşteri tarafı için referans).

    `header` X-Botfusions-Signature değeri, `timestamp` X-Botfusions-Timestamp
    değeridir. Zaman damgası yoksa, sayı değilse ya da `tolerance_s`'den daha
    eski/ileri ise False döner. Karşılaştırma hmac.compare_digest ile yapılır.
    """
    if not header or not secret or not timestamp:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > tolerance_s:
        return False
    expected = sign(body, secret, str(ts))
    return hmac.compare_digest(expected.encode(), header.strip().encode())


# --- Webhook çağrısı ----------------------------------------------------------


def _error(code: str) -> dict:
    return {"ok": False, "error": code}


async def call_webhook(
    tool: "WebhookTool",
    args: dict,
    *,
    session_id: str,
    agent_id: str,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Müşteri webhook'unu çağırır. İstisna fırlatmaz; her zaman sözlük döner.

    Dönüş: {"ok": True, "data": <json>} veya {"ok": False, "error": <kod>}.
    Kodlar: invalid_args, rate_limited, missing_secret, timeout, network_error,
    http_<kod>, response_too_large, invalid_json, internal_error.
    `client` yalnızca testlerde (httpx.MockTransport) ya da paylaşımlı bağlantı
    havuzu için verilir; verilmezse çağrı başına yeni istemci açılır.
    """
    try:
        clean_args = validate_args(tool, args)
    except ArgumentError as exc:
        logger.info("tool %s invalid args: %s", tool.name, exc)
        return _error("invalid_args")

    # Gizli anahtar, ağ isteği ve sayaçtan önce kontrol edilir.
    secret: str | None = None
    if tool.secret_env:
        secret = os.environ.get(tool.secret_env)
        if not secret:
            logger.error("tool %s: secret env %s is not set", tool.name, tool.secret_env)
            return _error("missing_secret")

    if not _take_call_slot(agent_id, session_id, tool.name):
        logger.warning("tool %s rate limited for session %s", tool.name, session_id)
        return _error("rate_limited")

    ts = str(int(time.time()))
    method = (tool.method or "POST").upper()
    url = str(tool.url)
    headers = {TIMESTAMP_HEADER: ts, "User-Agent": USER_AGENT, "Accept": "application/json"}

    if method == "GET":
        # GET: argümanlar sorgu dizesinde; imza ham sorgu dizesi üzerinden.
        query = urlencode(
            {**{k: _query_value(v) for k, v in clean_args.items()},
             "tool": tool.name, "session_id": session_id, "agent_id": agent_id, "ts": ts}
        )
        signed = query.encode()
        url = url + ("&" if "?" in url else "?") + query
        content: bytes | None = None
    else:
        payload = {"tool": tool.name, "args": clean_args, "session_id": session_id,
                   "agent_id": agent_id, "ts": int(ts)}
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        signed = content
        headers["Content-Type"] = "application/json"

    if secret:
        headers[SIGNATURE_HEADER] = sign(signed, secret, ts)

    timeout = float(tool.timeout_s)
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=False)
    try:
        # asyncio.wait_for: httpx zaman aşımı işlem başınadır; toplam süreyi de sınırla.
        return await asyncio.wait_for(
            _send(http, method, url, headers, content, timeout, tool.name), timeout=timeout
        )
    except (asyncio.TimeoutError, httpx.TimeoutException):
        logger.warning("tool %s timed out after %.1fs", tool.name, timeout)
        return _error("timeout")
    except httpx.HTTPError as exc:
        logger.warning("tool %s network error: %s", tool.name, type(exc).__name__)
        return _error("network_error")
    except Exception:  # noqa: BLE001 — araç çağrısı oturumu asla düşürmemeli
        logger.exception("tool %s unexpected error", tool.name)
        return _error("internal_error")
    finally:
        if own_client:
            await http.aclose()


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


async def _send(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict,
    content: bytes | None,
    timeout: float,
    tool_name: str,
) -> dict:
    """İsteği gönderir, gövdeyi en fazla 64 KB okuyarak JSON'a çevirir."""
    async with http.stream(method, url, headers=headers, content=content,
                           timeout=httpx.Timeout(timeout)) as resp:
        if resp.status_code >= 400:
            logger.warning("tool %s returned HTTP %s", tool_name, resp.status_code)
            return _error(f"http_{resp.status_code}")
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
            return _error("response_too_large")
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > MAX_RESPONSE_BYTES:
                logger.warning("tool %s response exceeded %d bytes", tool_name, MAX_RESPONSE_BYTES)
                return _error("response_too_large")

    if not buf.strip():
        return {"ok": True, "data": None}  # 204 / boş gövde: başarılı, veri yok
    try:
        data = json.loads(bytes(buf).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("tool %s returned non-JSON body", tool_name)
        return _error("invalid_json")
    return {"ok": True, "data": data}


# --- ADK aracı ----------------------------------------------------------------

_TYPE_MAP = {
    "string": types.Type.STRING,
    "number": types.Type.NUMBER,
    "integer": types.Type.INTEGER,
    "boolean": types.Type.BOOLEAN,
}


def build_declaration(tool: "WebhookTool") -> types.FunctionDeclaration:
    """WebhookTool → genai FunctionDeclaration (ToolParam → Schema)."""
    params = tool.parameters or {}
    parameters: types.Schema | None = None
    if params:
        properties: dict[str, types.Schema] = {}
        required: list[str] = []
        for pname, p in params.items():
            schema = types.Schema(type=_TYPE_MAP[p.type], description=p.description or None)
            if p.enum:
                if p.type == "string":
                    schema.enum = list(p.enum)
                else:
                    # Gemini enum'u yalnız string için destekler; açıklamaya ekle.
                    extra = "İzin verilen değerler: " + ", ".join(p.enum)
                    schema.description = f"{schema.description} ({extra})" if schema.description else extra
            properties[pname] = schema
            if p.required:
                required.append(pname)
        parameters = types.Schema(type=types.Type.OBJECT, properties=properties,
                                  required=required or None)
    return types.FunctionDeclaration(name=tool.name, description=tool.description,
                                     parameters=parameters)


class WebhookAdkTool(BaseTool):
    """Bir WebhookTool'u ADK'ya tanıtan dinamik araç."""

    def __init__(
        self,
        webhook: "WebhookTool",
        *,
        agent_id: str,
        on_event: ToolEventCallback,
        session_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        non_blocking = bool(getattr(webhook, "speak_while_running", True))
        super().__init__(
            name=webhook.name,
            description=webhook.description,
            # Canlı modda araç arka planda çalışır, asistan konuşmaya devam eder;
            # sonuç asistan sustuğunda (WHEN_IDLE) modele iletilir.
            behavior=types.Behavior.NON_BLOCKING if non_blocking else None,
            response_scheduling=types.FunctionResponseScheduling.WHEN_IDLE if non_blocking else None,
        )
        self.webhook = webhook
        self.agent_id = agent_id
        self.session_id = session_id
        self._on_event = on_event
        self._client = client

    def _get_declaration(self) -> types.FunctionDeclaration:
        return build_declaration(self.webhook)

    def _resolve_session_id(self, tool_context: "ToolContext | None") -> str:
        if self.session_id:
            return self.session_id
        try:
            return str(tool_context.session.id)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return "unknown"

    async def _emit(self, status: str, summary: str | None = None) -> None:
        event: dict[str, Any] = {"type": "tool", "name": self.name, "status": status}
        if summary:
            event["summary"] = summary
        try:
            await self._on_event(event)
        except Exception:  # noqa: BLE001 — UI bildirimi aracı bozmamalı
            logger.exception("tool %s: on_event callback failed", self.name)

    async def run_async(self, *, args: dict[str, Any], tool_context: "ToolContext") -> dict:
        session_id = self._resolve_session_id(tool_context)
        await self._emit("start", _START_SUMMARY)
        result = await call_webhook(self.webhook, args, session_id=session_id,
                                    agent_id=self.agent_id, client=self._client)
        if result.get("ok"):
            await self._emit("ok", _OK_SUMMARY)
            return {"ok": True, "data": result.get("data")}
        code = str(result.get("error", "internal_error"))
        message = error_message(code)
        await self._emit("error", message)
        # Modele yalnızca kod + Türkçe açıklama gider; URL/istisna ayrıntısı gitmez.
        return {"ok": False, "error": code, "message": message}


class SupabaseCrmAdkTool(WebhookAdkTool):
    """`type: supabase_crm` aracı: adayı ve görevi BOTCRm'in Supabase tablolarına yazar."""

    async def run_async(self, *, args: dict[str, Any], tool_context: "ToolContext") -> dict:
        from server import crm_supabase  # geç import: webhook-only kurulumlarda gerekmesin

        session_id = self._resolve_session_id(tool_context)
        await self._emit("start", "Kaydınız oluşturuluyor…")
        try:
            clean_args = validate_args(self.webhook, args)
        except ArgumentError as exc:
            logger.info("tool %s: invalid args: %s", self.name, exc)
            result: dict = {"ok": False, "error": "invalid_args"}
        else:
            if not _take_call_slot(self.agent_id, session_id, self.name):
                result = {"ok": False, "error": "rate_limited"}
            else:
                result = await crm_supabase.book(self.webhook, clean_args, session_id=session_id,
                                                 client=self._client)
        if result.get("ok"):
            await self._emit("ok", "Kaydınız alındı.")
            return {"ok": True, "data": result.get("data")}
        code = str(result.get("error", "internal_error"))
        message = error_message(code)
        await self._emit("error", message)
        out = {"ok": False, "error": code, "message": message}
        if "free_slots" in result:
            out["free_slots"] = result["free_slots"]
        return out


class AvailabilityAdkTool(BaseTool):
    """`check_availability`: Google Takvim'de verilen günün boş saatlerini döndürür (yalnız okur)."""

    def __init__(self, crm_tool: "SupabaseCrmTool", *, agent_id: str, on_event: ToolEventCallback,
                 session_id: str | None = None, client: httpx.AsyncClient | None = None):
        super().__init__(
            name="check_availability",
            description=("Verilen gün için demo görüşmesine uygun boş saatleri döndürür. Kullanıcı gün söyleyince, "
                         "book_demo çağırmadan ÖNCE çağır ve boş saatlerden seçtir."),
        )
        self.calendar = crm_tool.calendar
        self.agent_id = agent_id
        self._on_event = on_event
        self._session_id = session_id
        self._client = client

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name, description=self.description,
            parameters=types.Schema(type=types.Type.OBJECT, required=["date"], properties={
                "date": types.Schema(type=types.Type.STRING, description="Gün, YYYY-MM-DD (ör. 2026-10-09)")}))

    async def run_async(self, *, args: dict[str, Any], tool_context: "ToolContext") -> dict:
        import datetime as dt

        from server import gcal

        session_id = self._session_id or (tool_context.session.id if tool_context else "unknown")
        try:
            date = dt.date.fromisoformat(str((args or {}).get("date", "")).strip())
        except ValueError:
            return {"ok": False, "error": "invalid_args", "message": error_message("invalid_args")}
        if not _take_call_slot(self.agent_id, session_id, self.name):
            return {"ok": False, "error": "rate_limited", "message": error_message("rate_limited")}
        try:
            slots = await gcal.free_slots(self.calendar, date, client=self._client)
        except gcal.CalendarError as exc:
            return {"ok": False, "error": exc.code, "message": error_message(exc.code)}
        return {"ok": True, "data": {"date": date.isoformat(), "free_slots": slots,
                                     "duration_min": self.calendar.duration_min}}


def build_adk_tools(
    agent: "AgentConfig",
    on_event: ToolEventCallback,
    *,
    session_id: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[BaseTool]:
    """Asistanın webhook araçlarından ADK araç listesi üretir.

    `session_id` verilirse webhook'a o gönderilir (storage oturum kimliği);
    verilmezse ADK oturum kimliği (`tool_context.session.id`) kullanılır.
    """
    out: list[BaseTool] = []
    for t in agent.tools or []:
        cls = SupabaseCrmAdkTool if getattr(t, "type", "webhook") == "supabase_crm" else WebhookAdkTool
        out.append(cls(t, agent_id=agent.id, on_event=on_event, session_id=session_id, client=client))
        if getattr(t, "calendar", None):
            out.append(AvailabilityAdkTool(t, agent_id=agent.id, on_event=on_event, session_id=session_id, client=client))
    return out
