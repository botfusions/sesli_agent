"""admin.py testleri: küçük bir FastAPI uygulamasına router eklenerek."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import admin as admin_mod
from server.storage import Store

TOKEN = "s3cret-token-xyz"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "admin.db")
    yield s
    s.close()


@pytest.fixture
def make_client(store, monkeypatch):
    def _make(token=TOKEN):
        monkeypatch.setattr(admin_mod, "_configured_token", lambda: token)
        app = FastAPI()
        app.state.store = store
        app.include_router(admin_mod.router)
        return TestClient(app, follow_redirects=False)

    return _make


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def _seed(store):
    sid = store.create_session("botfusions-satis", "https://ornek.com")
    store.add_transcript(sid, "user", "<script>alert(1)</script> merhaba")
    store.add_transcript(sid, "agent", "Size nasıl yardımcı olabilirim?")
    store.add_event(sid, "tool", {"name": "create_lead", "summary": "<img src=x onerror=alert(2)>"})
    store.end_session(sid, "client_end", {"input_tokens": 1_000_000, "output_tokens": 0})
    store.set_summary(sid, "- Konu: <b>fiyat</b>")
    other = store.create_session("destek", None)
    return sid, other


def test_no_token_configured_404(make_client):
    c = make_client(token="")
    for path in ("/admin", "/admin/", "/admin/api/sessions", "/admin/sessions/" + "a" * 32):
        assert c.get(path).status_code == 404
        assert c.get(path, headers=_auth()).status_code == 404


def test_missing_or_wrong_token_401(make_client):
    c = make_client()
    assert c.get("/admin").status_code == 401
    r = c.get("/admin", headers={"Authorization": "Bearer yanlis"})
    assert r.status_code == 401
    assert "Yönetim paneli" in r.text  # giriş formu
    assert c.get("/admin?token=yanlis").status_code == 401
    r = c.get("/admin/api/sessions", headers={"Authorization": "Bearer yanlis"})
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    c.cookies.set("bf_admin", "sahte")
    assert c.get("/admin").status_code == 401


def test_list_and_detail_with_bearer(make_client, store):
    sid, other = _seed(store)
    c = make_client()
    r = c.get("/admin", headers=_auth())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "botfusions-satis" in r.text and "destek" in r.text
    assert f"/admin/sessions/{sid}" in r.text
    assert "Kullanıcı kapattı" in r.text
    assert "₺37.50" in r.text  # 1M metin girdi token * 0.75 USD * 50 TL
    assert "#0E0B15" in r.text and "#A855F7" in r.text
    assert r.headers["cache-control"] == "no-store"

    # Asistan filtresi
    r = c.get("/admin?agent=destek", headers=_auth())
    assert f"/admin/sessions/{other}" in r.text and f"/admin/sessions/{sid}" not in r.text

    r = c.get(f"/admin/sessions/{sid}", headers=_auth())
    assert r.status_code == 200
    assert "Size nasıl yardımcı olabilirim?" in r.text
    assert "bubble user" in r.text and "bubble agent" in r.text
    assert "create_lead" in r.text

    assert c.get("/admin/sessions/" + "0" * 32, headers=_auth()).status_code == 404
    assert c.get("/admin/sessions/../etc", headers=_auth()).status_code == 404


def test_xss_escaped(make_client, store):
    sid, _ = _seed(store)
    store.create_session('"><script>alert(3)</script>', None)
    c = make_client()
    r = c.get(f"/admin/sessions/{sid}", headers=_auth())
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    assert "<img src=x" not in r.text
    assert "<b>fiyat</b>" not in r.text and "&lt;b&gt;fiyat&lt;/b&gt;" in r.text
    r = c.get("/admin", headers=_auth())
    assert "<script>alert(3)</script>" not in r.text
    r = c.get('/admin?agent="><script>alert(4)</script>', headers=_auth())
    assert "<script>alert(4)</script>" not in r.text


def test_cookie_flow(make_client, store):
    sid, _ = _seed(store)
    c = make_client()
    r = c.get(f"/admin/sessions/{sid}?token={TOKEN}")
    assert r.status_code == 303
    assert r.headers["location"] == f"/admin/sessions/{sid}"  # token URL'den temizlendi
    set_cookie = r.headers["set-cookie"]
    assert "bf_admin=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=strict" in set_cookie.lower()
    assert "Path=/admin" in set_cookie
    assert TOKEN not in set_cookie  # ham token çerezde tutulmaz

    # Diğer sorgu parametreleri korunur
    r2 = c.get(f"/admin?agent=destek&token={TOKEN}&page=1")
    assert r2.status_code == 303 and r2.headers["location"] == "/admin?agent=destek&page=1"

    # Çerezle erişim
    r = c.get(f"/admin/sessions/{sid}")
    assert r.status_code == 200 and "Size nasıl" in r.text
    assert c.get("/admin/api/sessions").status_code == 200


def test_api_json(make_client, store):
    sid, other = _seed(store)
    c = make_client()
    r = c.get("/admin/api/sessions", headers=_auth())
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2 and data["limit"] == 50 and data["offset"] == 0
    assert {i["id"] for i in data["items"]} == {sid, other}
    item = next(i for i in data["items"] if i["id"] == sid)
    assert item["estimated_cost_usd"] == pytest.approx(0.75)
    r = c.get("/admin/api/sessions?agent=destek&limit=1", headers=_auth())
    assert [i["id"] for i in r.json()["items"]] == [other]
    # API ?token= ile yönlendirmesiz çalışır
    r = c.get(f"/admin/api/sessions?token={TOKEN}")
    assert r.status_code == 200


def test_pagination(make_client, store):
    for _ in range(admin_mod.PAGE_SIZE + 3):
        store.create_session("a1", None)
    c = make_client()
    r = c.get("/admin", headers=_auth())
    assert "Sonraki" in r.text and "Önceki" not in r.text
    r = c.get("/admin?page=2", headers=_auth())
    assert "Önceki" in r.text and "Sonraki" not in r.text
    assert r.text.count("/admin/sessions/") == 3


def test_store_missing_503(monkeypatch):
    monkeypatch.setattr(admin_mod, "_configured_token", lambda: TOKEN)
    app = FastAPI()
    app.include_router(admin_mod.router)
    c = TestClient(app)
    assert c.get("/admin", headers=_auth()).status_code == 503
    assert c.get("/admin/api/sessions", headers=_auth()).status_code == 503


def test_estimate_cost_modalities_aux_and_phone(monkeypatch):
    from server.admin import estimate_cost, _fmt_cost
    monkeypatch.delenv("NETGSM_TRY_PER_MIN", raising=False)
    monkeypatch.delenv("USD_TRY", raising=False)
    s = {"input_tokens": 3_000_000, "input_audio_tokens": 1_000_000,
         "output_tokens": 1_000_000, "output_audio_tokens": 1_000_000,
         "aux_input_tokens": 1_000_000, "aux_output_tokens": 1_000_000,
         "origin": "tel:0532***|abcd1234", "duration_s": 120}
    # 2M metin*0.75 + 1M ses*3 + 1M ses çıktı*12 + aux 0.75 + 3.75
    assert round(estimate_cost(s), 6) == 21.036  # varsayılan 2 dk * 0.90 TL / 50
    assert _fmt_cost(1.0) == "₺50.00"
    monkeypatch.setenv("USD_TRY", "40")
    assert round(estimate_cost(s), 6) == 21.045  # varsayılan 2 dk * 0.90 TL / 40
    monkeypatch.setenv("NETGSM_TRY_PER_MIN", "0,50")
    assert round(estimate_cost(s), 6) == 21.025  # 2 dk * 0.50 TL / 40
    assert round(estimate_cost(dict(s, origin="https://botfusions.com")), 6) == 21.0
    assert _fmt_cost(1.0) == "₺40.00"
