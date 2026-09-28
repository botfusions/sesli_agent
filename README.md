# Botfusions Voice Agent

Müşteri sitesine **tek satırla** eklenen, Türkçe öncelikli, gerçek zamanlı **sesli AI asistan**.
Google Gemini Live + Agent Development Kit (ADK) üzerinde çalışır. Her müşteri için kod yazmadan,
bir YAML dosyasıyla yeni asistan kurulur.

> Durum: MVP. Gerçek Gemini anahtarıyla uçtan uca ses testi henüz yapılmadı (aşağıdaki "İlk çalıştırma" bölümü).

## Neler var?
- **Widget:** Sağ altta mikrofon düğmesi; sesli konuşma, asistanın sözünü kesme (barge-in), canlı transkript, yazılı mesaj, mobil uyumlu. Mikrofon izni yoksa yazılı moda düşer.
- **Asistan yapılandırması (`agents/*.yaml`):** talimat, bilgi metni, ses, dil, karşılama cümlesi, izinli alan adları, limitler, tema rengi.
- **Araçlar (webhook):** Asistan konuşurken müşterinin sistemine HMAC imzalı HTTP çağrısı yapar (ör. randevu). Araç çalışırken asistan konuşmaya devam edebilir.
- **Güvenlik:** API anahtarı yalnızca sunucuda; alan adı (origin) kontrolü; oturum süresi ve günlük kota; kayıtlarda telefon, e-posta, TCKN ve kart numarası maskelenir.
- **Yönetim:** `/admin` sayfasında oturum listesi, transkript, otomatik özet, token kullanımı ve tahmini maliyet.

## Mimari
```
Tarayıcı (widget/voice-agent.js)
  │  WebSocket /ws/live/{asistan}: 16 kHz PCM ses ↑  ·  24 kHz PCM ses ↓  ·  JSON olaylar
  ▼
FastAPI (server/app.py → live.py)
  │  ADK Runner.run_live + LiveRequestQueue
  ▼
Gemini Live modeli ──► araç çağrısı ──► server/tools.py ──► müşterinin webhook'u
  │
  └─► SQLite (server/storage.py) ──► oturum sonu özet (summary.py) ──► /admin
```
Protokolün ve modül arayüzlerinin tamamı: [SPEC.md](SPEC.md).

## İlk çalıştırma (kendi bilgisayarınızda)
Gerekenler: Python 3.11+, bir Gemini API anahtarı ([Google AI Studio](https://aistudio.google.com/apikey)).

```bash
git clone https://github.com/botfusions/sesli_agent.git
cd sesli_agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env` dosyasını açıp **yalnızca kendi bilgisayarınızda** doldurun (anahtarı hiçbir yere yapıştırmayın, git'e eklemeyin; `.env` zaten `.gitignore`'da):
```
GEMINI_API_KEY=buraya-anahtar
ADMIN_TOKEN=uzun-rastgele-bir-deger
```

Örnek asistan yalnızca botfusions.com'dan bağlantı kabul eder. Lokal deneme için `agents/botfusions-satis.yaml` içindeki `allowed_origins` listesine geçici olarak `http://localhost:8090` ekleyin, sonra:

```bash
set -a; source .env; set +a
uvicorn server.app:app --port 8090
```

- Demo sayfası: http://localhost:8090/demo/botfusions-satis → sağ alttaki düğmeye basıp konuşun.
- Yönetim: http://localhost:8090/admin?token=ADMIN_TOKEN_DEĞERİ

**Model adı:** `LIVE_MODEL` varsayılanı `gemini-2.5-flash-native-audio-preview-09-2025`. Hesabınızda bu ad yoksa demo sayfasında hata görürsünüz; [model listesinden](https://ai.google.dev/gemini-api/docs/models) erişebildiğiniz canlı ses modelinin adını `.env`'ye yazın.

## Docker
```bash
cp .env.example .env   # doldurun
docker compose up -d --build
```
Kayıtlar `./data`, asistan dosyaları `./agents` klasöründe kalır (imajı yeniden almadan YAML değiştirilebilir, sunucu yeniden başlatılır).

## Müşteri sitesine ekleme
```html
<script src="https://ASISTAN-SUNUCUNUZ/widget/voice-agent.js" data-agent="botfusions-satis" defer></script>
```
- Sunucu **HTTPS** üzerinde olmalı (mikrofon yalnızca HTTPS'te çalışır).
- Müşteri alan adı asistanın `allowed_origins` listesinde olmalı.

## Yeni asistan kurmak
1. `agents/botfusions-satis.yaml` dosyasını kopyalayın; dosya adı ile `id` aynı olmalı (ör. `agents/klinik-x.yaml` → `id: klinik-x`).
2. `instructions`, `knowledge`, `greeting`, `allowed_origins`, `theme` alanlarını düzenleyin.
3. Araç gerekiyorsa `tools` altına webhook tanımlayın; imza anahtarını `.env`'de `secret_env` adıyla verin.
4. Sunucuyu yeniden başlatın.

Bilgi metnine **yalnızca doğrulanmış** bilgi yazın; talimat, bilmediği konuda uydurmamasını söyler.

## Webhook alıcısı (müşteri tarafı)
`examples/webhook_receiver.py` imza doğrulayan bağımsız bir FastAPI örneğidir. Asistan her çağrıda şunu gönderir:
```
POST <url>
X-Botfusions-Timestamp: <unix saniye>
X-Botfusions-Signature: sha256=<HMAC-SHA256(secret, "<timestamp>." + gövde)>
{"tool": "book_demo", "args": {...}, "session_id": "...", "agent_id": "...", "ts": ...}
```
5 dakikadan eski istekleri reddedin. Örnek YAML'daki `https://example.invalid/...` adresi yer tutucudur; kendi webhook adresinizle değiştirin (n8n webhook'u da olabilir).

## Testler
```bash
python3 -m pytest -q            # 143 test, ağ gerekmez (Gemini ve webhook'lar sahte)
bash tests/widget/run.sh        # widget: Playwright + sahte sunucu (Chromium gerekir)
```

## Bilinen sınırlar ve riskler
- **Gerçek anahtarla denenmedi:** model adı, token sayımının doğruluğu (birikimli olabilir), araç çalışırken konuşma (NON_BLOCKING) ve karşılama cümlesinin birebir söylenmesi ilk canlı testte kontrol edilmeli.
- **Maliyet:** `/admin`'deki maliyet `server/admin.py` üstündeki `COST_PER_1M` sabitinden tahmindir; kendi faturanızla güncelleyin.
- **Tek sunucu:** Çağrı sayaçları ve ADK oturumları bellekte; birden fazla worker/sunucu için paylaşılan depo gerekir.
- **Kötüye kullanım:** IP başına oturum sınırı yok; günlük kota maliyeti sınırlar ama tek kullanıcı kotayı tüketebilir.
- **Kapsam dışı:** telefon hattı (Twilio/SIP), kamera/ekran paylaşımı, çok kiracılı panel, ödeme.
- **Tarayıcılar:** Chromium'da test edildi; Safari/iOS ve Firefox gerçek cihazda denenmedi.
- ADK'nın canlı API'si deneysel olarak işaretli.

## Lisans ve atıf
Bu kod Botfusions için sıfırdan yazıldı. [emrahmete/gemini-live-voicebot-studio](https://github.com/emrahmete/gemini-live-voicebot-studio) mimari fikir kaynağıdır; oradan kod kopyalanmadı (o repo lisanssızdır).
