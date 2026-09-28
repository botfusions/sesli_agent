"""Oturum sonu özet üretimi (google-genai, metin modeli).

`summarize()` asla istisna fırlatmaz; herhangi bir hata, zaman aşımı veya boş girdi → "".
Doğrulanan API (google-genai 2.x): `genai.Client(api_key=... | vertexai=True, project=..., location=...)`
ve `await client.aio.models.generate_content(model=..., contents=..., config=types.GenerateContentConfig(...))`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from server.redact import redact

__all__ = ["summarize", "TIMEOUT_S"]

log = logging.getLogger(__name__)

TIMEOUT_S = 20.0
MAX_TRANSCRIPT_CHARS = 30_000
DEFAULT_SUMMARY_MODEL = "gemini-3.8-flash"

_PROMPT_TR = (
    "Aşağıda bir müşteri ile sesli asistan arasındaki görüşmenin transkripti var. "
    "Görüşmeyi Türkçe olarak 3-5 kısa madde halinde özetle. Maddeler sırasıyla şunları kapsasın: "
    "görüşmenin konusu, müşterinin talebi, sonuç ve varsa sonraki adım. "
    "Kişisel veri YAZMA: isim, telefon, e-posta, adres, kimlik veya kart numarası gibi bilgileri "
    "özete ekleme. Her madde '- ' ile başlasın; başlık, giriş veya kapanış cümlesi ekleme."
)

_PROMPT_OTHER = (
    "Below is the transcript of a conversation between a customer and a voice assistant. "
    "Summarize it in 3-5 short bullet points written in the language '{language}': topic, "
    "customer request, outcome and next step if any. Do NOT include personal data (names, phone "
    "numbers, e-mails, addresses, ID or card numbers). Start each bullet with '- ' and add nothing else."
)

_ROLE_LABELS = {"user": "Müşteri", "agent": "Asistan"}

# Ortam değişkeni adı → server.settings.Settings alan adı (farklı olanlar)
_SETTINGS_ALIASES = {"GOOGLE_GENAI_USE_VERTEXAI": "use_vertexai"}


def _setting(name: str, default: Any = None) -> Any:
    """Ayarı önce server.settings'ten (Ajan A), yoksa ortam değişkeninden okur."""
    try:
        from server.settings import get_settings  # type: ignore

        s = get_settings()
        for attr in (_SETTINGS_ALIASES.get(name, name.lower()), name):
            if hasattr(s, attr):
                val = getattr(s, attr)
                if val not in (None, ""):
                    return val
    except Exception:  # settings henüz yok veya hatalı → ortam değişkeni
        pass
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


def _make_client():
    """google-genai istemcisi; anahtar/Vertex yapılandırması yoksa None."""
    from google import genai
    from google.genai import types

    http_options = types.HttpOptions(timeout=int(TIMEOUT_S * 1000))  # milisaniye
    if _truthy(_setting("GOOGLE_GENAI_USE_VERTEXAI", False)):
        return genai.Client(
            vertexai=True,
            project=_setting("GOOGLE_CLOUD_PROJECT"),
            # Metin modelleri (ör. gemini-3.8-flash) Vertex'te çoğu zaman yalnızca "global" bölgededir;
            # canlı ses modeli ise bölgesel (us-central1). Bu yüzden özet için ayrı bölge ayarı var.
            location=os.environ.get("SUMMARY_LOCATION") or "global",
            http_options=http_options,
        )
    api_key = _setting("GEMINI_API_KEY") or _setting("GOOGLE_API_KEY")
    if not api_key:
        return None
    return genai.Client(api_key=api_key, http_options=http_options)


def _format_transcript(transcript: list[dict]) -> str:
    lines: list[str] = []
    for item in transcript or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        role = _ROLE_LABELS.get(str(item.get("role") or ""), str(item.get("role") or "?"))
        # Modele gitmeden önce de maskele (depodan gelmeyen transkriptler için)
        lines.append(f"{role}: {redact(text)}")
    joined = "\n".join(lines)
    if len(joined) > MAX_TRANSCRIPT_CHARS:
        # Uzun görüşmelerde son kısım (sonuç) daha değerli
        joined = joined[-MAX_TRANSCRIPT_CHARS:]
    return joined


async def summarize(transcript: list[dict], language: str = "tr") -> str:
    """Transkriptten 3–5 maddelik özet üretir. Hata/boş → ""."""
    try:
        body = _format_transcript(transcript)
        if not body:
            return ""
        client = _make_client()
        if client is None:
            log.warning("summary skipped: no GEMINI_API_KEY / Vertex configuration")
            return ""

        from google.genai import types

        lang = (language or "tr").lower()
        instruction = _PROMPT_TR if lang.startswith("tr") else _PROMPT_OTHER.format(language=language)
        config = types.GenerateContentConfig(
            system_instruction=instruction,
            temperature=0.2,
            # 3.x modelleri çıktı bütçesinin bir kısmını "düşünme"ye harcar; 600'de özet yarıda kesiliyordu
            max_output_tokens=2048,
        )
        model = _setting("SUMMARY_MODEL", DEFAULT_SUMMARY_MODEL)
        response = await asyncio.wait_for(
            client.aio.models.generate_content(model=model, contents=body, config=config),
            timeout=TIMEOUT_S,
        )
        text = getattr(response, "text", None) or ""
        text = text.strip()
        return redact(text) if text else ""
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # zaman aşımı dahil her hata → ""
        log.warning("summary failed: %s: %s", type(exc).__name__, exc)
        return ""
