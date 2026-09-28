"""WebSocket ↔ Google ADK `Runner.run_live` köprüsü.

Protokol SPEC.md'deki "WebSocket protokolü" bölümündedir. Akış:
origin kontrolü → `start` bekle → günlük limit → store.create_session → ADK ajanı + run_live
→ iki eşzamanlı görev (istemci→model, model→istemci) + süre sayacı → kapanış, kayıt, özet.

Oturum çekirdeği (`start_session` / `_run_session`) kanaldan bağımsızdır: widget WebSocket'i
ve telefon (Asterisk AudioSocket, bkz. telephony_asterisk.py) aynı çekirdeği kullanır.
Kanal arayüzü: `send_json`, `send_bytes`, `error`, `close`, `closed`; istemci→model yönü
dışarıdan verilen `upstream` coroutine'idir.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from fastapi import WebSocket
from google.adk.agents import LlmAgent
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from server import config, limits, summary, tools
from server.config import AgentConfig

logger = logging.getLogger(__name__)

APP_NAME = "botfusions_voice_agent"
MAX_BINARY_FRAME = 32 * 1024
MAX_TEXT_CHARS = 2000
MAX_JSON_FRAME = 8 * 1024
START_TIMEOUT_S = 30.0
INPUT_MIME = "audio/pcm;rate=16000"
INPUT_BYTES_PER_S = 16000 * 2
OUTPUT_BYTES_PER_S = 24000 * 2

POLICY_VIOLATION = 1008

# Kullanıcıya gösterilecek Türkçe hata metinleri
ERRORS: dict[str, str] = {
    "agent_not_found": "Asistan bulunamadı.",
    "origin_not_allowed": "Bu site bu asistanı kullanmaya yetkili değil.",
    "start_required": "Oturum başlatılamadı: ilk mesaj 'start' olmalı.",
    "bad_message": "Geçersiz mesaj biçimi.",
    "frame_too_large": "Ses parçası çok büyük; parça atlandı.",
    "text_too_long": f"Mesaj en fazla {MAX_TEXT_CHARS} karakter olabilir.",
    "empty_text": "Boş mesaj gönderilemez.",
    "server_not_configured": "Sunucu yapılandırması eksik. Lütfen daha sonra tekrar deneyin.",
    "storage_error": "Oturum kaydı oluşturulamadı. Lütfen tekrar deneyin.",
    "model_error": "Asistan şu anda yanıt veremiyor. Lütfen biraz sonra tekrar deneyin.",
    "internal_error": "Beklenmeyen bir hata oluştu.",
}

# Arka plan özet görevlerinin çöp toplayıcıya gitmemesi için referans tutulur
_background_tasks: set[asyncio.Task] = set()


class Channel(Protocol):
    """Oturum çekirdeğinin kullandığı kanal arayüzü (widget WebSocket'i, telefon hattı)."""

    closed: bool

    async def send_json(self, payload: dict) -> None: ...

    async def send_bytes(self, data: bytes) -> None: ...

    async def error(self, code: str, message: str | None = None) -> None: ...

    async def close(self, code: int = 1000) -> None: ...


class _Channel:
    """WebSocket'e eşzamanlı yazımı kilitle sıralayan ince sarmalayıcı."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self._lock = asyncio.Lock()
        self.closed = False

    async def send_json(self, payload: dict) -> None:
        if self.closed:
            return
        try:
            async with self._lock:
                await self.ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception:  # bağlantı kopmuş olabilir
            self.closed = True

    async def send_bytes(self, data: bytes) -> None:
        if self.closed:
            return
        try:
            async with self._lock:
                await self.ws.send_bytes(data)
        except Exception:
            self.closed = True

    async def error(self, code: str, message: str | None = None) -> None:
        await self.send_json({"type": "error", "code": code, "message": message or ERRORS.get(code, ERRORS["internal_error"])})

    async def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.ws.close(code=code)
        except Exception:
            pass


# --- ADK kurulumu -------------------------------------------------------------


def _language_directive(language: str) -> str:
    if language.lower().startswith("tr"):
        return (
            "Dil: Kullanıcı hangi dilde konuşuyorsa o dilde yanıt ver; kullanıcı dil değiştirirse "
            "sen de değiştir. Kullanıcının dili belli değilse Türkçe konuş. Bir yanıtta tek dil "
            "kullan; başka dilden kelime karıştırma. Kısa, doğal ve sesli konuşmaya uygun cümleler "
            "kur; madde işareti, tablo veya emoji kullanma."
        )
    return (
        "Language: reply in the language the user is speaking; if the user switches language, "
        f"switch too. If the user's language is unclear, use '{language}'. Use a single language "
        "per reply; never mix in words from other languages. Keep replies short and natural for "
        "spoken conversation; no bullet lists, tables or emoji."
    )


def build_instruction(agent: AgentConfig, extra_instruction: str | None = None) -> str:
    """Sistem talimatı = instructions + knowledge + dil talimatı (+ kanala özel ek talimat)."""
    parts = [agent.instructions.strip()]
    if agent.knowledge and agent.knowledge.strip():
        parts.append("## Bilgi (yalnızca buna dayanarak yanıt ver)\n" + agent.knowledge.strip())
    parts.append(_language_directive(agent.language))
    if extra_instruction and extra_instruction.strip():
        parts.append(extra_instruction.strip())
    return "\n\n".join(parts)


def build_run_config(agent: AgentConfig, model: str) -> RunConfig:
    voice = types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=agent.voice))
    # Native-audio modelleri dili kendiliğinden seçer; language_code bu modellerde reddedilebilir.
    speech = types.SpeechConfig(
        voice_config=voice,
        language_code=None if "native-audio" in model else agent.language,
    )
    kwargs: dict[str, Any] = dict(
        streaming_mode=StreamingMode.BIDI,
        response_modalities=[types.Modality.AUDIO],
        speech_config=speech,
        output_audio_transcription=types.AudioTranscriptionConfig(),
        input_audio_transcription=types.AudioTranscriptionConfig(),
    )
    # Uzun görüşmelerde bağlam penceresi taşmasın diye kayan pencere sıkıştırması
    if "context_window_compression" in RunConfig.model_fields:
        kwargs["context_window_compression"] = types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow()
        )
    return RunConfig(**kwargs)


def build_llm_agent(agent: AgentConfig, model: str, adk_tools: list,
                    extra_instruction: str | None = None) -> LlmAgent:
    instruction_text = build_instruction(agent, extra_instruction)

    # Çağrılabilir talimat: ADK'nın {değişken} şablon enjeksiyonunu atlar
    # (bilgi metnindeki süslü parantezler hataya yol açmasın).
    def _instruction(_ctx) -> str:
        return instruction_text

    return LlmAgent(
        name=agent.id.replace("-", "_"),
        model=model,
        description=agent.name,
        instruction=_instruction,
        tools=adk_tools,
    )


# --- Ana işleyici -------------------------------------------------------------


class _Usage:
    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.audio_in_bytes = 0
        self.audio_out_bytes = 0

    def add_metadata(self, meta: Any) -> None:
        # Live API her tur için kullanım bildirir; turlar toplanır
        self.input_tokens += int(getattr(meta, "prompt_token_count", 0) or 0)
        self.output_tokens += int(getattr(meta, "candidates_token_count", 0) or 0)

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "audio_in_s": round(self.audio_in_bytes / INPUT_BYTES_PER_S, 2),
            "audio_out_s": round(self.audio_out_bytes / OUTPUT_BYTES_PER_S, 2),
        }


async def _wait_for_start(ws: WebSocket) -> bool:
    """İlk mesajın {"type":"start"} olmasını bekler."""
    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=START_TIMEOUT_S)
    except asyncio.TimeoutError:
        return False
    if msg.get("type") != "websocket.receive" or msg.get("text") is None:
        return False
    try:
        data = json.loads(msg["text"])
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("type") == "start"


async def handle_live(ws: WebSocket, agent_id: str, store, settings) -> None:
    chan = _Channel(ws)
    await ws.accept()

    try:
        agent = config.get_agent(agent_id)
    except KeyError:
        await chan.error("agent_not_found")
        await chan.close(POLICY_VIOLATION)
        return

    origin = ws.headers.get("origin")
    if not limits.origin_allowed(origin, agent):
        logger.info("Rejected websocket origin=%r agent=%s", origin, agent.id)
        await chan.error("origin_not_allowed")
        await chan.close(POLICY_VIOLATION)
        return

    if not await _wait_for_start(ws):
        await chan.error("start_required")
        await chan.close(POLICY_VIOLATION)
        return

    await start_session(
        chan, agent, store, settings,
        origin=origin,
        upstream=_widget_upstream(ws, chan),
        greeting=agent.greeting,
    )


@dataclass
class SessionIO:
    """Upstream coroutine'ine verilen oturum bağlamı."""

    session_id: str
    queue: LiveRequestQueue
    usage: _Usage
    record_transcript: Callable[[str, str], Awaitable[None]]
    record_event: Callable[[str, dict], Awaitable[None]]


# İstemci → model yönü: dönüş değeri oturumun bitiş nedenidir (ör. "client_end", "caller_hangup").
Upstream = Callable[[SessionIO], Awaitable[str]]


def _widget_upstream(ws: WebSocket, chan: _Channel) -> Upstream:
    """Widget WebSocket'inden gelen ses/metin çerçevelerini modele ileten upstream."""

    async def upstream(io: SessionIO) -> str:
        queue, usage = io.queue, io.usage
        while True:
            msg = await ws.receive()
            mtype = msg.get("type")
            if mtype == "websocket.disconnect":
                return "disconnect"
            if mtype != "websocket.receive":
                continue
            data = msg.get("bytes")
            if data is not None:
                if len(data) > MAX_BINARY_FRAME:
                    await chan.error("frame_too_large")
                    continue
                if not data:
                    continue
                usage.audio_in_bytes += len(data)
                queue.send_realtime(types.Blob(data=data, mime_type=INPUT_MIME))
                continue
            text = msg.get("text")
            if text is None:
                continue
            if len(text) > MAX_JSON_FRAME:
                await chan.error("bad_message")
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                await chan.error("bad_message")
                continue
            if not isinstance(payload, dict):
                await chan.error("bad_message")
                continue
            ptype = payload.get("type")
            if ptype == "end":
                return "client_end"
            if ptype == "start":
                continue  # yinelenen start yok sayılır
            if ptype == "text":
                user_text = payload.get("text")
                if not isinstance(user_text, str) or not user_text.strip():
                    await chan.error("empty_text")
                    continue
                if len(user_text) > MAX_TEXT_CHARS:
                    await chan.error("text_too_long")
                    continue
                user_text = user_text.strip()
                queue.send_content(types.Content(role="user", parts=[types.Part(text=user_text)]))
                await io.record_transcript("user", user_text)
                continue
            await chan.error("bad_message")

    return upstream


async def start_session(
    chan: Channel,
    agent: AgentConfig,
    store,
    settings,
    *,
    origin: str | None,
    upstream: Upstream,
    greeting: str | None,
    extra_instruction: str | None = None,
    on_session_created: Callable[[str], None] | None = None,
) -> str | None:
    """Kanaldan bağımsız oturum açılışı: model yapılandırması → günlük limit → kayıt → canlı oturum.

    Oturum açılamazsa kanal kapatılır ve None döner; aksi halde oturum bitene kadar bekler
    ve kayıt oturum kimliğini döndürür.
    """
    if not settings.model_configured:
        logger.error("Live model credentials missing (GEMINI_API_KEY / Vertex settings)")
        await chan.error("server_not_configured")
        await chan.close(1011)
        return None

    try:
        usage_today = await asyncio.to_thread(store.usage_today, agent.id)
    except Exception:
        logger.exception("usage_today failed")
        await chan.error("storage_error")
        await chan.close(1011)
        return None
    reason = limits.evaluate_daily(usage_today, agent)
    if reason:
        await chan.send_json({"type": "limit", "reason": reason})
        await chan.close(1000)
        return None
    budget_s, budget_reason = limits.session_budget(usage_today, agent)

    try:
        session_id = await asyncio.to_thread(store.create_session, agent.id, origin)
    except Exception:
        logger.exception("create_session failed")
        await chan.error("storage_error")
        await chan.close(1011)
        return None

    if on_session_created is not None:
        try:
            on_session_created(session_id)
        except Exception:
            logger.exception("on_session_created callback failed")

    await _run_session(
        chan, agent, store, settings, session_id, budget_s, budget_reason,
        upstream=upstream, greeting=greeting, extra_instruction=extra_instruction,
    )
    return session_id


async def _run_session(
    chan: Channel,
    agent: AgentConfig,
    store,
    settings,
    session_id: str,
    budget_s: float,
    budget_reason: str,
    *,
    upstream: Upstream,
    greeting: str | None,
    extra_instruction: str | None = None,
) -> None:
    usage = _Usage()
    transcript: list[dict] = []
    end_reason = "unknown"
    started = time.monotonic()
    queue = LiveRequestQueue()

    async def record_event(kind: str, payload: dict) -> None:
        try:
            await asyncio.to_thread(store.add_event, session_id, kind, payload)
        except Exception:
            logger.exception("add_event failed kind=%s", kind)

    async def record_transcript(role: str, text: str) -> None:
        transcript.append({"role": role, "text": text})
        try:
            await asyncio.to_thread(store.add_transcript, session_id, role, text)
        except Exception:
            logger.exception("add_transcript failed")

    async def on_tool_event(event: dict) -> None:
        payload = {"type": "tool", **{k: v for k, v in event.items() if k != "type"}}
        await chan.send_json(payload)
        await record_event("tool", payload)

    try:
        model = agent.model or settings.live_model
        adk_tools = tools.build_adk_tools(agent, on_tool_event, session_id=session_id)
        llm_agent = build_llm_agent(agent, model, adk_tools, extra_instruction)
        session_service = InMemorySessionService()
        # ADK oturum kimliği = kayıt oturum kimliği (araçlar tool_context üzerinden okuyabilir)
        await session_service.create_session(
            app_name=APP_NAME,
            user_id=agent.id,
            session_id=session_id,
            state={"agent_id": agent.id, "session_id": session_id},
        )
        runner = Runner(app_name=APP_NAME, agent=llm_agent, session_service=session_service)
        run_config = build_run_config(agent, model)
    except Exception:
        logger.exception("Failed to build live agent for %s", agent.id)
        await chan.error("internal_error")
        await record_event("error", {"code": "internal_error", "stage": "setup"})
        await _finish(chan, store, agent, session_id, "error", usage, transcript, 1011)
        return

    await chan.send_json({"type": "ready", "session_id": session_id, "agent": config.public_view(agent)})

    if greeting:
        queue.send_content(
            types.Content(
                role="user",
                parts=[types.Part(text=(
                    "[Sistem] Görüşme yeni başladı. Kullanıcıyı şu cümleyle selamla ve "
                    f"sonra onu dinle: \"{greeting}\""
                ))],
            )
        )

    io = SessionIO(session_id=session_id, queue=queue, usage=usage,
                   record_transcript=record_transcript, record_event=record_event)

    buffers: dict[str, str] = {"user": "", "agent": ""}

    async def handle_transcription(role: str, tr: Any) -> None:
        text = getattr(tr, "text", None) or ""
        if getattr(tr, "finished", False):
            final_text = (text or buffers[role]).strip()
            buffers[role] = ""
            if final_text:
                await chan.send_json({"type": "transcript", "role": role, "text": final_text, "final": True})
                await record_transcript(role, final_text)
        elif text:
            # Kısmi parçalar artımlıdır; istemciye birikmiş metin gönderilir
            buffers[role] += text
            await chan.send_json({"type": "transcript", "role": role, "text": buffers[role], "final": False})

    async def downstream() -> str:
        """Model → istemci."""
        async for event in runner.run_live(
            user_id=agent.id,
            session_id=session_id,
            live_request_queue=queue,
            run_config=run_config,
        ):
            if event.usage_metadata is not None:
                usage.add_metadata(event.usage_metadata)
            if event.error_code:
                logger.warning("Live model error code=%s", event.error_code)
                await record_event("error", {"code": "model_error", "detail": str(event.error_code)})
                await chan.error("model_error")
            content = event.content
            if content and content.parts:
                for part in content.parts:
                    blob = part.inline_data
                    if blob and blob.data and (blob.mime_type or "").startswith("audio/"):
                        usage.audio_out_bytes += len(blob.data)
                        await chan.send_bytes(blob.data)
            if event.input_transcription is not None:
                await handle_transcription("user", event.input_transcription)
            if event.output_transcription is not None:
                await handle_transcription("agent", event.output_transcription)
            if event.interrupted:
                await chan.send_json({"type": "interrupted"})
            if event.turn_complete:
                await chan.send_json({"type": "turn_complete"})
            if chan.closed:
                return "disconnect"
        return "model_closed"

    async def timer() -> str:
        await asyncio.sleep(max(budget_s, 0.0))
        return budget_reason

    tasks = {
        asyncio.create_task(upstream(io), name="upstream"),
        asyncio.create_task(downstream(), name="downstream"),
        asyncio.create_task(timer(), name="timer"),
    }
    close_code = 1000
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # Aynı anda birden çok görev bittiyse istemci tarafının nedeni (ör. caller_hangup) önceliklidir
        task = next((t for t in done if t.get_name() == "upstream"), next(iter(done)))
        try:
            end_reason = task.result()
        except Exception:
            logger.exception("Live task %s failed", task.get_name())
            end_reason = "error"
            close_code = 1011
            await chan.error("model_error" if task.get_name() == "downstream" else "internal_error")
        if end_reason in {"session_time", "daily_minutes"}:
            await chan.send_json({"type": "limit", "reason": end_reason})
            await record_event("limit", {"reason": end_reason})
        elif end_reason == "model_closed":
            await chan.error("model_error")
    finally:
        queue.close()
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(
            "Live session ended agent=%s session=%s reason=%s duration=%.1fs",
            agent.id, session_id, end_reason, time.monotonic() - started,
        )
        await _finish(chan, store, agent, session_id, end_reason, usage, transcript, close_code)


async def _finish(chan, store, agent, session_id, reason, usage: _Usage, transcript, close_code) -> None:
    await chan.close(close_code)
    # Oturum başına araç çağrı sayaçlarını bırak (tools modülü destekliyorsa)
    reset = getattr(tools, "reset_call_counts", None)
    if callable(reset):
        try:
            reset(session_id)
        except Exception:
            logger.exception("reset_call_counts failed")
    try:
        await asyncio.to_thread(store.end_session, session_id, reason, usage.as_dict())
    except Exception:
        logger.exception("end_session failed")
    if transcript:
        task = asyncio.create_task(_summarize_later(store, session_id, list(transcript), agent.language))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)


async def _summarize_later(store, session_id: str, transcript: list[dict], language: str) -> None:
    """Oturum özeti arka planda üretilir; hata yutulur."""
    try:
        text = await summary.summarize(transcript, language.split("-")[0] or "tr")
        if text:
            await asyncio.to_thread(store.set_summary, session_id, text)
            # Bu oturumda CRM'e aday yazıldıysa özeti adayın kaydına ekle (notes_column tanımlıysa)
            from server import crm_supabase
            await crm_supabase.attach_summary(session_id, text)
    except Exception:
        logger.exception("Summary generation failed for session %s", session_id)
