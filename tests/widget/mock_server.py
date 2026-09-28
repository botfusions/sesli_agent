"""Widget testi için sahte sunucu.

SPEC'teki WebSocket protokolünü taklit eder; gerçek Gemini/ADK kullanılmaz.
- /widget/*            → widget klasörü (statik)
- /demo/{agent_id}     → demo.html ({{BASE_URL}}, {{AGENT_ID}} yerleştirilmiş)
- /api/agents/{id}     → public_view benzeri JSON
- /ws/live/{id}        → start→ready, gelen ikili parçaları sayar, text/end kaydeder
- /__state             → test için kayıtlı istatistikler
- /__push/{senaryo}    → etkin bağlantıya senaryo gönderir (speak, interrupt, tool, drop, limit)

Çalıştırma: python tests/widget/mock_server.py --port 8765
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import struct
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[2]
WIDGET_DIR = ROOT / "widget"

AGENTS = {
    "demo-agent": {
        "id": "demo-agent",
        "name": "Botfusions Satış",
        "greeting": "Merhaba! Size nasıl yardımcı olabilirim?",
        "language": "tr-TR",
        "theme": {"title": "Botfusions Asistan", "primary_color": "#A855F7", "position": "bottom-right"},
    }
}

app = FastAPI()


class State:
    """Testin sorgulayacağı sayaçlar."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.connections = 0
        self.starts = 0
        self.binary_count = 0
        self.binary_sizes: dict[int, int] = {}
        self.texts: list[str] = []
        self.ends = 0
        self.first_message_types: list[str] = []
        self.closed = 0
        self.ws: WebSocket | None = None


STATE = State()


def sine_pcm24k(seconds: float, freq: float = 440.0) -> bytes:
    """24 kHz mono PCM16 LE sinüs sesi üretir."""
    n = int(24000 * seconds)
    return b"".join(
        struct.pack("<h", int(0.25 * 32767 * math.sin(2 * math.pi * freq * i / 24000))) for i in range(n)
    )


SINE_3S = sine_pcm24k(3.0)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/agents/{agent_id}")
async def agent_view(agent_id: str):
    if agent_id not in AGENTS:
        raise HTTPException(404, "not found")
    return JSONResponse(AGENTS[agent_id], headers={"Access-Control-Allow-Origin": "*"})


@app.get("/demo/{agent_id}", response_class=HTMLResponse)
async def demo(agent_id: str):
    html = (WIDGET_DIR / "demo.html").read_text(encoding="utf-8")
    base = app.state.base_url
    return html.replace("{{BASE_URL}}", base).replace("{{AGENT_ID}}", agent_id)


@app.get("/__state")
async def get_state():
    return {
        "connections": STATE.connections,
        "starts": STATE.starts,
        "binary_count": STATE.binary_count,
        "binary_sizes": {str(k): v for k, v in STATE.binary_sizes.items()},
        "texts": STATE.texts,
        "ends": STATE.ends,
        "first_message_types": STATE.first_message_types,
        "closed": STATE.closed,
        "active": STATE.ws is not None,
    }


async def _send_json(ws: WebSocket, obj: dict) -> None:
    await ws.send_text(json.dumps(obj, ensure_ascii=False))


@app.post("/__push/{scenario}")
async def push(scenario: str):
    ws = STATE.ws
    if ws is None:
        raise HTTPException(409, "no active websocket")
    if scenario == "speak":
        # Kullanıcı konuşması (kısmi → final), asistan yanıtı ve 3 sn ses
        await _send_json(ws, {"type": "transcript", "role": "user", "text": "Merhaba, fiyat", "final": False})
        await _send_json(ws, {"type": "transcript", "role": "user", "text": "Merhaba, fiyatlarınızı öğrenebilir miyim?", "final": True})
        await _send_json(ws, {"type": "transcript", "role": "agent", "text": "Tabii, paketlerimiz", "final": False})
        for i in range(0, len(SINE_3S), 4800):  # 100 ms'lik parçalar
            await ws.send_bytes(SINE_3S[i : i + 4800])
        await _send_json(ws, {"type": "transcript", "role": "agent", "text": "Tabii, paketlerimiz aylık 990 TL'den başlıyor.", "final": False})
    elif scenario == "interrupt":
        await _send_json(ws, {"type": "interrupted"})
        await _send_json(ws, {"type": "turn_complete"})
    elif scenario == "tool":
        await _send_json(ws, {"type": "tool", "name": "randevu_olustur", "status": "start"})
        await asyncio.sleep(0.1)
        await _send_json(ws, {"type": "tool", "name": "randevu_olustur", "status": "ok", "summary": "Randevu yarın 14:00 için oluşturuldu"})
    elif scenario == "drop":
        # Beklenmedik kopma: widget bir kez otomatik yeniden bağlanmalı
        await ws.close(code=1011)
    elif scenario == "limit":
        await _send_json(ws, {"type": "limit", "reason": "session_time"})
        await ws.close(code=1000)
    else:
        raise HTTPException(400, "unknown scenario")
    return {"ok": True}


@app.post("/__reset")
async def reset():
    STATE.reset()
    return {"ok": True}


@app.websocket("/ws/live/{agent_id}")
async def live(ws: WebSocket, agent_id: str):
    await ws.accept()
    STATE.connections += 1
    if agent_id not in AGENTS:
        await _send_json(ws, {"type": "error", "code": "not_found", "message": "Asistan bulunamadı."})
        await ws.close(code=1008)
        return
    first = True
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                if first:
                    STATE.first_message_types.append("binary")
                    first = False
                size = len(msg["bytes"])
                STATE.binary_count += 1
                STATE.binary_sizes[size] = STATE.binary_sizes.get(size, 0) + 1
                continue
            data = json.loads(msg.get("text") or "{}")
            if first:
                STATE.first_message_types.append(data.get("type", "?"))
                first = False
            kind = data.get("type")
            if kind == "start":
                STATE.starts += 1
                STATE.ws = ws
                await _send_json(ws, {"type": "ready", "session_id": f"sess{STATE.starts}", "agent": AGENTS[agent_id]})
            elif kind == "text":
                STATE.texts.append(data.get("text", ""))
                await _send_json(ws, {"type": "transcript", "role": "agent", "text": "Yazılı mesajınızı aldım.", "final": True})
                await _send_json(ws, {"type": "turn_complete"})
            elif kind == "end":
                STATE.ends += 1
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        STATE.closed += 1
        if STATE.ws is ws:
            STATE.ws = None
        try:
            await ws.close()
        except RuntimeError:
            pass


app.mount("/widget", StaticFiles(directory=WIDGET_DIR), name="widget")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    app.state.base_url = f"http://127.0.0.1:{args.port}"
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
