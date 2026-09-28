# Botfusions Voice Agent

Müşteri sitesine **tek satırla** eklenen, Türkçe öncelikli, gerçek zamanlı **sesli AI asistan**.
Google Gemini Live + Agent Development Kit (ADK) üzerinde çalışır. Her müşteri için kod yazmadan,
bir YAML dosyasıyla yeni asistan kurulur.

> Durum: MVP. 28-09-2026'da gerçek anahtarla uçtan uca test edildi (metin girişli sesli sohbet, randevu aracı, kayıt, özet). Mikrofonla gerçek tarayıcı testi henüz yapılmadı.

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

**Modeller (28-09-2026'da gerçek anahtarla test edildi):**

| Kullanım | Model | AI Studio | Vertex (us-central1) | İlk ses gecikmesi |
|---|---|---|---|---|
| Canlı ses (varsayılan) | `gemini-3.8-live` | ✅ | ✅ | ~1,3 sn |
| Canlı ses (yedek) | `gemini-2.5-flash-native-audio-preview-09-2025` / Vertex: `gemini-live-2.5-flash-native-audio` | ✅ | ✅ | ~1,5–2,5 sn |
| Özet | `gemini-3.8-flash` | ✅ | ✅ (yalnızca `global` bölge → `SUMMARY_LOCATION=global`) | — |

**Vertex AI ile (servis hesabı):** `.env`'de `GOOGLE_GENAI_USE_VERTEXAI=true`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION=us-central1` ve `GOOGLE_APPLICATION_CREDENTIALS=/yol/servis-hesabi.json` verin. Anahtarın hangi API'lere izinli olduğu önemlidir: Vertex *express* modunda kullanılan anahtarlar metinde çalışıp canlı seste "Invalid resource field value" hatası verebilir. Canlı ses için Gemini API'ye (AI Studio) izinli bir anahtar ya da servis hesabı kullanın.

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

## BOTCRm (Supabase) bağlantısı
Asistan randevu aldığında kaydı doğrudan BOTCRm'e yazabilir (`type: supabase_crm` aracı):
- `bots_leads`: e-postaya (yoksa telefona) göre aranır. Varsa durum/etiket güncellenir, yoksa yeni aday açılır (kaynak `Sesli Asistan`, durum `Meeting Scheduled`). Telefonlar `+90…` biçimine çevrilir.
- `bots_tasks`: adaya bağlı "Demo görüşmesi: <ad> — tercih: <zaman>" görevi açılır.
- `notes_column` verilirse (ör. `notes`) oturum sonu özeti adayın kaydına eklenir. BOTCRm'de şu an böyle bir kolon yok; açılınca etkinleştirin.

`.env`'ye ekleyin (yalnızca sunucuda kalır, istemciye gitmez):
```
SUPABASE_URL=https://<proje>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service role anahtarı>
```
Service role anahtarı RLS'i atlar; bu yüzden asla tarayıcı koduna, repoya ya da sohbete koymayın. Kayıt başarısız olursa asistan kullanıcıya bunu söyler ve hata `/admin`'de görünür.

Webhook örneği (kendi sunucunuz ya da n8n):
```yaml
tools:
  - type: webhook
    name: book_demo
    description: Demo talebi oluşturur.
    url: https://sunucunuz/webhooks/book_demo
    secret_env: BOOK_DEMO_SECRET
    parameters:
      name: {type: string, description: Ad soyad}
```

## Webhook alıcısı (müşteri tarafı)
`examples/webhook_receiver.py` imza doğrulayan bağımsız bir FastAPI örneğidir. Asistan her çağrıda şunu gönderir:
```
POST <url>
X-Botfusions-Timestamp: <unix saniye>
X-Botfusions-Signature: sha256=<HMAC-SHA256(secret, "<timestamp>." + gövde)>
{"tool": "book_demo", "args": {...}, "session_id": "...", "agent_id": "...", "ts": ...}
```
5 dakikadan eski istekleri reddedin. Örnek YAML'daki `https://example.invalid/...` adresi yer tutucudur; kendi webhook adresinizle değiştirin (n8n webhook'u da olabilir).

## Telefon hattı (Netgsm + Asterisk)
Müşteri sabit numaranızı (ör. 0850) aradığında aynı asistan telefonda cevap verir. Yalnızca **gelen arama** desteklenir.

```
Arayan ──► Netgsm (SIP trunk) ──SIP 5060/udp + RTP 10000-10100/udp──► Asterisk (asterisk/ konteyneri)
                                                                         │ 1) POST /telephony/asterisk/call
                                                                         │    uuid, caller, agent, token  → "OK"
                                                                         │ 2) AudioSocket(uuid) TCP 9092, 8 kHz slin
                                                                         ▼   (yalnızca iç Docker ağı)
                                                                  voice-agent (FastAPI) ──► Gemini Live
```
- Asterisk her aramada rastgele bir UUID üretir, arayanı `TELEPHONY_SECRET` ile voice-agent'a kaydeder; yanıt tam olarak `OK` ise sesi AudioSocket ile bağlar, değilse kapatır. Kayıt 30 sn içinde kullanılmazsa geçersiz olur.
- Asterisk yapılandırması `asterisk/conf/` altındadır; `asterisk/entrypoint.sh` açılışta şablonları `.env` değerleriyle doldurur ve eksik ayarda Türkçe hata vererek durur.
- İmaj: Ubuntu 24.04 + Asterisk 20 (paket), root olmayan `asterisk` kullanıcısıyla çalışır; yalnızca gereken modüller yüklenir (AMI, HTTP, chan_sip vb. kapalı).
- Asistanın YAML'ında `telephony: enabled: true` olmalı (örnek: `agents/botfusions-satis.yaml`).

### VPS gereksinimleri
- **Sabit genel IP** (Netgsm kaydı ve ses için). Bu IP `.env`'de `EXTERNAL_IP` olur.
- Güvenlik duvarında açılacaklar: **5060/udp** (SIP) ve **10000-10100/udp** (RTP; `RTP_START`/`RTP_END` ile aynı). Mümkünse 5060'ı yalnızca Netgsm IP'lerine açın. **9092 açılmaz** (iç ağ).
- Not: Docker, yayınlanan portlar için `ufw` kurallarını atlar; kısıtlamayı bulut sağlayıcının güvenlik duvarında veya `DOCKER-USER` zincirinde yapın.
- **Netgsm paneli:** SIP trunk için kullanıcı adı, şifre ve SIP sunucu adresini panelden alın (`NETGSM_SIP_SERVER` için bu repoda doğrulanmış bir değer yok, **panelden kontrol edin**). Panelde IP kısıtlaması varsa VPS IP'sine izin verin. VPS yurt dışındaysa (ör. Almanya) paneldeki **yurt dışı erişim** iznini açın; kapalıysa kayıt reddedilir. (Menü adları panel sürümüne göre değişebilir.)

### Kurulum (VPS)
```bash
cp .env.example .env            # doldurun (aşağıdaki değişkenler)
# TELEPHONY_ENABLED=true
# TELEPHONY_SECRET=$(openssl rand -hex 32)   ← voice-agent ve Asterisk aynı değeri okur
# NETGSM_SIP_USER / NETGSM_SIP_PASSWORD / NETGSM_SIP_SERVER   ← Netgsm panelinden
# EXTERNAL_IP=<VPS genel IP>
docker compose --profile telephony up -d --build
docker compose logs -f asterisk
docker compose exec asterisk asterisk -rx "pjsip show registrations"   # "Registered" görmelisiniz
```
Sonra numaranızı cep telefonundan arayın. `--profile telephony` verilmezse yalnızca web asistanı çalışır.

### Bilgisayarda Netgsm olmadan test
Docker Desktop + bir softphone (MicroSIP, Zoiper) ile:
1. `.env`: `TELEPHONY_ENABLED=true`, `TELEPHONY_SECRET=<rastgele>`, `EXTERNAL_IP=127.0.0.1`, `SOFTPHONE_PASSWORD=<en az 12 karakter>`; `NETGSM_*` boş kalsın.
2. `docker compose --profile telephony up -d --build`
3. Softphone hesabı: sunucu/alan adı `127.0.0.1`, kullanıcı `1001`, şifre `SOFTPHONE_PASSWORD`, taşıma UDP, codec G.711 (PCMA/PCMU). Softphone'un kendi yerel portu 5060 ise başka bir porta alın (5060'ı Docker kullanır).
4. **100**'ü arayın; asistan karşılamalı. `SOFTPHONE_PASSWORD` boşsa bu hesap hiç oluşturulmaz — sunucuda gerekmedikçe boş bırakın.

### KVKK
- Karşılama cümlesinde arayana **yapay zekâ ile konuştuğunu** ve **görüşmenin kaydedildiğini** söyleyin (örnek: `agents/botfusions-satis.yaml` → `telephony.greeting`); aydınlatma metnine bağlantıyı web sitenizde bulundurun.
- Arayan numarası ve transkript kişisel veridir; ses Google Gemini'ye (yurt dışı) gider. Yurt dışına aktarım ve saklama süresi için hukuk danışmanınızla aydınlatma/onay metnini netleştirin.

### Bilinen sınırlar
- **Ses kalitesi:** telefon hattı 8 kHz'dir (G.711); tanıma ve ses, web widget'ına göre daha düşük kalitededir.
- **Gecikme:** modelin ~1,3 sn'lik yanıt süresine telefon ağı ve yeniden örnekleme eklenir; toplamda 1,5–2,5 sn beklenebilir (tahmin; telefonda henüz ölçülmedi).
- **Yalnızca gelen arama.** Giden arama (otomatik arama/kampanya) yok; yapılacaksa ticari ileti izinleri için **İYS** kaydı ve kontrolü gerekir.
- Eşzamanlı arama sayısı `asterisk/conf/asterisk.conf` → `maxcalls` (20) ve voice-agent'ın günlük limitleriyle sınırlıdır.
- Sunucu aramayı kapattığında Asterisk 20 günlüğe `Failed to receive frame from AudioSocket` yazabilir; bu beklenen bir mesajdır.

### Sorun giderme
| Belirti | Olası neden / çözüm |
|---|---|
| Asterisk açılmıyor, `HATA: ...` | Mesajdaki `.env` değişkenini doldurun (ör. `TELEPHONY_SECRET`, `EXTERNAL_IP`). |
| `pjsip show registrations` → `Rejected` / `Unregistered` | Kullanıcı adı/şifre yanlış, Netgsm panelinde IP izni yok ya da yurt dışı erişim kapalı; `NETGSM_SIP_SERVER` değerini panelden kontrol edin. |
| Arama geliyor ama **tek yönlü ses / hiç ses yok** | NAT/RTP: `EXTERNAL_IP` sunucunun gerçek genel IP'si mi, 10000-10100/udp güvenlik duvarında ve compose'da açık mı? |
| Arama açılıp hemen kapanıyor | `docker compose logs asterisk` → `Kayit reddedildi`: `TELEPHONY_SECRET` iki tarafta aynı mı, `TELEPHONY_ENABLED=true` mi, `TELEPHONY_AGENT` doğru ve YAML'da `telephony.enabled: true` mi? |
| Softphone kayıt olamıyor | Şifre/kullanıcı (1001) ve sunucu `127.0.0.1`; softphone yerel portu 5060 ile çakışıyor olabilir. |
| SIP ayrıntısı görmek | `docker compose exec asterisk asterisk -rx "pjsip set logger on"` sonra `docker compose logs -f asterisk`. |

## Testler
```bash
python3 -m pytest -q            # 158 test, ağ gerekmez (Gemini ve webhook'lar sahte)
bash tests/widget/run.sh        # widget: Playwright + sahte sunucu (Chromium gerekir)
```

## Bilinen sınırlar ve riskler
- **Araç sonucu beklenmeden konuşma:** Test görüşmesinde asistan `book_demo` sonucu gelmeden "talebinizi iletiyorum" dedi (NON_BLOCKING). Kesin onay gereken araçlarda YAML'da `speak_while_running: false` kullanın.
- **Token sayımı:** birikimli sayılıyor olabilir; kendi faturanızla karşılaştırın.
- **Maliyet:** `/admin`'deki maliyet `server/admin.py` üstündeki `COST_PER_1M` sabitinden tahmindir; kendi faturanızla güncelleyin.
- **Tek sunucu:** Çağrı sayaçları ve ADK oturumları bellekte; birden fazla worker/sunucu için paylaşılan depo gerekir.
- **Kötüye kullanım:** IP başına oturum sınırı yok; günlük kota maliyeti sınırlar ama tek kullanıcı kotayı tüketebilir.
- **Kapsam dışı:** kamera/ekran paylaşımı, çok kiracılı panel, ödeme.
- **Tarayıcılar:** Chromium'da test edildi; Safari/iOS ve Firefox gerçek cihazda denenmedi.
- ADK'nın canlı API'si deneysel olarak işaretli.

## Lisans ve atıf
Bu kod Botfusions için sıfırdan yazıldı. [emrahmete/gemini-live-voicebot-studio](https://github.com/emrahmete/gemini-live-voicebot-studio) mimari fikir kaynağıdır; oradan kod kopyalanmadı (o repo lisanssızdır).
