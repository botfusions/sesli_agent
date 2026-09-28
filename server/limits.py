"""Erişim ve kullanım limitleri: origin kontrolü, günlük oturum/dakika limitleri."""

from __future__ import annotations

import asyncio
from typing import Any

from server.config import AgentConfig, normalize_origin

LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


def _is_local(origin: str) -> bool:
    # normalize_origin çıktısı: scheme://host[:port]
    host_port = origin.split("://", 1)[1]
    if host_port.startswith("["):
        host = host_port.split("]", 1)[0] + "]"
    else:
        host = host_port.split(":", 1)[0]
    return host in LOCAL_HOSTS


def origin_allowed(origin: str | None, agent: AgentConfig) -> bool:
    """Origin başlığı bu asistan için izinli mi?

    - Origin yoksa/geçersizse reddedilir (tarayıcılar WebSocket'te her zaman gönderir).
    - allowed_origins boşsa yalnızca localhost / 127.0.0.1 (geliştirme) kabul edilir.
    - Aksi halde normalize edilmiş tam eşleşme gerekir.
    """
    norm = normalize_origin(origin)
    if norm is None:
        return False
    if not agent.allowed_origins:
        return _is_local(norm)
    return norm in agent.allowed_origins


def evaluate_daily(usage: dict[str, Any], agent: AgentConfig) -> str | None:
    """usage_today çıktısına göre limit nedeni (yoksa None)."""
    sessions = int(usage.get("sessions") or 0)
    seconds = float(usage.get("seconds") or 0.0)
    if sessions >= agent.limits.max_daily_sessions:
        return "daily_sessions"
    if seconds >= agent.limits.max_daily_minutes * 60:
        return "daily_minutes"
    return None


def session_budget(usage: dict[str, Any], agent: AgentConfig) -> tuple[float, str]:
    """Bu oturum için süre bütçesi (saniye) ve bütçe dolunca bildirilecek neden.

    Günlük kalan dakika oturum süresinden kısaysa bütçe ona göre kısalır.
    """
    remaining_daily = agent.limits.max_daily_minutes * 60 - float(usage.get("seconds") or 0.0)
    if remaining_daily < agent.limits.max_session_seconds:
        return max(remaining_daily, 0.0), "daily_minutes"
    return float(agent.limits.max_session_seconds), "session_time"


async def check_daily(store, agent: AgentConfig) -> str | None:
    """Günlük limit dolduysa nedeni döndürür (daily_sessions / daily_minutes), yoksa None."""
    usage = await asyncio.to_thread(store.usage_today, agent.id)
    return evaluate_daily(usage, agent)
