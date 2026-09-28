"""Botfusions Voice Agent — müşteri tarafı webhook alıcısı (örnek).

Bu dosya tek başına çalışır; Botfusions sunucu koduna bağımlı değildir.
Sesli asistan `book_demo` aracını çağırdığında bu uç noktaya imzalı bir POST gelir.

Çalıştırma:
    pip install fastapi uvicorn
    export BOTFUSIONS_WEBHOOK_SECRET="asistan YAML'ındaki secret_env ile aynı değer"
    uvicorn webhook_receiver:app --port 9000

Asistan YAML'ında örnek araç tanımı:
    tools:
      - name: book_demo
        description: Müşteri için demo toplantısı planlar.
        url: https://sizin-sunucunuz.com/webhooks/book_demo
        secret_env: BOTFUSIONS_WEBHOOK_SECRET
        parameters:
          name:  {type: string, description: Ad soyad}
          phone: {type: string, description: Telefon numarası}
          date:  {type: string, description: "Tercih edilen tarih (YYYY-AA-GG)"}

Gelen istek:
    Başlıklar:
        X-Botfusions-Timestamp: <unix saniye>
        X-Botfusions-Signature: sha256=<hex>
    Gövde (JSON):
        {"tool": "book_demo", "args": {...}, "session_id": "...", "agent_id": "...", "ts": 1700000000}

İmza = HMAC-SHA256(secret, f"{timestamp}.".encode() + ham_gövde_baytları)
Önemli: İmzayı JSON'u ayrıştırıp yeniden serileştirerek DEĞİL, gelen ham baytlar
üzerinden doğrulayın; aksi hâlde boşluk/sıra farkları imzayı bozar.

Yanıt: JSON döndürün (≤ 64 KB). Asistan bu yanıtı okuyup kullanıcıya sesli aktarır;
bu yüzden kısa, anlaşılır bir `message` alanı eklemek iyi bir pratiktir.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi import FastAPI, HTTPException, Request

# İmzanın kabul edileceği en büyük zaman farkı (saniye). Tekrar (replay)
# saldırılarını sınırlamak için 5 dakika.
TOLERANCE_S = 300

app = FastAPI(title="Botfusions webhook alıcısı (örnek)")


def verify_signature(body: bytes, signature: str, secret: str, timestamp: str) -> bool:
    """Botfusions imzasını doğrular. Geçerliyse True döner."""
    if not (body is not None and signature and secret and timestamp):
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    # Çok eski (ya da saat farkı yüzünden çok ileri) istekleri reddet.
    if abs(time.time() - ts) > TOLERANCE_S:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    # Zamanlama saldırılarına karşı sabit süreli karşılaştırma.
    return hmac.compare_digest(expected.encode(), signature.strip().encode())


@app.post("/webhooks/book_demo")
async def book_demo(request: Request) -> dict:
    secret = os.environ.get("BOTFUSIONS_WEBHOOK_SECRET", "")
    if not secret:
        # Anahtar tanımlı değilse hiçbir isteği kabul etme.
        raise HTTPException(status_code=500, detail="secret not configured")

    body = await request.body()  # ham baytlar — imza bunun üzerinden
    ok = verify_signature(
        body,
        request.headers.get("X-Botfusions-Signature", ""),
        secret,
        request.headers.get("X-Botfusions-Timestamp", ""),
    )
    if not ok:
        raise HTTPException(status_code=401, detail="invalid signature")

    payload = await request.json()
    args = payload.get("args", {})

    # Burada kendi iş mantığınızı çalıştırın: CRM'e kayıt, takvime ekleme vb.
    # Aynı session_id ile gelen tekrar çağrıları tekilleştirmek isteyebilirsiniz.
    name = args.get("name", "")
    date = args.get("date", "")

    return {
        "booked": True,
        "message": f"{name} için {date} tarihine demo kaydı oluşturuldu.",
    }
