/*!
 * Botfusions Voice Agent — gömülebilir sesli asistan widget'ı.
 * Kullanım:
 *   <script src="https://ASISTAN-SUNUCUSU/widget/voice-agent.js" data-agent="botfusions-satis" defer></script>
 * Bağımlılık yoktur; tüm arayüz Shadow DOM içinde çizilir.
 */
(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  // 1) Script etiketini ve sunucu adresini bul
  // ---------------------------------------------------------------------------
  var scriptEl = document.currentScript;
  if (!scriptEl || !scriptEl.getAttribute('data-agent')) {
    // currentScript bazı eski ortamlarda boş olabilir; data-agent taşıyan etiketi ara.
    var cands = document.querySelectorAll('script[data-agent][src*="voice-agent"]');
    scriptEl = cands.length ? cands[cands.length - 1] : null;
  }
  if (!scriptEl) return;
  if (scriptEl.__botfusionsVoiceAgent) return; // aynı etiket iki kez çalışmasın
  scriptEl.__botfusionsVoiceAgent = true;

  var AGENT_ID = (scriptEl.getAttribute('data-agent') || '').trim();
  if (!AGENT_ID) {
    console.warn('[voice-agent] data-agent özniteliği zorunludur.');
    return;
  }

  var SCRIPT_URL = new URL(scriptEl.src, location.href);
  // "/widget/voice-agent.js" kısmını atarak sunucu kökünü bul (alt yol altında barındırmayı destekler).
  var BASE_PATH = SCRIPT_URL.pathname.replace(/\/widget\/[^\/]*$/, '');
  if (BASE_PATH === SCRIPT_URL.pathname) BASE_PATH = SCRIPT_URL.pathname.replace(/\/[^\/]*$/, '');
  var HTTP_BASE = SCRIPT_URL.origin + BASE_PATH;
  var WS_BASE = HTTP_BASE.replace(/^http/i, 'ws'); // http→ws, https→wss
  var WORKLET_URL = new URL('pcm-worklet.js', SCRIPT_URL).href;

  var INPUT_RATE = 16000;
  var OUTPUT_RATE = 24000;
  var MAX_TEXT = 2000;
  var MAX_WS_BUFFER = 256 * 1024; // gönderim kuyruğu bu değeri aşarsa mikrofon parçası atlanır

  // ---------------------------------------------------------------------------
  // 2) Türkçe metinler
  // ---------------------------------------------------------------------------
  var T = {
    open: 'Sesli asistanı aç',
    close: 'Kapat',
    closeLong: 'Görüşmeyi bitir ve paneli kapat',
    send: 'Gönder',
    placeholder: 'Mesajınızı yazın…',
    inputLabel: 'Yazılı mesaj',
    micOn: 'Mikrofonu kapat',
    micOff: 'Mikrofonu aç',
    log: 'Konuşma dökümü',
    reconnect: 'Tekrar bağlan',
    you: 'Siz',
    agent: 'Asistan',
    status: {
      idle: 'Hazır',
      connecting: 'Bağlanıyor',
      listening: 'Dinliyor',
      speaking: 'Konuşuyor',
      muted: 'Mikrofon kapalı',
      text: 'Yazılı mod',
      disconnected: 'Bağlantı koptu',
      ended: 'Görüşme bitti'
    },
    micDenied: 'Mikrofon izni verilmedi. Yazarak devam edebilirsiniz. Sesli görüşme için adres çubuğundaki izin simgesinden mikrofona izin verip mikrofon düğmesine basın.',
    micMissing: 'Mikrofon bulunamadı. Yazarak devam edebilirsiniz.',
    micInsecure: 'Sesli görüşme için güvenli (HTTPS) bağlantı gerekir. Yazarak devam edebilirsiniz.',
    micBusy: 'Mikrofon başka bir uygulama tarafından kullanılıyor olabilir. Yazarak devam edebilirsiniz.',
    micGeneric: 'Mikrofon başlatılamadı. Yazarak devam edebilirsiniz.',
    reconnecting: 'Bağlantı koptu, yeniden bağlanılıyor…',
    lost: 'Bağlantı koptu. Devam etmek için “Tekrar bağlan” düğmesine basın.',
    notFound: 'Asistan bulunamadı.',
    originDenied: 'Bu site asistana bağlanma iznine sahip değil.',
    limit: {
      session_time: 'Görüşme süresi doldu. Yeni bir görüşme başlatabilirsiniz.',
      daily_sessions: 'Bugünkü görüşme sınırına ulaşıldı. Lütfen daha sonra tekrar deneyin.',
      daily_minutes: 'Bugünkü görüşme süresi sınırına ulaşıldı. Lütfen daha sonra tekrar deneyin.',
      _: 'Kullanım sınırına ulaşıldı.'
    },
    genericError: 'Bir hata oluştu. Lütfen tekrar deneyin.',
    tool: { start: 'çalışıyor…', ok: 'tamamlandı', error: 'başarısız oldu' },
    toolPrefix: 'İşlem: '
  };

  // ---------------------------------------------------------------------------
  // 3) Yardımcılar
  // ---------------------------------------------------------------------------
  function hexToRgb(hex) {
    var m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
    if (!m) return null;
    var n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function rgbToHex(c) {
    return '#' + c.map(function (v) { return ('0' + Math.round(v).toString(16)).slice(-2); }).join('');
  }
  function mix(a, b, t) { // a ile b arasında t oranında karışım
    return rgbToHex([0, 1, 2].map(function (i) { return a[i] * (1 - t) + b[i] * t; }));
  }
  function luminance(c) {
    var v = c.map(function (x) { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); });
    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2];
  }
  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === 'text') e.textContent = attrs[k];
      else if (k === 'html') e.innerHTML = attrs[k]; // yalnızca sabit SVG için kullanılır
      else e.setAttribute(k, attrs[k]);
    }
    (children || []).forEach(function (c) { if (c) e.appendChild(c); });
    return e;
  }

  // Satır içi SVG simgeleri (sabit metin; kullanıcı verisi içermez)
  var ICON_MIC = '<svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M12 14a3 3 0 0 0 3-3V5a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2Z"/></svg>';
  var ICON_MIC_OFF = '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" focusable="false"><path fill="currentColor" d="M19 11h-2a5 5 0 0 1-.7 2.55l1.46 1.46A6.96 6.96 0 0 0 19 11ZM15 11V5a3 3 0 0 0-5.9-.77L15 10.13V11ZM4.27 3 3 4.27l6 6V11a3 3 0 0 0 4.52 2.59l1.5 1.5A5 5 0 0 1 7 11H5a7 7 0 0 0 6 6.92V21h2v-3.08a6.9 6.9 0 0 0 3.5-1.44L19.73 21 21 19.73 4.27 3Z"/></svg>';
  var ICON_CLOSE = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false"><path fill="currentColor" d="M18.3 5.71 12 12.01l-6.3-6.3-1.41 1.41 6.3 6.3-6.3 6.29 1.41 1.42 6.3-6.3 6.29 6.3 1.42-1.42-6.3-6.29 6.3-6.3z"/></svg>';
  var ICON_SEND = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false"><path fill="currentColor" d="M3.4 20.4 21 12 3.4 3.6 3.4 10l12.6 2-12.6 2z"/></svg>';

  // ---------------------------------------------------------------------------
  // 4) Stil (Shadow DOM içinde; müşteri CSS'inden yalıtılmış)
  // ---------------------------------------------------------------------------
  var CSS = [
    ':host{all:initial}',
    '*{box-sizing:border-box}',
    '.root{--va-primary:#A855F7;--va-primary-ink:#fff;--va-user-bg:#4b2a70;--va-bg:#16121f;--va-bg2:#1e1829;--va-line:#2e2640;',
    '--va-text:#EDE8DF;--va-muted:#b3aabd;--va-focus:#EDE8DF;',
    'position:fixed;bottom:20px;right:20px;z-index:2147483000;',
    'font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;color:var(--va-text)}',
    '.root.left{right:auto;left:20px}',
    'button{font:inherit;cursor:pointer}',
    'button:focus-visible,input:focus-visible,.log:focus-visible{outline:3px solid var(--va-focus);outline-offset:2px}',
    '.fab{width:60px;height:60px;border-radius:50%;border:0;background:var(--va-primary);color:var(--va-primary-ink);',
    'display:grid;place-items:center;box-shadow:0 8px 24px rgba(0,0,0,.35);transition:transform .15s ease}',
    '.fab:hover{transform:scale(1.06)}',
    '.fab[aria-expanded="true"]{display:none}',
    '.panel{position:absolute;bottom:0;right:0;width:370px;height:min(580px,calc(100vh - 40px));display:flex;flex-direction:column;',
    'background:var(--va-bg);border:1px solid var(--va-line);border-radius:18px;overflow:hidden;box-shadow:0 18px 48px rgba(0,0,0,.45);',
    'animation:va-in .18s ease-out}',
    '.root.left .panel{right:auto;left:0}',
    '.panel[hidden]{display:none}',
    '@keyframes va-in{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}',
    'header{display:flex;align-items:center;gap:10px;padding:14px 14px 12px 16px;border-bottom:1px solid var(--va-line);background:var(--va-bg2)}',
    '.titles{flex:1;min-width:0}',
    'h2{margin:0;font-size:16px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.badge{display:inline-flex;align-items:center;gap:6px;margin-top:3px;font-size:12.5px;color:var(--va-muted)}',
    '.dot{width:8px;height:8px;border-radius:50%;background:#8a8196}',
    '.badge[data-state="listening"] .dot{background:#34d399}',
    '.badge[data-state="speaking"] .dot{background:var(--va-primary);animation:va-pulse 1s ease-in-out infinite}',
    '.badge[data-state="connecting"] .dot{background:#fbbf24;animation:va-pulse 1s ease-in-out infinite}',
    '.badge[data-state="disconnected"] .dot{background:#f87171}',
    '@keyframes va-pulse{50%{opacity:.35}}',
    '.icon-btn{width:40px;height:40px;border-radius:10px;border:0;background:transparent;color:var(--va-text);display:grid;place-items:center}',
    '.icon-btn:hover{background:var(--va-line)}',
    '.notice{margin:10px 12px 0;padding:10px 12px;border-radius:10px;background:#3a1d24;color:#fde2e2;font-size:13.5px;border:1px solid #6b2b36}',
    '.notice.info{background:#1f2a3a;color:#dbeafe;border-color:#2c4668}',
    '.notice[hidden],.toolline[hidden],.retry[hidden]{display:none}',
    '.log{flex:1;overflow-y:auto;padding:14px 12px;display:flex;flex-direction:column;gap:8px;scroll-behavior:smooth}',
    '.intro{margin:auto 8px;text-align:center;color:var(--va-muted);font-size:14px}',
    '.msg{max-width:84%;padding:9px 12px;border-radius:14px;white-space:pre-wrap;overflow-wrap:anywhere}',
    '.msg.agent{align-self:flex-start;background:var(--va-bg2);border:1px solid var(--va-line);border-bottom-left-radius:4px}',
    '.msg.user{align-self:flex-end;background:var(--va-user-bg);border-bottom-right-radius:4px}',
    '.msg.partial{opacity:.72}',
    '.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}',
    '.toolline{margin:0 12px 6px;padding:6px 10px;border-radius:8px;font-size:12.5px;color:var(--va-muted);background:var(--va-bg2);border:1px dashed var(--va-line)}',
    '.toolline.error{color:#fca5a5}',
    '.retry{margin:0 12px 8px;padding:10px;border-radius:10px;border:1px solid var(--va-primary);background:transparent;color:var(--va-text);font-weight:600}',
    '.retry:hover{background:var(--va-line)}',
    'form{display:flex;align-items:center;gap:8px;padding:10px 12px 12px;border-top:1px solid var(--va-line);background:var(--va-bg2)}',
    'input{flex:1;min-width:0;height:42px;padding:0 12px;border-radius:10px;border:1px solid var(--va-line);background:var(--va-bg);color:var(--va-text);font:inherit;font-size:16px}',
    'input::placeholder{color:#8f879b}',
    '.mic{width:42px;height:42px;border-radius:50%;border:0;display:grid;place-items:center;background:var(--va-primary);color:var(--va-primary-ink)}',
    '.mic[aria-pressed="false"]{background:var(--va-line);color:var(--va-text)}',
    '.send{width:42px;height:42px;border-radius:10px;border:0;display:grid;place-items:center;background:var(--va-line);color:var(--va-text)}',
    '.send:hover{background:var(--va-primary);color:var(--va-primary-ink)}',
    // Mobil: tam genişlikte alt sayfa
    '@media (max-width:480px){',
    '.root,.root.left{right:16px;bottom:16px;left:auto}',
    '.root.left{left:16px;right:auto}',
    '.root.open{left:0;right:0;bottom:0}',
    '.panel,.root.left .panel{position:fixed;left:0;right:0;bottom:0;width:100%;height:78vh;height:78dvh;max-height:none;',
    'border-radius:18px 18px 0 0;border-left:0;border-right:0;border-bottom:0;padding-bottom:env(safe-area-inset-bottom)}',
    '}',
    '@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}'
  ].join('\n');

  // ---------------------------------------------------------------------------
  // 5) Durum
  // ---------------------------------------------------------------------------
  var S = {
    agent: { id: AGENT_ID, name: 'Sesli Asistan', greeting: null, theme: { title: 'Sesli Asistan', primary_color: '#A855F7', position: 'bottom-right' } },
    open: false,
    status: 'idle',
    ws: null,
    ready: false,
    sessionId: null,
    userClosed: false,
    noAutoRetry: false,     // limit/hata sonrası otomatik yeniden bağlanma yapılmaz
    retriesLeft: 1,         // yalnızca bir kez otomatik yeniden bağlan
    retryTimer: null,
    // ses
    ctx: null,
    stream: null,
    micNode: null,
    srcNode: null,
    micEnabled: true,       // kullanıcının mikrofon tercihi
    micAvailable: false,
    textMode: false,
    playGain: null,
    nextStartTime: 0,
    sources: [],            // planlanmış/çalan AudioBufferSourceNode listesi
    oddByte: null,          // tek bayt artığı (PCM16 hizalaması için)
    // transkript
    partial: { user: null, agent: null },
    // sayaçlar (test/hata ayıklama)
    chunksSent: 0,
    bytesSent: 0,
    chunksReceived: 0,
    bytesReceived: 0,
    interruptions: 0,
    connects: 0,
    lastEvent: null
  };

  // Test ve hata ayıklama için salt-okunur iç durum özeti
  window.__voiceAgentDebug = function () {
    return {
      agentId: AGENT_ID,
      httpBase: HTTP_BASE,
      wsBase: WS_BASE,
      status: S.status,
      open: S.open,
      ready: S.ready,
      sessionId: S.sessionId,
      wsState: S.ws ? S.ws.readyState : -1,
      queueSize: S.sources.length,
      queuedSeconds: S.ctx ? Math.max(0, S.nextStartTime - S.ctx.currentTime) : 0,
      chunksSent: S.chunksSent,
      bytesSent: S.bytesSent,
      chunksReceived: S.chunksReceived,
      bytesReceived: S.bytesReceived,
      interruptions: S.interruptions,
      connects: S.connects,
      micActive: !!(S.stream && S.micEnabled),
      textMode: S.textMode,
      audioContextState: S.ctx ? S.ctx.state : 'none',
      sampleRate: S.ctx ? S.ctx.sampleRate : 0,
      lastEvent: S.lastEvent
    };
  };

  // ---------------------------------------------------------------------------
  // 6) Arayüz
  // ---------------------------------------------------------------------------
  var host = el('div', { id: 'botfusions-voice-agent' });
  var shadow = host.attachShadow({ mode: 'open' });
  var styleEl = el('style', { text: CSS });

  var fab = el('button', { class: 'fab', type: 'button', 'aria-label': T.open, 'aria-expanded': 'false', 'aria-controls': 'va-panel', 'aria-haspopup': 'dialog', html: ICON_MIC });
  var titleEl = el('h2', { id: 'va-title', text: S.agent.theme.title });
  var badgeText = el('span', { text: T.status.idle });
  var badge = el('div', { class: 'badge', 'data-state': 'idle', role: 'status' }, [el('span', { class: 'dot', 'aria-hidden': 'true' }), badgeText]);
  var closeBtn = el('button', { class: 'icon-btn close', type: 'button', 'aria-label': T.closeLong, title: T.close, html: ICON_CLOSE });
  var header = el('header', null, [el('div', { class: 'titles' }, [titleEl, badge]), closeBtn]);
  var notice = el('div', { class: 'notice', role: 'alert', hidden: '' });
  var intro = el('p', { class: 'intro' });
  var log = el('div', { class: 'log', role: 'log', 'aria-live': 'polite', 'aria-relevant': 'additions text', 'aria-label': T.log, tabindex: '0' }, [intro]);
  var toolLine = el('div', { class: 'toolline', role: 'status', hidden: '' });
  var retryBtn = el('button', { class: 'retry', type: 'button', hidden: '', text: T.reconnect });
  var micBtn = el('button', { class: 'mic', type: 'button', 'aria-pressed': 'true', 'aria-label': T.micOn, title: T.micOn, html: ICON_MIC });
  var inputLabel = el('label', { class: 'sr-only', for: 'va-input', text: T.inputLabel });
  var input = el('input', { id: 'va-input', type: 'text', maxlength: String(MAX_TEXT), placeholder: T.placeholder, autocomplete: 'off', enterkeyhint: 'send' });
  var sendBtn = el('button', { class: 'send', type: 'submit', 'aria-label': T.send, title: T.send, html: ICON_SEND });
  var form = el('form', { novalidate: '' }, [micBtn, inputLabel, input, sendBtn]);
  var panel = el('section', { class: 'panel', id: 'va-panel', role: 'dialog', 'aria-labelledby': 'va-title', hidden: '' }, [header, notice, log, toolLine, retryBtn, form]);
  var root = el('div', { class: 'root' }, [fab, panel]);
  shadow.appendChild(styleEl);
  shadow.appendChild(root);

  function applyAgent(view) {
    if (!view || typeof view !== 'object') return;
    var theme = view.theme || {};
    S.agent = {
      id: view.id || AGENT_ID,
      name: view.name || S.agent.name,
      greeting: view.greeting || null,
      theme: {
        title: theme.title || view.name || S.agent.theme.title,
        primary_color: hexToRgb(theme.primary_color) ? theme.primary_color : S.agent.theme.primary_color,
        position: theme.position === 'bottom-left' ? 'bottom-left' : 'bottom-right'
      }
    };
    var t = S.agent.theme;
    titleEl.textContent = t.title;
    fab.setAttribute('aria-label', T.open + ': ' + t.title);
    root.classList.toggle('left', t.position === 'bottom-left');
    var rgb = hexToRgb(t.primary_color);
    root.style.setProperty('--va-primary', t.primary_color);
    // Birincil renk üzerindeki simge rengi: parlak renklerde koyu, koyularda beyaz
    root.style.setProperty('--va-primary-ink', luminance(rgb) > 0.45 ? '#0E0B15' : '#ffffff');
    // Kullanıcı balonu: tema rengi ile koyu zeminin karışımı (kemik metinle okunaklı kontrast)
    root.style.setProperty('--va-user-bg', mix(rgb, [22, 18, 31], 0.62));
    intro.textContent = S.agent.greeting || (t.title + ' ile konuşmak için mikrofona izin verin ya da yazın.');
  }

  function setStatus(s) {
    S.status = s;
    badge.setAttribute('data-state', s);
    badgeText.textContent = T.status[s] || s;
  }

  // Ses/bağlantı durumuna göre rozeti hesaplar
  function refreshStatus() {
    if (!S.ws || S.ws.readyState > 1) return; // bağlantı yoksa rozet olduğu gibi kalır
    if (!S.ready) return setStatus('connecting');
    if (S.sources.length > 0) return setStatus('speaking');
    if (S.textMode) return setStatus('text');
    if (!S.micEnabled) return setStatus('muted');
    setStatus('listening');
  }

  function showNotice(text, kind) {
    notice.textContent = text;
    notice.className = 'notice' + (kind === 'info' ? ' info' : '');
    notice.hidden = !text;
  }

  function scrollLog() { log.scrollTop = log.scrollHeight; }

  function addBubble(role, text, partial) {
    if (intro.parentNode) intro.parentNode.removeChild(intro);
    var b = el('div', { class: 'msg ' + role + (partial ? ' partial' : '') });
    b.appendChild(el('span', { class: 'sr-only', text: (role === 'user' ? T.you : T.agent) + ': ' }));
    b.appendChild(el('span', { class: 'txt', text: text }));
    log.appendChild(b);
    scrollLog();
    return b;
  }

  // Transkript olayı: final:false balonu yerinde güncellenir, final:true sabitlenir.
  function onTranscript(role, text, final) {
    role = role === 'user' ? 'user' : 'agent';
    text = String(text == null ? '' : text);
    var b = S.partial[role];
    if (!b) {
      if (!text && final) return;
      b = addBubble(role, text, !final);
    } else {
      b.querySelector('.txt').textContent = text;
      scrollLog();
    }
    if (final) {
      b.classList.remove('partial');
      S.partial[role] = null;
    } else {
      S.partial[role] = b;
    }
    // Ekran okuyucular yarım cümleleri tekrar tekrar okumasın
    log.setAttribute('aria-busy', S.partial.user || S.partial.agent ? 'true' : 'false');
  }

  function finalizePartial(role) {
    var b = S.partial[role];
    if (b) {
      b.classList.remove('partial');
      if (!b.querySelector('.txt').textContent) b.parentNode.removeChild(b);
    }
    S.partial[role] = null;
    log.setAttribute('aria-busy', S.partial.user || S.partial.agent ? 'true' : 'false');
  }

  function onTool(ev) {
    var st = ev.status === 'ok' || ev.status === 'error' ? ev.status : 'start';
    var name = String(ev.name || 'araç').replace(/_/g, ' ');
    var txt = T.toolPrefix + name + ' ' + T.tool[st];
    if (ev.summary && st !== 'start') txt += ' — ' + String(ev.summary).slice(0, 160);
    toolLine.textContent = txt;
    toolLine.className = 'toolline' + (st === 'error' ? ' error' : '');
    toolLine.hidden = false;
    clearTimeout(onTool.t);
    if (st !== 'start') onTool.t = setTimeout(function () { toolLine.hidden = true; }, 6000);
  }

  function setMicButton() {
    var on = S.micEnabled && !S.textMode;
    micBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    micBtn.setAttribute('aria-label', on ? T.micOn : T.micOff);
    micBtn.title = on ? T.micOn : T.micOff;
    micBtn.innerHTML = on ? ICON_MIC : ICON_MIC_OFF;
  }

  // ---------------------------------------------------------------------------
  // 7) Ses çalma (24 kHz PCM16 → AudioBuffer kuyruğu)
  // ---------------------------------------------------------------------------
  function ensureAudioContext() {
    if (S.ctx && S.ctx.state !== 'closed') return S.ctx;
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    S.ctx = new AC({ latencyHint: 'interactive' });
    S.playGain = S.ctx.createGain();
    S.playGain.connect(S.ctx.destination);
    S.nextStartTime = 0;
    return S.ctx;
  }

  function playPcm(arrayBuf) {
    var ctx = S.ctx;
    if (!ctx || ctx.state === 'closed') return;
    var bytes = new Uint8Array(arrayBuf);
    // Önceki çerçeveden kalan tek bayt varsa başa ekle
    if (S.oddByte !== null) {
      var merged = new Uint8Array(bytes.length + 1);
      merged[0] = S.oddByte;
      merged.set(bytes, 1);
      bytes = merged;
      S.oddByte = null;
    }
    if (bytes.length % 2) {
      S.oddByte = bytes[bytes.length - 1];
      bytes = bytes.subarray(0, bytes.length - 1);
    }
    var n = bytes.length >> 1;
    if (!n) return;
    var dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.length);
    var buf = ctx.createBuffer(1, n, OUTPUT_RATE);
    var ch = buf.getChannelData(0);
    for (var i = 0; i < n; i++) ch[i] = dv.getInt16(i * 2, true) / 32768;
    var src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(S.playGain);
    // Kesintisiz çalma: bir sonraki parça bir öncekinin bittiği anda başlar
    var now = ctx.currentTime;
    if (S.nextStartTime < now + 0.01) S.nextStartTime = now + 0.03; // kuyruk boşaldıysa küçük tampon
    src.start(S.nextStartTime);
    S.nextStartTime += buf.duration;
    S.sources.push(src);
    src.onended = function () {
      var k = S.sources.indexOf(src);
      if (k >= 0) S.sources.splice(k, 1);
      if (!S.sources.length) refreshStatus();
    };
    if (S.sources.length === 1) refreshStatus();
  }

  // Söz kesildiğinde / kapanışta planlı tüm kaynakları anında durdurur
  function stopPlayback() {
    var list = S.sources;
    S.sources = [];
    for (var i = 0; i < list.length; i++) {
      try { list[i].onended = null; list[i].stop(0); } catch (e) { /* zaten bitmiş */ }
      try { list[i].disconnect(); } catch (e2) { /* yok say */ }
    }
    S.nextStartTime = S.ctx ? S.ctx.currentTime : 0;
    S.oddByte = null;
    refreshStatus();
  }

  // ---------------------------------------------------------------------------
  // 8) Mikrofon (getUserMedia → AudioWorklet → 16 kHz PCM16, 40 ms)
  // ---------------------------------------------------------------------------
  function onMicChunk(buf) {
    var ws = S.ws;
    if (!ws || ws.readyState !== 1 || !S.ready || !S.micEnabled) return;
    if (ws.bufferedAmount > MAX_WS_BUFFER) return; // ağ yavaşsa eski ses biriktirilmez
    ws.send(buf);
    S.chunksSent++;
    S.bytesSent += buf.byteLength;
  }

  // AudioWorklet yoksa/yüklenemezse (ör. CORS, eski Safari) ScriptProcessor ile aynı dönüşüm
  function makeFallbackProcessor(ctx, source) {
    var ratio = ctx.sampleRate / INPUT_RATE;
    var pos = 0, last = 0, out = new Int16Array(640), len = 0;
    var node = ctx.createScriptProcessor(2048, 1, 1);
    node.onaudioprocess = function (e) {
      var d = e.inputBuffer.getChannelData(0), n = d.length;
      while (pos + 1 < n) {
        var i = Math.floor(pos), f = pos - i;
        var s0 = i < 0 ? last : d[i];
        var v = s0 + (d[i + 1] - s0) * f;
        v = v > 1 ? 1 : v < -1 ? -1 : v;
        out[len++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        if (len === 640) { onMicChunk(out.buffer); out = new Int16Array(640); len = 0; }
        pos += ratio;
      }
      pos -= n;
      last = d[n - 1];
    };
    source.connect(node);
    // ScriptProcessor'ın çalışması için hedefe bağlı olmalı; sessiz kazanç üzerinden bağlanır
    var mute = ctx.createGain();
    mute.gain.value = 0;
    node.connect(mute);
    mute.connect(ctx.destination);
    return node;
  }

  function enterTextMode(message) {
    S.textMode = true;
    S.micAvailable = false;
    showNotice(message, 'info');
    setMicButton();
    refreshStatus();
  }

  function startMic() {
    if (S.stream) return Promise.resolve();
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      enterTextMode(window.isSecureContext === false ? T.micInsecure : T.micGeneric);
      return Promise.resolve();
    }
    var ctx = ensureAudioContext();
    return navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 }
    }).then(function (stream) {
      // Panel kapandıysa ya da oturum izin beklenirken limit/hatayla bittiyse mikrofonu hemen bırak
      // (aksi halde hata mesajı silinir ve kayıt göstergesi yanık kalır)
      if (!S.open || (S.noAutoRetry && !S.ws)) { stream.getTracks().forEach(function (t) { t.stop(); }); return; }
      S.stream = stream;
      S.micAvailable = true;
      S.textMode = false;
      S.micEnabled = true;
      if (!S.noAutoRetry) showNotice('');
      var source = ctx.createMediaStreamSource(stream);
      S.srcNode = source;
      var useWorklet = ctx.audioWorklet && typeof AudioWorkletNode === 'function'
        ? ctx.audioWorklet.addModule(WORKLET_URL).then(function () { return true; }, function (err) {
            console.warn('[voice-agent] AudioWorklet yüklenemedi, yedek işlemci kullanılıyor:', err && err.message);
            return false;
          })
        : Promise.resolve(false);
      return useWorklet.then(function (ok) {
        if (!S.stream) return; // bu arada kapatıldı
        if (ok) {
          var node = new AudioWorkletNode(ctx, 'botfusions-pcm16', { numberOfInputs: 1, numberOfOutputs: 0, channelCount: 1, channelCountMode: 'explicit' });
          node.port.onmessage = function (e) { if (e.data instanceof ArrayBuffer) onMicChunk(e.data); };
          source.connect(node);
          S.micNode = node;
        } else {
          S.micNode = makeFallbackProcessor(ctx, source);
        }
        setMicButton();
        refreshStatus();
      });
    }).catch(function (err) {
      var name = err && err.name;
      console.warn('[voice-agent] mikrofon hatası:', name, err && err.message);
      if (name === 'NotAllowedError' || name === 'SecurityError' || name === 'PermissionDeniedError') enterTextMode(T.micDenied);
      else if (name === 'NotFoundError' || name === 'OverconstrainedError') enterTextMode(T.micMissing);
      else if (name === 'NotReadableError') enterTextMode(T.micBusy);
      else enterTextMode(T.micGeneric);
    });
  }

  function stopMic() {
    if (S.micNode) {
      try { if (S.micNode.port) { S.micNode.port.onmessage = null; S.micNode.port.close(); } } catch (e) { /* yok say */ }
      try { S.micNode.disconnect(); } catch (e2) { /* yok say */ }
      S.micNode = null;
    }
    if (S.srcNode) { try { S.srcNode.disconnect(); } catch (e3) { /* yok say */ } S.srcNode = null; }
    if (S.stream) { S.stream.getTracks().forEach(function (t) { t.stop(); }); S.stream = null; }
  }

  // ---------------------------------------------------------------------------
  // 9) WebSocket
  // ---------------------------------------------------------------------------
  function connect() {
    clearTimeout(S.retryTimer);
    retryBtn.hidden = true;
    S.ready = false;
    S.noAutoRetry = false;
    setStatus('connecting');
    var ws;
    try {
      ws = new WebSocket(WS_BASE + '/ws/live/' + encodeURIComponent(AGENT_ID));
    } catch (e) {
      onDisconnected(null, null);
      return;
    }
    ws.binaryType = 'arraybuffer';
    S.ws = ws;
    S.connects++;
    ws.onopen = function () {
      if (S.ws !== ws) return;
      ws.send(JSON.stringify({ type: 'start' }));
    };
    ws.onmessage = function (ev) {
      if (S.ws !== ws) return;
      if (typeof ev.data !== 'string') {
        S.chunksReceived++;
        S.bytesReceived += ev.data.byteLength;
        playPcm(ev.data);
        return;
      }
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      handleEvent(msg);
    };
    ws.onclose = function (ev) {
      if (S.ws !== ws) return;
      onDisconnected(ws, ev);
    };
    ws.onerror = function () { /* ayrıntı onclose'da ele alınır */ };
  }

  function handleEvent(msg) {
    if (!msg || typeof msg.type !== 'string') return;
    S.lastEvent = msg.type;
    switch (msg.type) {
      case 'ready':
        S.ready = true;
        S.sessionId = msg.session_id || null;
        S.retriesLeft = 1; // başarılı bağlantıdan sonra otomatik deneme hakkı yenilenir
        if (msg.agent) applyAgent(msg.agent);
        if (notice.textContent === T.reconnecting) showNotice('');
        refreshStatus();
        break;
      case 'transcript':
        onTranscript(msg.role, msg.text, msg.final !== false);
        break;
      case 'interrupted':
        S.interruptions++;
        stopPlayback();
        finalizePartial('agent');
        break;
      case 'turn_complete':
        finalizePartial('agent');
        refreshStatus();
        break;
      case 'tool':
        onTool(msg);
        break;
      case 'limit':
        S.noAutoRetry = true;
        showNotice(T.limit[msg.reason] || T.limit._);
        break;
      case 'error':
        S.noAutoRetry = true;
        showNotice(typeof msg.message === 'string' && msg.message ? msg.message : T.genericError);
        break;
    }
  }

  function onDisconnected(ws, ev) {
    S.ws = null;
    S.ready = false;
    stopPlayback();
    finalizePartial('agent');
    finalizePartial('user');
    if (S.userClosed || !S.open) return;
    var code = ev ? ev.code : 0;
    if (code === 1008) {
      S.noAutoRetry = true;
      if (!notice.textContent || notice.hidden) showNotice(T.originDenied);
    }
    if (!S.noAutoRetry && S.retriesLeft > 0) {
      S.retriesLeft--;
      setStatus('connecting');
      showNotice(T.reconnecting, 'info');
      S.retryTimer = setTimeout(function () { if (S.open && !S.ws) connect(); }, 800);
      return;
    }
    // limit/hata sonrası sunucu kapattıysa "Görüşme bitti", aksi halde "Bağlantı koptu"
    setStatus(S.noAutoRetry && code !== 1008 ? 'ended' : 'disconnected');
    if (!S.noAutoRetry) showNotice(T.lost);
    // Oturum bitti: mikrofonu bırak (tarayıcının kayıt göstergesi sönsün); "Tekrar bağlan" yeniden açar
    stopMic();
    retryBtn.hidden = false;
  }

  // ---------------------------------------------------------------------------
  // 10) Panel aç/kapat ve olaylar
  // ---------------------------------------------------------------------------
  function openPanel() {
    if (S.open) return;
    S.open = true;
    S.userClosed = false;
    S.retriesLeft = 1;
    panel.hidden = false;
    root.classList.add('open');
    fab.setAttribute('aria-expanded', 'true');
    // AudioContext kullanıcı hareketi içinde oluşturulup başlatılmalı (Safari/iOS, otomatik oynatma kuralları)
    var ctx = ensureAudioContext();
    if (ctx && ctx.state === 'suspended') ctx.resume().catch(function () {});
    connect();
    startMic();
    setTimeout(function () { (S.textMode ? input : closeBtn).focus(); }, 0);
  }

  function closePanel() {
    if (!S.open) return;
    S.open = false;
    S.userClosed = true;
    clearTimeout(S.retryTimer);
    var ws = S.ws;
    S.ws = null;
    S.ready = false;
    if (ws) {
      try { if (ws.readyState === 1) ws.send(JSON.stringify({ type: 'end' })); } catch (e) { /* yok say */ }
      try { ws.close(1000, 'client_end'); } catch (e2) { /* yok say */ }
    }
    stopPlayback();
    stopMic();
    if (S.ctx) { try { S.ctx.close(); } catch (e3) { /* yok say */ } S.ctx = null; S.playGain = null; }
    finalizePartial('agent');
    finalizePartial('user');
    toolLine.hidden = true;
    retryBtn.hidden = true;
    showNotice('');
    S.textMode = false;
    S.micEnabled = true;
    setMicButton();
    setStatus('idle');
    panel.hidden = true;
    root.classList.remove('open');
    fab.setAttribute('aria-expanded', 'false');
    fab.focus();
  }

  function sendText() {
    var text = input.value.trim();
    if (!text) return;
    if (text.length > MAX_TEXT) text = text.slice(0, MAX_TEXT);
    if (!S.ws || S.ws.readyState !== 1 || !S.ready) {
      showNotice(T.lost);
      return;
    }
    finalizePartial('user');
    S.ws.send(JSON.stringify({ type: 'text', text: text }));
    onTranscript('user', text, true);
    input.value = '';
  }

  fab.addEventListener('click', openPanel);
  closeBtn.addEventListener('click', closePanel);
  retryBtn.addEventListener('click', function () {
    S.retriesLeft = 1;
    showNotice('');
    var ctx = ensureAudioContext();
    if (ctx && ctx.state === 'suspended') ctx.resume().catch(function () {});
    connect();
    if (!S.stream && !S.textMode) startMic();
  });
  form.addEventListener('submit', function (e) { e.preventDefault(); sendText(); });
  micBtn.addEventListener('click', function () {
    if (S.textMode || !S.stream) {
      // Yazılı moddayken tekrar izin istemeyi dene
      S.textMode = false;
      showNotice('');
      startMic();
      return;
    }
    S.micEnabled = !S.micEnabled;
    S.stream.getAudioTracks().forEach(function (t) { t.enabled = S.micEnabled; });
    setMicButton();
    refreshStatus();
  });
  panel.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { e.preventDefault(); closePanel(); }
  });
  // Sayfadan ayrılırken oturumu düzgün kapat
  window.addEventListener('pagehide', function () { if (S.open) closePanel(); });

  // ---------------------------------------------------------------------------
  // 11) Başlat: public_view al, düğmeyi göster
  // ---------------------------------------------------------------------------
  function mount() {
    if (!document.body) return setTimeout(mount, 20);
    document.body.appendChild(host);
  }

  applyAgent(S.agent);
  fetch(HTTP_BASE + '/api/agents/' + encodeURIComponent(AGENT_ID), { credentials: 'omit', mode: 'cors' })
    .then(function (r) {
      if (r.status === 404) throw new Error('not_found');
      if (!r.ok) throw new Error('http_' + r.status);
      return r.json();
    })
    .then(function (view) { applyAgent(view); mount(); })
    .catch(function (err) {
      if (err && err.message === 'not_found') {
        console.warn('[voice-agent] "' + AGENT_ID + '" adlı asistan bulunamadı; widget gösterilmiyor.');
        return;
      }
      // Ağ/CORS hatasında varsayılan görünümle yine de göster; bağlantı hatası panelde bildirilir
      console.warn('[voice-agent] asistan bilgisi alınamadı:', err && err.message);
      mount();
    });
})();
