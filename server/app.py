"""FastAPI uygulaması: rotalar, statik widget dosyaları, WebSocket ucu.

Çalıştırma: `uvicorn server.app:app --host 0.0.0.0 --port 8090`
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from server import config, limits, live, telephony_asterisk
from server.settings import Settings, get_settings

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
WIDGET_DIR = ROOT_DIR / "widget"


class CachedStaticFiles(StaticFiles):
    """Widget dosyaları kısa süreli önbelleklenir (müşteri sitesinde hızlı yükleme)."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=300"
            # Widget her siteden <script> ile yüklenir
            response.headers["Access-Control-Allow-Origin"] = "*"
        return response


def _cors_headers(origin: str | None, agent: config.AgentConfig) -> dict[str, str]:
    headers = {"Vary": "Origin"}
    if origin and limits.origin_allowed(origin, agent):
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return headers


def _lookup_agent(agent_id: str) -> config.AgentConfig:
    try:
        return config.get_agent(agent_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Asistan bulunamadı.") from None


def create_app(settings: Settings | None = None, store=None, agents: dict | None = None) -> FastAPI:
    """Uygulama fabrikası. Testler sahte store/ayar/asistan verebilir."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = settings or get_settings()
        app.state.settings = s
        if agents is not None:
            config.set_agents(agents)
        else:
            config.set_agents(config.load_agents(s.agents_dir))
        logger.info("Loaded agents: %s", ", ".join(sorted(config.all_agents())) or "(none)")
        own_store = None
        if store is not None:
            app.state.store = store
        else:
            from server.storage import Store

            s.database_path.parent.mkdir(parents=True, exist_ok=True)
            own_store = Store(s.database_path)
            app.state.store = own_store
        audiosocket = None
        if s.telephony_enabled:
            if not s.telephony_secret:
                logger.warning("TELEPHONY_ENABLED but TELEPHONY_SECRET is empty; calls cannot be registered")
            audiosocket = telephony_asterisk.AudioSocketServer(
                host=s.audiosocket_host, port=s.audiosocket_port,
                registry=app.state.call_registry, store=app.state.store, settings=s,
            )
            try:
                await audiosocket.start()
            except OSError:
                # Port açılamazsa widget çalışmaya devam eder; telefon kanalı devre dışı kalır
                logger.exception("AudioSocket server could not start on %s:%s", s.audiosocket_host, s.audiosocket_port)
                audiosocket = None
        app.state.audiosocket = audiosocket
        try:
            yield
        finally:
            if audiosocket is not None:
                await audiosocket.stop()
            if own_store is not None and hasattr(own_store, "close"):
                own_store.close()

    app = FastAPI(title="Botfusions Voice Agent", lifespan=lifespan, docs_url=None, redoc_url=None)
    # Asterisk'in kaydettiği, AudioSocket bağlantısı bekleyen çağrılar (uuid → asistan, arayan)
    app.state.call_registry = telephony_asterisk.CallRegistry()

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/api/agents/{agent_id}")
    async def agent_info(agent_id: str, request: Request) -> JSONResponse:
        agent = _lookup_agent(agent_id)
        return JSONResponse(config.public_view(agent), headers=_cors_headers(request.headers.get("origin"), agent))

    @app.options("/api/agents/{agent_id}")
    async def agent_info_preflight(agent_id: str, request: Request) -> Response:
        agent = _lookup_agent(agent_id)
        headers = _cors_headers(request.headers.get("origin"), agent)
        headers["Access-Control-Max-Age"] = "600"
        return Response(status_code=204, headers=headers)

    @app.websocket("/ws/live/{agent_id}")
    async def ws_live(websocket: WebSocket, agent_id: str) -> None:
        await live.handle_live(websocket, agent_id, websocket.app.state.store, websocket.app.state.settings)

    @app.post("/telephony/asterisk/call")
    async def telephony_register(request: Request) -> Response:
        return await telephony_asterisk.handle_register(request)

    @app.get("/demo/{agent_id}", response_class=HTMLResponse)
    async def demo(agent_id: str, request: Request) -> HTMLResponse:
        _lookup_agent(agent_id)
        demo_file = WIDGET_DIR / "demo.html"
        if not demo_file.is_file():
            raise HTTPException(status_code=404, detail="Demo sayfası bulunamadı.")
        base_url = request.app.state.settings.public_base_url.rstrip("/")
        html = demo_file.read_text(encoding="utf-8")
        html = html.replace("{{AGENT_ID}}", agent_id).replace("{{BASE_URL}}", base_url)
        return HTMLResponse(html)

    app.mount("/widget", CachedStaticFiles(directory=WIDGET_DIR, check_dir=False), name="widget")

    try:
        from server import admin

        app.include_router(admin.router)
    except ModuleNotFoundError as exc:
        # Yalnızca admin modülünün kendisi eksikse atla; iç import hataları yüzeye çıksın
        if exc.name not in {"server.admin"}:
            raise
        logger.warning("server.admin not available; /admin routes disabled")

    return app


app = create_app()
