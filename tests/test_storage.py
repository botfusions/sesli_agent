"""storage.py testleri: CRUD, usage_today, redaksiyon, eşzamanlı yazım."""

import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from server import storage as storage_mod
from server.storage import SCHEMA_VERSION, Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "nested" / "dir" / "db.sqlite")  # klasör yoksa oluşturulmalı
    yield s
    s.close()


def test_creates_folder_and_wal(tmp_path):
    path = tmp_path / "a" / "b" / "v.db"
    s = Store(path)
    assert path.exists()
    mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    s.close()
    # İdempotent: ikinci açılış hata vermez, şema sürümü tek kayıt
    s2 = Store(path)
    rows = s2._conn.execute("SELECT version FROM schema_version").fetchall()
    assert [r[0] for r in rows] == [SCHEMA_VERSION]
    s2.close()


def test_crud_roundtrip(store):
    sid = store.create_session("botfusions-satis", "https://ornek.com")
    assert len(sid) == 32 and all(c in "0123456789abcdef" for c in sid)

    store.add_transcript(sid, "user", "Merhaba, fiyat almak istiyorum")
    store.add_transcript(sid, "agent", "Tabii, hangi ürün?")
    store.add_transcript(sid, "user", "   ")  # boş metin yazılmaz
    store.add_event(sid, "tool", {"name": "create_lead", "status": "ok"})
    store.end_session(sid, "client_end", {"input_tokens": 120, "output_tokens": 80,
                                          "audio_in_s": 12.5, "audio_out_s": 9.0})
    store.set_summary(sid, "- Konu: fiyat\n- Talep: teklif")

    s = store.get_session(sid)
    assert s["agent_id"] == "botfusions-satis"
    assert s["origin"] == "https://ornek.com"
    assert s["end_reason"] == "client_end"
    assert s["input_tokens"] == 120 and s["output_tokens"] == 80
    assert s["audio_in_s"] == 12.5 and s["audio_out_s"] == 9.0
    assert s["duration_s"] is not None and s["duration_s"] >= 0
    assert s["summary"].startswith("- Konu")
    assert [t["role"] for t in s["transcript"]] == ["user", "agent"]
    assert s["events"] == [{"ts": s["events"][0]["ts"], "kind": "tool",
                            "payload": {"name": "create_lead", "status": "ok"}}]

    assert store.get_session("yok") is None


def test_end_session_idempotent(store):
    sid = store.create_session("a1", None)
    store.end_session(sid, "client_end", {"input_tokens": 5})
    store.end_session(sid, "disconnect", {"input_tokens": 999})
    s = store.get_session(sid)
    assert s["end_reason"] == "client_end" and s["input_tokens"] == 5


def test_end_session_tolerates_bad_usage(store):
    sid = store.create_session("a1", None)
    store.end_session(sid, "error", {"input_tokens": None, "output_tokens": "x"})
    s = store.get_session(sid)
    assert s["input_tokens"] == 0 and s["output_tokens"] == 0


def test_list_and_count(store):
    ids = []
    for i in range(5):
        ids.append(store.create_session("a1" if i % 2 == 0 else "a2", None))
        time.sleep(0.002)
    all_rows = store.list_sessions()
    assert [r["id"] for r in all_rows] == list(reversed(ids))  # en yeni önce
    assert len(store.list_sessions(agent_id="a1")) == 3
    assert len(store.list_sessions(agent_id="a2")) == 2
    page = store.list_sessions(limit=2, offset=2)
    assert [r["id"] for r in page] == list(reversed(ids))[2:4]
    assert store.count_sessions() == 5 and store.count_sessions("a2") == 2
    assert store.list_agent_ids() == ["a1", "a2"]


def test_usage_today_utc(store, monkeypatch):
    # "Şimdi"yi sabitle: 2026-09-28 10:00 UTC
    now = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc).timestamp()
    yesterday = now - 12 * 3600  # 27 Eylül 22:00 UTC
    clock = {"t": yesterday}
    monkeypatch.setattr(storage_mod.time, "time", lambda: clock["t"])

    old = store.create_session("a1", None)
    clock["t"] += 60
    store.end_session(old, "client_end", {})

    clock["t"] = now
    s1 = store.create_session("a1", None)
    clock["t"] += 90
    store.end_session(s1, "client_end", {})
    s2 = store.create_session("a1", None)  # açık oturum: sayılır, süresi yok
    store.create_session("a2", None)

    u = store.usage_today("a1")
    assert u == {"sessions": 2, "seconds": 90.0}
    assert store.usage_today("a2") == {"sessions": 1, "seconds": 0.0}
    assert store.usage_today("yok") == {"sessions": 0, "seconds": 0.0}
    assert s2


def test_redaction_applied(store, tmp_path):
    sid = store.create_session("a1", None)
    store.add_transcript(sid, "user", "numaram 0532 123 45 67, mail ali@ornek.com")
    store.add_event(sid, "tool", {"args": {"phone": "+90 532 123 45 67", "n": 3,
                                           "list": ["x@y.com", 1]}})
    store.set_summary(sid, "Müşteri ali@ornek.com adresini verdi")
    s = store.get_session(sid)
    assert s["transcript"][0]["text"] == "numaram [telefon], mail [e-posta]"
    assert s["events"][0]["payload"] == {"args": {"phone": "[telefon]", "n": 3,
                                                  "list": ["[e-posta]", 1]}}
    assert "[e-posta]" in s["summary"]
    # Ham veri diskte de yok
    raw = sqlite3.connect(store._conn.execute("PRAGMA database_list").fetchone()[2])
    dump = "\n".join(raw.iterdump())
    raw.close()
    assert "0532" not in dump and "ali@ornek.com" not in dump


def test_concurrent_writes(store):
    sids = [store.create_session(f"a{i % 3}", None) for i in range(8)]
    errors = []

    def worker(sid):
        try:
            for j in range(50):
                store.add_transcript(sid, "user", f"mesaj {j}")
                store.add_event(sid, "tick", {"j": j})
            store.end_session(sid, "client_end", {"input_tokens": 1})
            store.usage_today("a0")
            store.list_sessions()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(sid,)) for sid in sids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    for sid in sids:
        s = store.get_session(sid)
        assert len(s["transcript"]) == 50 and len(s["events"]) == 50
        assert s["end_reason"] == "client_end"


def test_migrates_v1_db_and_tracks_aux_usage(tmp_path):
    import sqlite3
    path = tmp_path / "v1.db"
    c = sqlite3.connect(path)
    c.executescript(
        "CREATE TABLE schema_version (version INTEGER NOT NULL); INSERT INTO schema_version VALUES (1);"
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, origin TEXT, started_at REAL NOT NULL,"
        " ended_at REAL, end_reason TEXT, duration_s REAL, input_tokens INTEGER NOT NULL DEFAULT 0,"
        " output_tokens INTEGER NOT NULL DEFAULT 0, audio_in_s REAL NOT NULL DEFAULT 0,"
        " audio_out_s REAL NOT NULL DEFAULT 0, summary TEXT);"
    )
    c.close()
    s = Store(path)
    assert [r[0] for r in s._conn.execute("SELECT version FROM schema_version")] == [SCHEMA_VERSION]
    sid = s.create_session("a1", None)
    s.end_session(sid, "client_end", {"input_tokens": 10, "input_audio_tokens": 7, "output_audio_tokens": 3})
    s.add_aux_usage(sid, 100, 20)
    s.add_aux_usage(sid, 1, 2)
    row = s.get_session(sid)
    assert (row["input_audio_tokens"], row["output_audio_tokens"]) == (7, 3)
    assert (row["aux_input_tokens"], row["aux_output_tokens"]) == (101, 22)
    s.close()
