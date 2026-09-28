"""/admin yönetim paneli: oturum listesi, detay ve JSON API.

- ADMIN_TOKEN tanımlı değilse tüm /admin rotaları 404 döner.
- Kimlik: `Authorization: Bearer <token>`, `?token=<token>` (HTML sayfalarda çereze çevrilip
  token'sız URL'e yönlendirilir) veya HttpOnly + SameSite=Strict çerez.
- Store'a `request.app.state.store` üzerinden erişilir.
- Sunucu tarafı HTML (f-string + html.escape), Jinja yok.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

__all__ = ["router", "COST_PER_1M"]

# Tahmini maliyet için 1 milyon token başına USD fiyatı.
# DİKKAT: Bu değerler doğrulanmadı, kendi fiyatınızla (model, ses/metin ayrımı, bölge) güncelleyin.
COST_PER_1M = {"input": 3.00, "output": 12.00}

COOKIE_NAME = "bf_admin"
COOKIE_MAX_AGE = 12 * 60 * 60  # 12 saat
PAGE_SIZE = 25
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")

router = APIRouter(prefix="/admin")

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}


# ---- ayar / kimlik -----------------------------------------------------------


def _configured_token() -> str:
    """ADMIN_TOKEN değerini server.settings'ten (Ajan A), yoksa ortamdan okur."""
    try:
        from server.settings import get_settings  # type: ignore

        s = get_settings()
        for attr in ("admin_token", "ADMIN_TOKEN"):
            val = getattr(s, attr, None)
            if val:
                return str(val)
    except Exception:  # settings henüz yok veya hatalı → ortam değişkeni
        pass
    return os.environ.get("ADMIN_TOKEN", "") or ""


def _cookie_value(token: str) -> str:
    """Çerezde ham token yerine ondan türetilmiş HMAC tutulur."""
    return hmac.new(token.encode(), b"botfusions-admin-cookie-v1", hashlib.sha256).hexdigest()


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class _Auth:
    """Kimlik doğrulama sonucu: ok ya da hazır yanıt."""

    def __init__(self, ok: bool, response: Response | None = None):
        self.ok = ok
        self.response = response


def _check_auth(request: Request, *, html_page: bool) -> _Auth:
    token = _configured_token()
    if not token:
        return _Auth(False, Response(status_code=404))

    # 1) Bearer başlığı
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        if _eq(auth[7:].strip(), token):
            return _Auth(True)
        return _Auth(False, _unauthorized(html_page))

    # 2) ?token= sorgu parametresi
    q_token = request.query_params.get("token")
    if q_token is not None:
        if not _eq(q_token, token):
            return _Auth(False, _unauthorized(html_page))
        if not html_page:
            return _Auth(True)
        # Çerez ver ve token'ı adres çubuğundan temizle
        params = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
        target = request.url.path + (("?" + urlencode(params)) if params else "")
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie(
            COOKIE_NAME,
            _cookie_value(token),
            max_age=COOKIE_MAX_AGE,
            path="/admin",
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        for k, v in _SECURITY_HEADERS.items():
            resp.headers[k] = v
        return _Auth(False, resp)

    # 3) Çerez
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie and _eq(cookie, _cookie_value(token)):
        return _Auth(True)
    return _Auth(False, _unauthorized(html_page))


def _unauthorized(html_page: bool) -> Response:
    if not html_page:
        return JSONResponse({"error": "unauthorized"}, status_code=401, headers=_SECURITY_HEADERS)
    body = f"""
<section class="card login">
  <h1>Yönetim paneli</h1>
  <p class="muted">Devam etmek için yönetici anahtarını girin.</p>
  <form method="get" action="/admin">
    <label for="token">Yönetici anahtarı</label>
    <input id="token" name="token" type="password" autocomplete="current-password" required>
    <button type="submit">Giriş</button>
  </form>
</section>"""
    return _page("Giriş gerekli", body, status_code=401, headers={"WWW-Authenticate": "Bearer"})


def _store(request: Request):
    return getattr(request.app.state, "store", None)


# ---- biçimlendirme -----------------------------------------------------------


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _fmt_ts(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OSError):
        return "—"


def _fmt_time(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def _fmt_duration(sec: Any) -> str:
    if sec is None:
        return "sürüyor"
    try:
        total = int(round(float(sec)))
    except (TypeError, ValueError):
        return "—"
    m, s = divmod(total, 60)
    return f"{m} dk {s:02d} sn" if m else f"{s} sn"


def estimate_cost(input_tokens: Any, output_tokens: Any) -> float:
    """COST_PER_1M'e göre tahmini USD maliyet."""
    try:
        i = float(input_tokens or 0)
        o = float(output_tokens or 0)
    except (TypeError, ValueError):
        return 0.0
    return (i * COST_PER_1M["input"] + o * COST_PER_1M["output"]) / 1_000_000


_REASON_LABELS = {
    "client_end": "Kullanıcı kapattı",
    "disconnect": "Bağlantı koptu",
    "session_time": "Süre limiti",
    "daily_sessions": "Günlük oturum limiti",
    "daily_minutes": "Günlük dakika limiti",
    "error": "Hata",
    "model_closed": "Model kapattı",
    "unknown": "Bilinmiyor",
}


def _fmt_reason(reason: Any) -> str:
    if not reason:
        return "—"
    return _REASON_LABELS.get(str(reason), str(reason))


_CSS = """
:root{--bg:#0E0B15;--panel:#171222;--line:#2A2238;--accent:#A855F7;--text:#EDE8DF;--muted:#A79FB3}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);
 font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
a{color:var(--accent)}
header.top{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line)}
header.top .brand{font-weight:700;letter-spacing:.2px;color:var(--text);text-decoration:none}
header.top .brand span{color:var(--accent)}
main{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:1.4rem;margin:0 0 12px}
h2{font-size:1.1rem;margin:24px 0 10px}
.muted{color:var(--muted)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
.login{max-width:420px;margin:40px auto}
form.inline{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 14px}
label{color:var(--muted);font-size:.9rem}
input,select,button{font:inherit;color:var(--text);background:#0E0B15;border:1px solid var(--line);
 border-radius:8px;padding:8px 10px}
.login input{width:100%;margin:6px 0 12px}
button{background:var(--accent);border-color:var(--accent);color:#fff;cursor:pointer;font-weight:600}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px}
table{width:100%;border-collapse:collapse;font-size:.95rem}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:600;background:var(--panel)}
tr:last-child td{border-bottom:0}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.pager{display:flex;justify-content:space-between;align-items:center;margin-top:14px;gap:8px}
.summary{white-space:pre-wrap}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:0 0 16px}
.meta div{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.meta b{display:block;color:var(--muted);font-weight:500;font-size:.8rem}
.chat{display:flex;flex-direction:column;gap:10px}
.bubble{max-width:80%;padding:10px 14px;border-radius:14px;white-space:pre-wrap;word-wrap:break-word}
.bubble.user{align-self:flex-end;background:var(--accent);color:#fff;border-bottom-right-radius:4px}
.bubble.agent{align-self:flex-start;background:var(--panel);border:1px solid var(--line);border-bottom-left-radius:4px}
.bubble small{display:block;opacity:.7;font-size:.75rem;margin-bottom:2px}
pre{margin:0;white-space:pre-wrap;word-break:break-word;font-size:.85rem}
.events td{white-space:normal;vertical-align:top}
@media (max-width:640px){
 html,body{font-size:17px}
 main{padding:14px 12px 40px}
 .bubble{max-width:92%}
 th,td{padding:9px 10px}
}
"""


def _page(title: str, body: str, *, status_code: int = 200, headers: dict | None = None) -> HTMLResponse:
    doc = f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{_e(title)} · Botfusions Voice Agent</title>
<style>{_CSS}</style></head>
<body>
<header class="top"><a class="brand" href="/admin">Botfusions <span>Voice Agent</span></a>
<span class="muted">Yönetim</span></header>
<main>{body}</main>
</body></html>"""
    h = dict(_SECURITY_HEADERS)
    if headers:
        h.update(headers)
    return HTMLResponse(doc, status_code=status_code, headers=h)


def _unavailable(html_page: bool) -> Response:
    if not html_page:
        return JSONResponse({"error": "store_unavailable"}, status_code=503, headers=_SECURITY_HEADERS)
    return _page("Kullanılamıyor", '<p class="card">Kayıt veritabanı hazır değil.</p>', status_code=503)


def _int_param(request: Request, name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


# ---- rotalar -----------------------------------------------------------------
# Store çağrıları senkron olduğundan rotalar `def`; FastAPI bunları thread havuzunda çalıştırır.


@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def admin_list(request: Request) -> Response:
    auth = _check_auth(request, html_page=True)
    if not auth.ok:
        return auth.response
    store = _store(request)
    if store is None:
        return _unavailable(True)

    agent = (request.query_params.get("agent") or "").strip() or None
    page = _int_param(request, "page", 1, 1, 100_000)
    offset = (page - 1) * PAGE_SIZE
    rows = store.list_sessions(agent_id=agent, limit=PAGE_SIZE, offset=offset)
    total = store.count_sessions(agent) if hasattr(store, "count_sessions") else None
    agents = store.list_agent_ids() if hasattr(store, "list_agent_ids") else []

    options = ['<option value="">Tümü</option>']
    for a in agents:
        sel = " selected" if a == agent else ""
        options.append(f'<option value="{_e(a)}"{sel}>{_e(a)}</option>')

    trs = []
    for r in rows:
        cost = estimate_cost(r.get("input_tokens"), r.get("output_tokens"))
        href = "/admin/sessions/" + _e(r.get("id"))
        trs.append(
            "<tr>"
            f'<td><a href="{href}">{_e(_fmt_ts(r.get("started_at")))}</a></td>'
            f"<td>{_e(r.get('agent_id'))}</td>"
            f"<td>{_e(_fmt_duration(r.get('duration_s')))}</td>"
            f"<td>{_e(_fmt_reason(r.get('end_reason')))}</td>"
            f'<td class="num">{int(r.get("input_tokens") or 0):,} / {int(r.get("output_tokens") or 0):,}</td>'
            f'<td class="num">${cost:.4f}</td>'
            "</tr>"
        )
    if not trs:
        trs.append('<tr><td colspan="6" class="muted">Henüz oturum yok.</td></tr>')

    def _link(p: int) -> str:
        q: dict[str, Any] = {"page": p}
        if agent:
            q["agent"] = agent
        return "/admin?" + _e(urlencode(q))

    has_next = (offset + len(rows) < total) if total is not None else len(rows) == PAGE_SIZE
    prev_html = f'<a href="{_link(page - 1)}">&larr; Önceki</a>' if page > 1 else "<span></span>"
    next_html = f'<a href="{_link(page + 1)}">Sonraki &rarr;</a>' if has_next else "<span></span>"
    total_html = f"Toplam {total} oturum · Sayfa {page}" if total is not None else f"Sayfa {page}"

    body = f"""
<h1>Oturumlar</h1>
<form class="inline" method="get" action="/admin">
  <label for="agent">Asistan</label>
  <select id="agent" name="agent">{''.join(options)}</select>
  <button type="submit">Filtrele</button>
</form>
<div class="table-wrap"><table>
<thead><tr><th>Tarih</th><th>Asistan</th><th>Süre</th><th>Bitiş nedeni</th>
<th>Token (girdi / çıktı)</th><th>Tahmini maliyet</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table></div>
<div class="pager">{prev_html}<span class="muted">{_e(total_html)}</span>{next_html}</div>
<p class="muted">Maliyet tahminidir (doğrulanmamış birim fiyatlar, USD).</p>"""
    return _page("Oturumlar", body)


@router.get("/sessions/{session_id}", response_class=HTMLResponse, include_in_schema=False)
def admin_detail(request: Request, session_id: str) -> Response:
    auth = _check_auth(request, html_page=True)
    if not auth.ok:
        return auth.response
    store = _store(request)
    if store is None:
        return _unavailable(True)
    s = store.get_session(session_id) if _SESSION_ID_RE.match(session_id or "") else None
    if s is None:
        return _page("Bulunamadı", '<p class="card">Oturum bulunamadı. <a href="/admin">Listeye dön</a></p>',
                     status_code=404)

    cost = estimate_cost(s.get("input_tokens"), s.get("output_tokens"))
    summary = s.get("summary") or ""
    summary_html = (f'<div class="card summary">{_e(summary)}</div>' if summary
                    else '<p class="muted">Özet henüz yok.</p>')

    bubbles = []
    for t in s.get("transcript") or []:
        role = "user" if t.get("role") == "user" else "agent"
        label = "Müşteri" if role == "user" else "Asistan"
        bubbles.append(
            f'<div class="bubble {role}"><small>{label} · {_e(_fmt_time(t.get("ts")))}</small>'
            f"{_e(t.get('text'))}</div>"
        )
    chat_html = ('<div class="chat">' + "".join(bubbles) + "</div>") if bubbles \
        else '<p class="muted">Transkript yok.</p>'

    ev_rows = []
    for ev in s.get("events") or []:
        payload = json.dumps(ev.get("payload"), ensure_ascii=False, indent=2)
        ev_rows.append(
            f"<tr><td>{_e(_fmt_time(ev.get('ts')))}</td><td>{_e(ev.get('kind'))}</td>"
            f"<td><pre>{_e(payload)}</pre></td></tr>"
        )
    events_html = (
        '<div class="table-wrap events"><table><thead><tr><th>Saat</th><th>Tür</th><th>Ayrıntı</th></tr>'
        "</thead><tbody>" + "".join(ev_rows) + "</tbody></table></div>"
    ) if ev_rows else '<p class="muted">Olay yok.</p>'

    body = f"""
<p><a href="/admin">&larr; Oturumlar</a></p>
<h1>Oturum {_e(s.get('id'))}</h1>
<div class="meta">
  <div><b>Asistan</b>{_e(s.get('agent_id'))}</div>
  <div><b>Başlangıç</b>{_e(_fmt_ts(s.get('started_at')))}</div>
  <div><b>Süre</b>{_e(_fmt_duration(s.get('duration_s')))}</div>
  <div><b>Bitiş nedeni</b>{_e(_fmt_reason(s.get('end_reason')))}</div>
  <div><b>Kaynak (origin)</b>{_e(s.get('origin') or '—')}</div>
  <div><b>Token (girdi / çıktı)</b>{int(s.get('input_tokens') or 0):,} / {int(s.get('output_tokens') or 0):,}</div>
  <div><b>Ses (girdi / çıktı)</b>{float(s.get('audio_in_s') or 0):.1f} sn / {float(s.get('audio_out_s') or 0):.1f} sn</div>
  <div><b>Tahmini maliyet</b>${cost:.4f}</div>
</div>
<h2>Özet</h2>
{summary_html}
<h2>Transkript</h2>
{chat_html}
<h2>Olaylar</h2>
{events_html}"""
    return _page("Oturum ayrıntısı", body)


@router.get("/api/sessions")
def admin_api_sessions(request: Request) -> Response:
    auth = _check_auth(request, html_page=False)
    if not auth.ok:
        return auth.response
    store = _store(request)
    if store is None:
        return _unavailable(False)
    agent = (request.query_params.get("agent") or "").strip() or None
    limit = _int_param(request, "limit", 50, 1, 200)
    offset = _int_param(request, "offset", 0, 0, 10_000_000)
    rows = store.list_sessions(agent_id=agent, limit=limit, offset=offset)
    for r in rows:
        r["estimated_cost_usd"] = round(estimate_cost(r.get("input_tokens"), r.get("output_tokens")), 6)
    total = store.count_sessions(agent) if hasattr(store, "count_sessions") else None
    return JSONResponse(
        {"items": rows, "total": total, "limit": limit, "offset": offset},
        headers=_SECURITY_HEADERS,
    )
