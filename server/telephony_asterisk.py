"""Telefon kanalı: Netgsm SIP → Asterisk → AudioSocket (TCP) → bu sunucu → Gemini Live.

Akış:
  1. Asterisk çağrı gelince `POST /telephony/asterisk/call` ile (form-urlencoded: uuid, caller,
     agent, token) çağrıyı kaydeder. Kayıt bellekte tutulur: TTL 30 sn, tek kullanımlık.
  2. Ardından `AudioSocket(uuid, host:port)` ile bu sunucunun TCP portuna bağlanır.
     İlk çerçeve UUID'dir; kayıtlı değilse bağlantı kapatılır.
  3. Ses çerçeveleri (slin 16-bit LE 8 kHz mono) 16 kHz'e çevrilip ~100 ms'lik parçalar halinde
     modele gider; modelin 24 kHz sesi 8 kHz'e çevrilip 20 ms'de bir 320 baytlık çerçevelerle
     Asterisk'e gönderilir. Oturum çekirdeği widget ile ortaktır (live.start_session).

AudioSocket çerçevesi: 1 bayt tip + 2 bayt uzunluk (big-endian) + yük.
Tipler: 0x00 kapatma, 0x01 UUID (16 bayt), 0x03 DTMF, 0x10 ses, 0xff hata.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import PlainTextResponse
from google.genai import types

from server import config, crm_supabase, live
from server.audio_codec import Downsampler24to8, Upsampler8to16
from server.config import AgentConfig

logger = logging.getLogger(__name__)

# --- Protokol sabitleri -------------------------------------------------------

KIND_HANGUP = 0x00
KIND_UUID = 0x01
KIND_DTMF = 0x03
KIND_AUDIO = 0x10
KIND_ERROR = 0xFF

MAX_FRAME_PAYLOAD = 8 * 1024        # Asterisk 20 ms'lik (320 bayt) çerçeve gönderir; fazlası protokol hatası
FRAME_BYTES = 320                   # 8 kHz × 2 bayt × 20 ms
FRAME_S = 0.020
MAX_SEND_LAG_S = 0.200              # gönderici bu kadar geride kalırsa zamanlama sıfırlanır (patlama olmasın)
MAX_OUT_BUFFER = 8000 * 2 * 120     # en fazla 2 dk'lık çıkış sesi tamponlanır
INPUT_CHUNK_BYTES = 16000 * 2 // 10  # modele ~100 ms'lik 16 kHz parçalar
UUID_TIMEOUT_S = 5.0                # bağlantıdan sonra UUID çerçevesi için bekleme
READ_IDLE_TIMEOUT_S = 15.0          # Asterisk arama boyunca sürekli ses yollar; susarsa hat kopmuştur
CLOSE_TIMEOUT_S = 2.0

# --- Kayıt ucu sabitleri ------------------------------------------------------

CALL_TTL_S = 30.0
MAX_PENDING_CALLS = 1000
MAX_REGISTER_BODY = 4 * 1024
MAX_CALLER_CHARS = 64

ANONYMOUS_CALLERS = {"", "anonymous", "unknown", "restricted", "private", "unavailable", "gizli"}

PHONE_BASE_INSTRUCTION = "Bu bir telefon görüşmesi; cümleler kısa olsun; e-posta isteme."
PHONE_KNOWN_CALLER = (
    "Arayanın numarası sistemde kayıtlı; book_demo'yu phone parametresi olmadan çağırabilirsin, "
    "numarayı tekrar sorma."
)
PHONE_HIDDEN_CALLER = "Numara görünmüyor; randevu için telefon numarasını sor."


# --- Bekleyen çağrı kaydı -------------------------------------------------------


@dataclass(frozen=True)
class PendingCall:
    agent_id: str
    caller: str
    registered_at: float


class CallRegistry:
    """uuid → bekleyen çağrı. TTL'li, tek kullanımlık, boyutu sınırlı (en eski atılır)."""

    def __init__(self, ttl_s: float = CALL_TTL_S, max_entries: int = MAX_PENDING_CALLS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self.clock = clock
        self._items: "OrderedDict[str, PendingCall]" = OrderedDict()
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        while self._items:
            key, item = next(iter(self._items.items()))
            if now - item.registered_at < self.ttl_s:
                break
            del self._items[key]

    def put(self, call_id: str, agent_id: str, caller: str) -> None:
        with self._lock:
            now = self.clock()
            self._purge(now)
            self._items.pop(call_id, None)  # yeniden kayıt: süre baştan başlar
            self._items[call_id] = PendingCall(agent_id=agent_id, caller=caller, registered_at=now)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def pop(self, call_id: str) -> PendingCall | None:
        """Kaydı alır ve siler; yoksa ya da süresi dolduysa None."""
        with self._lock:
            now = self.clock()
            self._purge(now)
            item = self._items.pop(call_id, None)
        if item is None or now - item.registered_at >= self.ttl_s:
            return None
        return item

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


# --- Arayan numarası yardımcıları ---------------------------------------------


def normalize_caller(caller: str | None) -> str | None:
    """Arayan numarasını normalize eder; gizli/boş/tanınmaz numara için None."""
    raw = (caller or "").strip()
    if raw.lower() in ANONYMOUS_CALLERS:
        return None
    return crm_supabase._normalize_phone(raw)


def mask_phone(phone: str) -> str:
    """+905321234567 → +90532***4567 (kayıt/yönetim ekranı için)."""
    head_len = 6 if phone.startswith("+") else 3
    if len(phone) <= head_len + 4:
        head_len = 2
    return f"{phone[:head_len]}***{phone[-4:]}"


def caller_origin(phone: str | None, secret: str | None) -> str:
    """Oturum kaydındaki origin: `tel:<maskeli numara>|<hmac8>` ya da `tel:anonymous`.

    HMAC, maskeli numaralar çakışsa bile aynı arayanın günlük aramalarını saymayı sağlar;
    numaranın kendisi veritabanına yazılmaz.
    """
    if not phone:
        return "tel:anonymous"
    digest = hmac.new((secret or "").encode(), phone.encode(), hashlib.sha256).hexdigest()[:8]
    return f"tel:{mask_phone(phone)}|{digest}"


def build_phone_instruction(agent: AgentConfig, has_caller: bool) -> str:
    """Telefon kanalına özel ek talimat (numara durumuna göre değişir)."""
    parts = [PHONE_BASE_INSTRUCTION, PHONE_KNOWN_CALLER if has_caller else PHONE_HIDDEN_CALLER]
    extra = agent.telephony.instructions
    if extra and extra.strip():
        parts.append(extra.strip())
    return "\n".join(parts)


# --- Kayıt ucu (Asterisk → HTTP) ----------------------------------------------


def _text(status: int, body: str) -> PlainTextResponse:
    return PlainTextResponse(body, status_code=status)


async def handle_register(request: Request) -> PlainTextResponse:
    """`POST /telephony/asterisk/call` — form-urlencoded: uuid, caller, agent, token."""
    settings = request.app.state.settings
    registry: CallRegistry = request.app.state.call_registry
    secret = settings.telephony_secret
    if not settings.telephony_enabled or not secret:
        return _text(404, "Bulunamadı.")

    body = await request.body()
    if len(body) > MAX_REGISTER_BODY:
        return _text(400, "İstek çok büyük.")
    try:
        # python-multipart bağımlılığı eklememek için gövde elle ayrıştırılır
        form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True,
                        max_num_fields=20)
    except ValueError:
        return _text(400, "Geçersiz istek.")

    def field_value(name: str) -> str:
        return (form.get(name) or [""])[0].strip()

    token = field_value("token")
    if not hmac.compare_digest(token.encode(), secret.encode()):
        logger.warning("Telephony register rejected: bad token from %s",
                       request.client.host if request.client else "?")
        return _text(403, "Yetkisiz.")

    try:
        call_id = str(uuid.UUID(field_value("uuid")))
    except ValueError:
        return _text(400, "Geçersiz uuid.")

    agent_id = field_value("agent")
    try:
        agent = config.get_agent(agent_id)
    except KeyError:
        return _text(404, "Asistan bulunamadı.")
    if not agent.telephony.enabled:
        return _text(404, "Asistan bulunamadı.")

    caller = field_value("caller")[:MAX_CALLER_CHARS]
    registry.put(call_id, agent.id, caller)
    logger.info("Telephony call registered uuid=%s agent=%s", call_id, agent.id)
    return _text(200, "OK")


# --- AudioSocket çerçeveleri --------------------------------------------------


class FrameError(Exception):
    """Protokol dışı çerçeve (ör. aşırı uzun)."""


def encode_frame(kind: int, payload: bytes = b"") -> bytes:
    return bytes([kind]) + len(payload).to_bytes(2, "big") + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(3)
    kind = header[0]
    length = int.from_bytes(header[1:3], "big")
    if length > MAX_FRAME_PAYLOAD:
        raise FrameError(f"frame too large: {length}")
    payload = await reader.readexactly(length) if length else b""
    return kind, payload


# --- Telefon kanalı -----------------------------------------------------------


class TelephonyChannel:
    """live.Channel arayüzünün AudioSocket karşılığı.

    Modelin 24 kHz sesi 8 kHz'e çevrilip tampona eklenir; ayrı gönderici görev tamponu
    monotonik saatle 20 ms'de bir 320 baytlık ses çerçeveleri halinde gönderir.
    """

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.closed = False
        self._downsampler = Downsampler24to8()
        self._buf = bytearray()
        self._data_ready = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._sender: asyncio.Task | None = None
        self._shutdown = False
        self.frames_sent = 0

    def start(self) -> None:
        if self._sender is None:
            self._sender = asyncio.create_task(self._send_loop(), name="audiosocket-sender")

    async def send_json(self, payload: dict) -> None:
        ptype = payload.get("type")
        if ptype == "interrupted":
            # Söz kesme (barge-in): çalınmamış ses atılır
            self.clear_audio()
        elif ptype in {"tool", "limit"}:
            logger.info("Telephony event: %s", {k: v for k, v in payload.items() if k != "summary"})
        else:
            logger.debug("Telephony ignores event type=%s", ptype)

    async def send_bytes(self, data: bytes) -> None:
        if self.closed or not data:
            return
        pcm8 = self._downsampler.process(data)
        if len(self._buf) + len(pcm8) > MAX_OUT_BUFFER:
            logger.warning("Telephony output buffer full; dropping %d bytes", len(pcm8))
            return
        self._buf.extend(pcm8)
        self._data_ready.set()

    def clear_audio(self) -> None:
        self._buf.clear()
        self._downsampler.reset()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buf)

    async def error(self, code: str, message: str | None = None) -> None:
        # Telefonda metin gösterilemez; yalnızca loglanır
        logger.warning("Telephony session error code=%s", code)

    async def _write(self, data: bytes) -> bool:
        try:
            async with self._write_lock:
                self.writer.write(data)
                await self.writer.drain()
            return True
        except Exception:  # bağlantı kopmuş olabilir
            self.closed = True
            return False

    async def _send_loop(self) -> None:
        loop = asyncio.get_running_loop()
        next_t: float | None = None
        partial_ticks = 0
        while not self.closed:
            if not self._buf:
                self._data_ready.clear()
                await self._data_ready.wait()
                next_t = None
                continue
            now = loop.time()
            if next_t is None or now - next_t > MAX_SEND_LAG_S:
                next_t = now
            delay = next_t - now
            if delay > 0:
                await asyncio.sleep(delay)
            # Mutlak zaman çizelgesi: gecikmeler birikmez (kayma olmaz)
            next_t += FRAME_S
            n = len(self._buf)
            frame: bytes | None = None
            if n >= FRAME_BYTES:
                frame = bytes(self._buf[:FRAME_BYTES])
                del self._buf[:FRAME_BYTES]
                partial_ticks = 0
            elif n > 0:
                # Eksik çerçeve: bir tur daha veri bekle, gelmezse sessizlikle tamamla
                partial_ticks += 1
                if partial_ticks > 1:
                    frame = bytes(self._buf) + b"\x00" * (FRAME_BYTES - n)
                    self._buf.clear()
                    partial_ticks = 0
            if frame is not None:
                if not await self._write(encode_frame(KIND_AUDIO, frame)):
                    return
                self.frames_sent += 1

    async def close(self, code: int = 1000) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self.closed = True
        self._data_ready.set()
        if self._sender is not None:
            self._sender.cancel()
            await asyncio.gather(self._sender, return_exceptions=True)
        try:
            # Asterisk'e kapatma çerçevesi, ardından soketi kapat
            self.writer.write(encode_frame(KIND_HANGUP))
            await asyncio.wait_for(self.writer.drain(), timeout=CLOSE_TIMEOUT_S)
        except Exception:
            pass
        await _close_writer(self.writer)


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    try:
        if not writer.is_closing():
            writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=CLOSE_TIMEOUT_S)
    except Exception:
        pass


def telephony_upstream(reader: asyncio.StreamReader) -> live.Upstream:
    """Asterisk → model: 8 kHz ses 16 kHz'e çevrilip ~100 ms'lik parçalarla kuyruğa gider."""
    upsampler = Upsampler8to16()

    async def upstream(io: live.SessionIO) -> str:
        pending = bytearray()
        while True:
            try:
                kind, payload = await asyncio.wait_for(read_frame(reader), timeout=READ_IDLE_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.info("AudioSocket idle timeout session=%s", io.session_id)
                return "caller_timeout"
            except (asyncio.IncompleteReadError, ConnectionError):
                return "caller_hangup"
            except FrameError as exc:
                logger.warning("AudioSocket protocol error session=%s: %s", io.session_id, exc)
                return "protocol_error"
            if kind == KIND_HANGUP:
                return "caller_hangup"
            if kind == KIND_AUDIO:
                if not payload:
                    continue
                pending.extend(upsampler.process(payload))
                while len(pending) >= INPUT_CHUNK_BYTES:
                    chunk = bytes(pending[:INPUT_CHUNK_BYTES])
                    del pending[:INPUT_CHUNK_BYTES]
                    io.usage.audio_in_bytes += len(chunk)
                    io.queue.send_realtime(types.Blob(data=chunk, mime_type=live.INPUT_MIME))
            elif kind == KIND_DTMF:
                # Tuşlanan rakam loglanmaz (PIN vb. olabilir)
                logger.debug("AudioSocket DTMF received session=%s", io.session_id)
            elif kind == KIND_ERROR:
                logger.warning("AudioSocket error frame session=%s code=%s", io.session_id, payload.hex())
                return "telephony_error"
            else:
                logger.debug("AudioSocket ignores frame kind=0x%02x", kind)

    return upstream


# --- TCP sunucusu -------------------------------------------------------------


class AudioSocketServer:
    """asyncio TCP sunucusu; her bağlantı bir telefon görüşmesidir."""

    def __init__(self, *, host: str, port: int, registry: CallRegistry, store, settings) -> None:
        self.host = host
        self.requested_port = port
        self.registry = registry
        self.store = store
        self.settings = settings
        self._server: asyncio.base_events.Server | None = None
        self._conns: set[asyncio.Task] = set()

    @property
    def port(self) -> int | None:
        if not self._server or not self._server.sockets:
            return None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_connect, self.host, self.requested_port)
        logger.info("AudioSocket server listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in list(self._conns):
            task.cancel()
        if self._conns:
            await asyncio.gather(*self._conns, return_exceptions=True)
        if self._server is not None:
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=CLOSE_TIMEOUT_S)
            except Exception:
                pass
            self._server = None
        logger.info("AudioSocket server stopped")

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._conns.add(task)
        try:
            await self._handle(reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("AudioSocket connection failed")
        finally:
            await _close_writer(writer)
            if task is not None:
                self._conns.discard(task)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            kind, payload = await asyncio.wait_for(read_frame(reader), timeout=UUID_TIMEOUT_S)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, FrameError):
            logger.warning("AudioSocket %s: no UUID frame", peer)
            return
        if kind != KIND_UUID or len(payload) != 16:
            logger.warning("AudioSocket %s: first frame must be UUID (kind=0x%02x)", peer, kind)
            return
        call_id = str(uuid.UUID(bytes=payload))
        pending = self.registry.pop(call_id)
        if pending is None:
            logger.warning("AudioSocket %s: unregistered or expired uuid=%s", peer, call_id)
            return
        await run_call(pending, reader, writer, store=self.store, settings=self.settings)


async def run_call(pending: PendingCall, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                   *, store, settings) -> None:
    """Kayıtlı çağrı için canlı oturumu yürütür (kapanışta Asterisk'e 0x00 gider)."""
    chan = TelephonyChannel(writer)
    try:
        agent = config.get_agent(pending.agent_id)
    except KeyError:
        agent = None
    if agent is None or not agent.telephony.enabled:
        logger.warning("Telephony call for unavailable agent=%s", pending.agent_id)
        await chan.close()
        return

    phone = normalize_caller(pending.caller)
    origin = caller_origin(phone, settings.telephony_secret)

    per_caller = agent.telephony.max_daily_calls_per_caller
    if phone and per_caller > 0:
        try:
            calls = await asyncio.to_thread(store.count_sessions_today, agent.id, origin)
        except Exception:
            logger.exception("count_sessions_today failed")
            await chan.close()
            return
        if calls >= per_caller:
            logger.info("Telephony per-caller daily limit reached agent=%s origin=%s", agent.id, origin)
            await chan.close()
            return

    session_ids: list[str] = []

    def on_session_created(session_id: str) -> None:
        session_ids.append(session_id)
        if phone:
            crm_supabase.set_session_caller(session_id, phone)

    chan.start()
    try:
        await live.start_session(
            chan, agent, store, settings,
            origin=origin,
            upstream=telephony_upstream(reader),
            greeting=agent.telephony.greeting or agent.greeting,
            extra_instruction=build_phone_instruction(agent, has_caller=phone is not None),
            on_session_created=on_session_created,
        )
    finally:
        for sid in session_ids:
            crm_supabase.forget_session_caller(sid)
        await chan.close()
