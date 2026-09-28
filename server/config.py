"""Asistan yapılandırması (AgentConfig) şeması ve YAML yükleyici.

Her asistan `agents/<id>.yaml` dosyasında tanımlanır; dosya adı id ile aynı olmalıdır.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Annotated, Any, Literal, Union
from urllib.parse import urlsplit

import yaml
from pydantic import (BaseModel, ConfigDict, Discriminator, Field, HttpUrl, Tag, field_validator,
                      model_validator)

logger = logging.getLogger(__name__)

AGENT_ID_RE = re.compile(r"^[a-z0-9-]{2,40}$")
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
PARAM_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
MAX_KNOWLEDGE_CHARS = 20_000


class _Strict(BaseModel):
    # YAML'daki yazım hatalarını sessizce yutmamak için bilinmeyen alanlar hata verir
    model_config = ConfigDict(extra="forbid")


class ToolParam(_Strict):
    """Araç parametresi (JSON Schema alt kümesi)."""

    type: Literal["string", "number", "integer", "boolean"]
    description: str = ""
    enum: list[str] | None = None
    required: bool = True


class WebhookTool(_Strict):
    type: Literal["webhook"] = "webhook"
    name: str
    description: str
    parameters: dict[str, ToolParam] = Field(default_factory=dict)
    url: HttpUrl
    method: Literal["POST", "GET"] = "POST"
    timeout_s: float = Field(default=8.0, ge=1, le=30)
    secret_env: str | None = None
    speak_while_running: bool = True

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not TOOL_NAME_RE.match(v):
            raise ValueError("araç adı ^[a-z][a-z0-9_]{1,40}$ biçiminde olmalı")
        return v

    @field_validator("description")
    @classmethod
    def _check_description(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("araç açıklaması boş olamaz")
        return v.strip()

    @field_validator("parameters")
    @classmethod
    def _check_params(cls, v: dict[str, ToolParam]) -> dict[str, ToolParam]:
        for key in v:
            if not PARAM_NAME_RE.match(key):
                raise ValueError(f"geçersiz parametre adı: {key!r}")
        return v

    @field_validator("secret_env")
    @classmethod
    def _check_secret_env(cls, v: str | None) -> str | None:
        if v is not None and not ENV_NAME_RE.match(v):
            raise ValueError("secret_env bir ortam değişkeni ADI olmalı (ör. BOOK_DEMO_SECRET)")
        return v


def _default_crm_params() -> dict[str, "ToolParam"]:
    return {
        "name": ToolParam(type="string", description="Kişinin adı soyadı"),
        "email": ToolParam(type="string", description="E-posta adresi (e-posta veya telefondan en az biri)", required=False),
        "phone": ToolParam(type="string", description="Telefon numarası (e-posta veya telefondan en az biri)", required=False),
        "date": ToolParam(type="string", description="Görüşme tarihi, YYYY-MM-DD (ör. 2026-10-02)"),
        "time": ToolParam(type="string", description="Görüşme saati, 24 saat HH:MM (ör. 14:30)"),
        "topic": ToolParam(type="string", description="Görüşmenin konusu, kısa (ör. 'Web sitesi için sesli asistan')"),
        "note": ToolParam(type="string", description="Kişinin ihtiyacına dair kısa not", required=False),
    }


class SupabaseCrmTool(_Strict):
    """Yerleşik CRM aracı: adayı Supabase'deki CRM tablolarına (BOTCRm) yazar.

    Anahtarlar YAML'a yazılmaz; yalnızca ortam değişkeni ADLARI verilir.
    Service role anahtarı yalnızca sunucuda kalır, istemciye asla gitmez.
    """

    type: Literal["supabase_crm"]
    name: str = "book_demo"
    description: str = (
        "Kişi için Botfusions ekibiyle demo / tanışma görüşmesi talebi oluşturur ve CRM'e kaydeder. "
        "Yalnızca kullanıcı adını, e-posta veya telefonunu ve tercih ettiği zamanı verip onayladıktan sonra çağır."
    )
    parameters: dict[str, ToolParam] = Field(default_factory=_default_crm_params)
    url_env: str = "SUPABASE_URL"
    key_env: str = "SUPABASE_SERVICE_ROLE_KEY"
    leads_table: str = "crm_leads"
    tasks_table: str | None = "crm_tasks"       # None → görev açılmaz
    lead_source: str = "Sesli Asistan"
    lead_status: str = "Meeting Scheduled"
    lead_tags: list[str] = Field(default_factory=lambda: ["sesli-asistan"])
    task_assigned_to: str = "Sesli Asistan"
    notes_column: str | None = None               # ör. "notes": oturum özeti bu kolona eklenir
    timeout_s: float = Field(default=8.0, ge=1, le=30)
    speak_while_running: bool = False             # CRM kaydı kesin onay ister: sonuç beklenir

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not TOOL_NAME_RE.match(v):
            raise ValueError("araç adı ^[a-z][a-z0-9_]{1,40}$ biçiminde olmalı")
        return v

    @field_validator("url_env", "key_env")
    @classmethod
    def _check_env(cls, v: str) -> str:
        if not ENV_NAME_RE.match(v):
            raise ValueError("url_env/key_env bir ortam değişkeni ADI olmalı")
        return v

    @field_validator("leads_table", "tasks_table", "notes_column")
    @classmethod
    def _check_ident(cls, v: str | None) -> str | None:
        if v is not None and not PARAM_NAME_RE.match(v):
            raise ValueError(f"geçersiz tablo/kolon adı: {v!r}")
        return v


def _tool_kind(value: Any) -> str:
    if isinstance(value, dict):
        return value.get("type", "webhook")
    return getattr(value, "type", "webhook")


AnyTool = Annotated[
    Union[Annotated[WebhookTool, Tag("webhook")], Annotated[SupabaseCrmTool, Tag("supabase_crm")]],
    Discriminator(_tool_kind),
]


class Limits(_Strict):
    max_session_seconds: int = Field(default=600, ge=10, le=3600)
    max_daily_sessions: int = Field(default=200, ge=0)
    max_daily_minutes: int = Field(default=300, ge=0)


class Theme(_Strict):
    title: str = "Sesli Asistan"
    primary_color: str = "#A855F7"
    position: Literal["bottom-right", "bottom-left"] = "bottom-right"

    @field_validator("primary_color")
    @classmethod
    def _check_color(cls, v: str) -> str:
        if not COLOR_RE.match(v):
            raise ValueError("primary_color #RRGGBB biçiminde olmalı")
        return v


class Telephony(_Strict):
    """Telefon kanalı (Asterisk AudioSocket) ayarları; bölüm yoksa kanal kapalıdır."""

    enabled: bool = False
    greeting: str | None = None                   # yoksa agent.greeting kullanılır
    # Aynı numaradan günlük en fazla arama; 0 → arayan başına sınır yok
    # (genel günlük limitler her durumda geçerlidir)
    max_daily_calls_per_caller: int = Field(default=5, ge=0)
    instructions: str | None = None               # yalnızca telefon kanalına eklenen talimat


class Learning(_Strict):
    """Öğrenme döngüsü (server/learning.py): görüşme sonrası ders/hata çıkarıp talimata ekler."""

    enabled: bool = False
    max_items: int = Field(default=30, ge=1, le=100)   # dosya başına tutulan madde


class AgentConfig(_Strict):
    id: str
    name: str
    language: str = "tr-TR"
    voice: str = "Kore"
    model: str | None = None
    instructions: str
    greeting: str | None = None
    knowledge: str | None = None
    tools: list[AnyTool] = Field(default_factory=list)
    allowed_origins: list[str] = Field(default_factory=list)
    limits: Limits = Field(default_factory=Limits)
    theme: Theme = Field(default_factory=Theme)
    telephony: Telephony = Field(default_factory=Telephony)
    learning: Learning = Field(default_factory=Learning)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not AGENT_ID_RE.match(v):
            raise ValueError("id ^[a-z0-9-]{2,40}$ biçiminde olmalı")
        return v

    @field_validator("instructions")
    @classmethod
    def _check_instructions(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("instructions boş olamaz")
        return v.strip()

    @field_validator("knowledge")
    @classmethod
    def _check_knowledge(cls, v: str | None) -> str | None:
        if v is not None and len(v) > MAX_KNOWLEDGE_CHARS:
            raise ValueError(f"knowledge en fazla {MAX_KNOWLEDGE_CHARS} karakter olabilir")
        return v

    @field_validator("allowed_origins")
    @classmethod
    def _check_origins(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for item in v:
            norm = normalize_origin(item)
            if norm is None:
                raise ValueError(f"geçersiz origin: {item!r} (ör. https://ornek.com)")
            out.append(norm)
        return out

    @model_validator(mode="after")
    def _check_unique_tools(self) -> "AgentConfig":
        names = [t.name for t in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("araç adları benzersiz olmalı")
        return self


def normalize_origin(value: str | None) -> str | None:
    """Origin'i `scheme://host[:port]` biçimine indirger (küçük harf, varsayılan port atılır).

    Geçersizse None döner.
    """
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        return None
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        return None
    if ":" in host:  # IPv6 adresi köşeli parantezle yazılır
        host = f"[{host}]"
    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


class AgentConfigError(ValueError):
    """YAML dosyası geçersiz."""


def load_agent_file(path: Path) -> AgentConfig:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AgentConfigError(f"{path.name}: YAML okunamadı: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentConfigError(f"{path.name}: kök öğe bir sözlük olmalı")
    try:
        agent = AgentConfig.model_validate(data)
    except ValueError as exc:
        raise AgentConfigError(f"{path.name}: {exc}") from exc
    if agent.id != path.stem:
        raise AgentConfigError(f"{path.name}: dosya adı id ile aynı olmalı (id={agent.id!r})")
    return agent


def load_agents(dir: Path) -> dict[str, AgentConfig]:  # noqa: A002 (SPEC imzası)
    """Klasördeki tüm *.yaml / *.yml dosyalarını yükler; hatalı dosya varsa istisna fırlatır."""
    directory = Path(dir)
    agents: dict[str, AgentConfig] = {}
    if not directory.is_dir():
        logger.warning("Agents directory not found: %s", directory)
        return agents
    files = sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")])
    for path in files:
        agent = load_agent_file(path)
        if agent.id in agents:
            raise AgentConfigError(f"{path.name}: aynı id iki kez tanımlı ({agent.id})")
        agents[agent.id] = agent
    return agents


# --- Süreç içi kayıt defteri -------------------------------------------------

_registry: dict[str, AgentConfig] | None = None
_lock = threading.Lock()


def set_agents(agents: dict[str, AgentConfig]) -> None:
    """Kayıt defterini doğrudan ayarla (uygulama açılışı ve testler)."""
    global _registry
    with _lock:
        _registry = dict(agents)


def reload_agents() -> dict[str, AgentConfig]:
    from server.settings import get_settings

    agents = load_agents(get_settings().agents_dir)
    set_agents(agents)
    return agents


def all_agents() -> dict[str, AgentConfig]:
    if _registry is None:
        reload_agents()
    return dict(_registry or {})


def get_agent(agent_id: str) -> AgentConfig:
    """Asistanı döndürür; yoksa KeyError (HTTP katmanında 404)."""
    agents = all_agents()
    if agent_id not in agents:
        raise KeyError(agent_id)
    return agents[agent_id]


def public_view(agent: AgentConfig) -> dict:
    """Widget'a gönderilebilecek güvenli alt küme (talimat, araç, bilgi metni YOK)."""
    return {
        "id": agent.id,
        "name": agent.name,
        "greeting": agent.greeting,
        "theme": agent.theme.model_dump(),
        "language": agent.language,
    }
