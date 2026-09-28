"""SQLite kayıt katmanı (stdlib sqlite3).

Tek bağlantı + threading.Lock ile thread-safe; async koddan `asyncio.to_thread` ile çağrılmalıdır.
Tüm metinler ve olay yüklerindeki string değerler `redact()` ile maskelenerek yazılır.
Zaman damgaları UTC unix saniyesi (REAL) olarak tutulur.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.redact import redact

__all__ = ["Store", "SCHEMA_VERSION"]

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    origin        TEXT,
    started_at    REAL NOT NULL,
    ended_at      REAL,
    end_reason    TEXT,
    duration_s    REAL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    audio_in_s    REAL NOT NULL DEFAULT 0,
    audio_out_s   REAL NOT NULL DEFAULT 0,
    summary       TEXT,
    input_audio_tokens  INTEGER NOT NULL DEFAULT 0,
    output_audio_tokens INTEGER NOT NULL DEFAULT 0,
    aux_input_tokens    INTEGER NOT NULL DEFAULT 0,
    aux_output_tokens   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transcripts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ts         REAL NOT NULL,
    role       TEXT NOT NULL,
    text       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ts           REAL NOT NULL,
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_agent_started ON sessions(agent_id, started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_transcripts_session ON transcripts(session_id, id);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, id);
"""

_V2_COLS = ("input_audio_tokens", "output_audio_tokens", "aux_input_tokens", "aux_output_tokens")
_SESSION_COLS = (
    "id, agent_id, origin, started_at, ended_at, end_reason, duration_s, "
    "input_tokens, output_tokens, audio_in_s, audio_out_s, summary, " + ", ".join(_V2_COLS)
)


def _redact_value(value: Any) -> Any:
    """Olay yüklerini özyinelemeli olarak maskeler (yalnızca string değerler)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


def _utc_day_start(now: float) -> float:
    d = datetime.fromtimestamp(now, tz=timezone.utc)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _int(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _float(v: Any) -> float:
    try:
        return max(0.0, float(v or 0))
    except (TypeError, ValueError):
        return 0.0


class Store:
    """Oturum, transkript ve olay kayıtları."""

    def __init__(self, path: Path | str):
        path_str = str(path)
        if path_str != ":memory:":
            # Klasör yoksa oluştur
            Path(path_str).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path_str, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if path_str != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            if row["v"] is None:
                self._conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["v"] < 2:
                # v2: maliyet için ses/metin token ayrımı ve özet+öğrenme (aux) token'ları
                for col in _V2_COLS:
                    self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
                self._conn.execute("UPDATE schema_version SET version = 2")

    # ---- yazma -------------------------------------------------------------

    def create_session(self, agent_id: str, origin: str | None) -> str:
        sid = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(id, agent_id, origin, started_at) VALUES (?, ?, ?, ?)",
                (sid, agent_id, origin, time.time()),
            )
        return sid

    def add_transcript(self, session_id: str, role: str, text: str) -> None:
        if not text or not text.strip():
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO transcripts(session_id, ts, role, text) VALUES (?, ?, ?, ?)",
                (session_id, time.time(), role, redact(text)),
            )

    def add_event(self, session_id: str, kind: str, payload: dict) -> None:
        payload_json = json.dumps(_redact_value(payload or {}), ensure_ascii=False, default=str)
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(session_id, ts, kind, payload_json) VALUES (?, ?, ?, ?)",
                (session_id, time.time(), kind, payload_json),
            )

    def end_session(self, session_id: str, reason: str, usage: dict) -> None:
        """Oturumu kapatır. İkinci çağrı ilk kaydı ezmez (idempotent)."""
        usage = usage or {}
        now = time.time()
        with self._lock:
            self._conn.execute(
                """UPDATE sessions SET
                       ended_at = ?, end_reason = ?, duration_s = MAX(0, ? - started_at),
                       input_tokens = ?, output_tokens = ?, audio_in_s = ?, audio_out_s = ?,
                       input_audio_tokens = ?, output_audio_tokens = ?
                   WHERE id = ? AND ended_at IS NULL""",
                (
                    now,
                    reason,
                    now,
                    _int(usage.get("input_tokens")),
                    _int(usage.get("output_tokens")),
                    _float(usage.get("audio_in_s")),
                    _float(usage.get("audio_out_s")),
                    _int(usage.get("input_audio_tokens")),
                    _int(usage.get("output_audio_tokens")),
                    session_id,
                ),
            )

    def add_aux_usage(self, session_id: str, input_tokens: int, output_tokens: int) -> None:
        """Görüşme sonrası özet/öğrenme model çağrılarının token'larını oturuma ekler."""
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET aux_input_tokens = aux_input_tokens + ?, "
                "aux_output_tokens = aux_output_tokens + ? WHERE id = ?",
                (_int(input_tokens), _int(output_tokens), session_id),
            )

    def set_summary(self, session_id: str, summary: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET summary = ? WHERE id = ?",
                (redact(summary or ""), session_id),
            )

    # ---- okuma -------------------------------------------------------------

    def usage_today(self, agent_id: str) -> dict:
        """Bugünkü (UTC) oturum sayısı ve tamamlanmış oturumların toplam süresi.

        Not: süre yalnızca `end_session` ile kapanmış oturumlardan hesaplanır.
        """
        start = _utc_day_start(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(duration_s), 0) AS s "
                "FROM sessions WHERE agent_id = ? AND started_at >= ?",
                (agent_id, start),
            ).fetchone()
        return {"sessions": int(row["n"]), "seconds": float(row["s"])}

    def count_sessions_today(self, agent_id: str, origin: str) -> int:
        """Bugün (UTC) bu asistanda aynı origin ile açılan oturum sayısı.

        Telefon kanalında origin arayanı temsil eder (`tel:<maskeli>|<hmac8>`);
        arayan başına günlük arama limiti buna göre uygulanır.
        """
        start = _utc_day_start(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE agent_id = ? AND origin = ? AND started_at >= ?",
                (agent_id, origin, start),
            ).fetchone()
        return int(row["n"])

    def list_sessions(self, agent_id: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        sql = f"SELECT {_SESSION_COLS} FROM sessions"
        params: list[Any] = []
        if agent_id:
            sql += " WHERE agent_id = ?"
            params.append(agent_id)
        sql += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def count_sessions(self, agent_id: str | None = None) -> int:
        """Sayfalama için toplam oturum sayısı (SPEC'e ek)."""
        with self._lock:
            if agent_id:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM sessions WHERE agent_id = ?", (agent_id,)
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()
        return int(row["n"])

    def list_agent_ids(self) -> list[str]:
        """Kayıtlı oturumu olan asistan kimlikleri (admin filtresi için; SPEC'e ek)."""
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT agent_id FROM sessions ORDER BY agent_id").fetchall()
        return [r["agent_id"] for r in rows]

    def get_session(self, session_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_SESSION_COLS} FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            trows = self._conn.execute(
                "SELECT ts, role, text FROM transcripts WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
            erows = self._conn.execute(
                "SELECT ts, kind, payload_json FROM events WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        result = dict(row)
        result["transcript"] = [dict(r) for r in trows]
        events = []
        for r in erows:
            try:
                payload = json.loads(r["payload_json"])
            except (TypeError, ValueError):
                payload = {}
            events.append({"ts": r["ts"], "kind": r["kind"], "payload": payload})
        result["events"] = events
        return result

    def close(self) -> None:
        with self._lock:
            self._conn.close()
