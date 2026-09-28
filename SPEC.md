# Botfusions Voice Agent — Ortak Sözleşme (SPEC)

Bu dosya alt ajanların paralel çalışması için bağlayıcı sözleşmedir. Arayüzleri değiştirmek gerekirse
önce bu dosya güncellenir. Kod yorumları ve README Türkçe; kod içi adlar İngilizce.

Ürün: Müşteri sitesine tek `<script>` ile gömülen, Türkçe öncelikli, gerçek zamanlı sesli AI asistan.
Altyapı: Python 3.11, FastAPI + WebSocket, Google ADK (`Runner.run_live`, `LiveRequestQueue`),
`google-genai`. Widget: bağımlılıksız vanilla JS + Web Audio. Veritabanı: SQLite (stdlib `sqlite3`).
Emrah Mete'nin demosundan KOD KOPYALANMAZ (lisanssız); yalnızca mimari fikirler kullanılır.

## Klasör yapısı

```
sesli_agent/
├── SPEC.md                 ← bu dosya
├── README.md               ← Türkçe kurulum/kullanım (entegrasyon aşaması)
├── requirements.txt
├── Dockerfile, docker-compose.yml, .env.example, .gitignore
├── agents/                 ← asistan yapılandırmaları (YAML), ör. botfusions-satis.yaml
├── server/
│   ├── __init__.py
│   ├── settings.py         ← [A] ortam değişkenleri (pydantic-settings değil; os.environ + dataclass)
│   ├── config.py           ← [A] AgentConfig şeması + YAML yükleyici
│   ├── app.py              ← [A] FastAPI uygulaması, rotalar, statik dosyalar
│   ├── live.py             ← [A] WebSocket ↔ ADK run_live köprüsü
│   ├── limits.py           ← [A] origin kontrolü, oturum süresi, günlük limit
│   ├── tools.py            ← [B] webhook araçları (ADK tool üretimi, HMAC, timeout)
│   ├── storage.py          ← [D] SQLite katmanı
│   ├── summary.py          ← [D] oturum sonu özet (google-genai, metin modeli)
│   ├── admin.py            ← [D] /admin rotaları (APIRouter) + HTML
│   └── redact.py           ← [D] kişisel veri maskeleme (telefon, e-posta, TCKN)
├── widget/
│   ├── voice-agent.js      ← [C] gömülebilir widget (tek dosya, bağımlılıksız)
│   ├── pcm-worklet.js      ← [C] AudioWorklet: mikrofon → PCM16 16 kHz
│   └── demo.html           ← [C] örnek müşteri sayfası
└── tests/
    ├── test_config.py, test_limits.py, test_live_protocol.py   ← [A]
    ├── test_tools.py                                           ← [B]
    ├── test_storage.py, test_redact.py, test_admin.py          ← [D]
    └── widget/ (Playwright testi)                              ← [C]
```

Her ajan YALNIZCA kendi dosyalarına yazar. Başkasının dosyasına ihtiyaç duyarsa bu sözleşmedeki
imzaya göre import eder (dosya henüz yoksa testte sahte/mock kullanır).

## Ortam değişkenleri (server/settings.py → `Settings`, `get_settings()`)

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `GEMINI_API_KEY` | — | Google AI Studio anahtarı (Vertex kullanılmıyorsa zorunlu) |
| `GOOGLE_GENAI_USE_VERTEXAI` | `false` | `true` ise Vertex AI (+ `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`) |
| `LIVE_MODEL` | `gemini-2.5-flash-native-audio-preview-09-2025` | Canlı ses modeli; asistan YAML'ı `model` ile ezebilir. Gerçek erişilebilir ad anahtarla doğrulanacak. |
| `SUMMARY_MODEL` | `gemini-2.5-flash` | Özet için metin modeli |
| `AGENTS_DIR` | `./agents` | YAML klasörü |
| `DATABASE_PATH` | `./data/voice-agent.db` | SQLite dosyası |
| `ADMIN_TOKEN` | — | `/admin` için Bearer/çerez token; boşsa admin kapalı |
| `PUBLIC_BASE_URL` | `http://localhost:8090` | Widget'ın bağlanacağı adres (demo sayfası için) |
| `PORT` | `8090` | |

## AgentConfig (server/config.py)

pydantic v2 modelleri. YAML dosya adı ≠ id ise hata.

```python
class ToolParam(BaseModel):            # JSON Schema alt kümesi
    type: Literal["string","number","integer","boolean"]
    description: str = ""
    enum: list[str] | None = None
    required: bool = True

class WebhookTool(BaseModel):
    name: str                         # ^[a-z][a-z0-9_]{1,40}$
    description: str                  # modele gösterilir
    parameters: dict[str, ToolParam] = {}
    url: HttpUrl
    method: Literal["POST","GET"] = "POST"
    timeout_s: float = 8.0            # 1..30
    secret_env: str | None = None     # HMAC anahtarı hangi ortam değişkeninde
    speak_while_running: bool = True  # araç çalışırken asistan konuşmaya devam edebilir (NON_BLOCKING)

class Limits(BaseModel):
    max_session_seconds: int = 600
    max_daily_sessions: int = 200
    max_daily_minutes: int = 300

class Theme(BaseModel):
    title: str = "Sesli Asistan"
    primary_color: str = "#A855F7"    # ^#[0-9A-Fa-f]{6}$
    position: Literal["bottom-right","bottom-left"] = "bottom-right"

class AgentConfig(BaseModel):
    id: str                           # ^[a-z0-9-]{2,40}$
    name: str
    language: str = "tr-TR"
    voice: str = "Kore"               # Gemini hazır ses adı
    model: str | None = None          # None → settings.LIVE_MODEL
    instructions: str                 # sistem talimatı
    greeting: str | None = None       # oturum başında asistanın ilk cümlesi
    knowledge: str | None = None      # talimata eklenen bilgi metni (≤ 20 000 karakter)
    tools: list[WebhookTool] = []
    allowed_origins: list[str] = []   # boş = yalnızca localhost (geliştirme)
    limits: Limits = Limits()
    theme: Theme = Theme()

def load_agents(dir: Path) -> dict[str, AgentConfig]
def get_agent(agent_id: str) -> AgentConfig   # KeyError → 404
def public_view(agent: AgentConfig) -> dict   # widget'a gidecek güvenli alt küme: id, name, greeting, theme, language
```

## WebSocket protokolü — `GET /ws/live/{agent_id}`

- Origin başlığı `allowed_origins` ile kontrol edilir; uymazsa bağlantı `1008` ile kapanır.
- İkili (binary) çerçeve istemci→sunucu: PCM16 little-endian, mono, **16 kHz**, 20–100 ms parçalar (≤ 32 KB).
- İkili çerçeve sunucu→istemci: PCM16 little-endian, mono, **24 kHz**.
- Metin çerçeveleri JSON:

İstemci → sunucu
| type | alanlar | anlam |
|---|---|---|
| `start` | `{}` | oturumu başlat (ilk mesaj olmalı) |
| `text` | `text` | yazılı mesaj (≤ 2000 karakter) |
| `end` | `{}` | oturumu kapat |

Sunucu → istemci
| type | alanlar | anlam |
|---|---|---|
| `ready` | `session_id`, `agent` (public_view) | oturum açıldı |
| `transcript` | `role` (`user`/`agent`), `text`, `final` (bool) | canlı transkript |
| `interrupted` | `{}` | kullanıcı sözü kesti → istemci ses kuyruğunu HEMEN temizler |
| `turn_complete` | `{}` | asistan turunu bitirdi |
| `tool` | `name`, `status` (`start`/`ok`/`error`), `summary?` | araç durumu (UI kartı) |
| `limit` | `reason` (`session_time`/`daily_sessions`/`daily_minutes`) | limit doldu, ardından kapanış |
| `error` | `code`, `message` (Türkçe, kullanıcıya gösterilebilir) | hata |

Sunucu, bağlantı kapanınca `storage.end_session` çağırır ve arka planda özet üretir.

## Modül arayüzleri

### [B] server/tools.py
```python
ToolEventCallback = Callable[[dict], Awaitable[None]]   # {"type":"tool","name":..,"status":..,"summary":..}
def build_adk_tools(agent: AgentConfig, on_event: ToolEventCallback) -> list   # ADK BaseTool/FunctionTool listesi
async def call_webhook(tool: WebhookTool, args: dict, *, session_id: str, agent_id: str) -> dict
    # POST JSON {"tool":name,"args":args,"session_id":..,"agent_id":..,"ts":unix}
    # başlıklar: X-Botfusions-Signature: sha256=<hex HMAC(body)> (secret_env tanımlıysa), X-Botfusions-Timestamp
    # dönüş: {"ok":True,"data":<json>} | {"ok":False,"error":"timeout"|"http_<kod>"|"invalid_json"|..}
    # yanıt gövdesi ≤ 64 KB; bir oturumda araç başına ≤ 20 çağrı
def verify_signature(body: bytes, header: str, secret: str) -> bool   # müşteri tarafı örneği için
```
Parametre şeması `ToolParam`'dan ADK/genai `FunctionDeclaration`'a çevrilir (dinamik araç; Python fonksiyon imzası üretmek yerine BaseTool alt sınıfı tercih edilir — kurulu ADK sürümünün API'sine bakılarak).

### [D] server/storage.py
```python
class Store:
    def __init__(self, path: Path)                       # tabloları oluşturur (idempotent)
    def create_session(self, agent_id: str, origin: str | None) -> str      # uuid4 hex
    def add_transcript(self, session_id: str, role: str, text: str) -> None # final metinler
    def add_event(self, session_id: str, kind: str, payload: dict) -> None  # tool, error, limit…
    def end_session(self, session_id: str, reason: str, usage: dict) -> None
        # usage: {"input_tokens":int,"output_tokens":int,"audio_in_s":float,"audio_out_s":float}
    def set_summary(self, session_id: str, summary: str) -> None
    def usage_today(self, agent_id: str) -> dict          # {"sessions":int,"seconds":float} (UTC gün)
    def list_sessions(self, agent_id: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]
    def get_session(self, session_id: str) -> dict | None # transcript + events dahil
```
Metinler `redact.redact()` ile maskelenerek yazılır. Thread-safe (tek bağlantı + `threading.Lock`), async kodda `asyncio.to_thread` ile çağrılır.

### [D] server/summary.py
`async def summarize(transcript: list[dict], language: str = "tr") -> str` — 3–5 madde: konu, müşteri talebi, sonuç/sonraki adım. Hata olursa boş string, istisna fırlatmaz.

### [D] server/admin.py
`router = APIRouter(prefix="/admin")` — `GET /admin` (HTML liste), `GET /admin/sessions/{id}` (HTML detay), `GET /admin/api/sessions` (JSON). `ADMIN_TOKEN` yoksa 404; varsa `Authorization: Bearer` veya `?token=` → çerez. Sayfalar Botfusions stiline uygun, sunucu tarafı render (Jinja yok; f-string + `html.escape`).

### [D] server/redact.py
`def redact(text: str) -> str` — e-posta, TR telefon (+90/0 5xx…), 11 haneli TCKN, 16 haneli kart no → `[e-posta]`, `[telefon]`, `[tckn]`, `[kart]`.

### [A] server/limits.py
```python
def origin_allowed(origin: str | None, agent: AgentConfig) -> bool   # boş liste → yalnızca localhost/127.0.0.1
async def check_daily(store, agent) -> str | None                     # None veya limit reason
```

### [A] server/app.py
- `GET /health` → `{"ok":true}`
- `GET /api/agents/{id}` → `public_view` (CORS: allowed_origins)
- `WS /ws/live/{id}` → live.py
- `GET /widget/*` → widget klasörü (statik, `Cache-Control: public, max-age=300`)
- `GET /demo/{id}` → widget/demo.html (agent id enjekte)
- `app.include_router(admin.router)`

### [C] Widget gömme kodu
```html
<script src="https://ASISTAN-SUNUCUSU/widget/voice-agent.js" data-agent="botfusions-satis" defer></script>
```
- Sağ altta yuvarlak buton → panel: başlık, durum (Bağlanıyor / Dinliyor / Konuşuyor), transkript balonları, mikrofon aç/kapa, yazıyla mesaj, kapat.
- `data-agent` zorunlu; sunucu adresi script'in kendi `src`'sinden türetilir.
- Mikrofon: `getUserMedia` + AudioWorklet (pcm-worklet.js) → 16 kHz PCM16, 40 ms parçalar.
- Çalma: 24 kHz PCM16 → AudioBuffer kuyruğu; `interrupted` gelince tüm planlı kaynaklar durdurulur.
- Shadow DOM içinde stil (müşteri CSS'iyle çakışmaz), `theme.primary_color` uygulanır, erişilebilir (aria, klavye).
- Mikrofon izni reddedilirse yazılı moda düşer ve Türkçe açıklama gösterir.

## Genel kurallar
- Gizli anahtarlar asla loglanmaz, istemciye gönderilmez.
- Tüm dış çağrılarda zaman aşımı.
- Hatalar kullanıcıya Türkçe, logda İngilizce teknik ayrıntıyla.
- Testler `pytest -q` ile ağ erişimi olmadan geçer (Gemini ve webhook'lar sahte).
