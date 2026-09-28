// Botfusions Voice Agent widget'ı için uçtan uca Playwright testi.
// Sahte sunucu (mock_server.py) çalışırken çalıştırılır: tests/widget/run.sh
import { chromium } from '/opt/node22/lib/node_modules/playwright/index.mjs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const BASE = process.env.MOCK_BASE || 'http://127.0.0.1:8765';
const HERE = path.dirname(fileURLToPath(import.meta.url));
const AGENT = 'demo-agent';

let passed = 0;
let failed = 0;
function check(cond, label, extra) {
  if (cond) { passed++; console.log('  ✓ ' + label); }
  else { failed++; console.log('  ✗ ' + label + (extra !== undefined ? '  → ' + JSON.stringify(extra) : '')); }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function state() { return (await fetch(BASE + '/__state')).json(); }
async function push(s) {
  const r = await fetch(BASE + '/__push/' + s, { method: 'POST' });
  if (!r.ok) throw new Error('push ' + s + ' → ' + r.status);
}
async function reset() { await fetch(BASE + '/__reset', { method: 'POST' }); }
// Koşul doğru olana kadar bekler (zaman aşımında son değeri döndürür)
async function waitFor(fn, timeout = 5000) {
  const t0 = Date.now();
  let v;
  while (Date.now() - t0 < timeout) {
    v = await fn();
    if (v) return v;
    await sleep(50);
  }
  return v;
}
const dbg = (page) => page.evaluate(() => window.__voiceAgentDebug && window.__voiceAgentDebug());

const browser = await chromium.launch({
  args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'],
});

const consoleErrors = [];
function watchConsole(page) {
  page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
}

try {
  // ------------------------------------------------------------------ masaüstü
  console.log('Masaüstü senaryosu');
  await reset();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, permissions: ['microphone'] });
  const page = await ctx.newPage();
  watchConsole(page);
  await page.goto(`${BASE}/demo/${AGENT}`);

  const fab = page.locator('#botfusions-voice-agent .fab');
  await fab.waitFor({ state: 'visible', timeout: 5000 });
  check(await fab.isVisible(), 'buton görünür');
  check((await fab.getAttribute('aria-label')).includes('Botfusions Asistan'), 'buton aria-label public_view başlığını içeriyor');
  const d0 = await dbg(page);
  check(d0.wsBase === BASE.replace('http', 'ws'), 'ws adresi script src\'den türetildi', d0.wsBase);

  await fab.click();
  const ready = await waitFor(async () => { const d = await dbg(page); return d.ready && d.status === 'listening' && d; });
  check(!!ready, 'ready alındı, durum "Dinliyor"', await dbg(page));
  check((await page.locator('#botfusions-voice-agent .badge').textContent()).includes('Dinliyor'), 'rozet metni "Dinliyor"');

  const st1 = await waitFor(async () => { const s = await state(); return s.binary_count >= 10 && s; }, 6000);
  check(!!st1, 'sunucu ≥10 ikili ses parçası aldı', await state());
  const sizes = Object.keys((st1 || (await state())).binary_sizes);
  check(sizes.length === 1 && sizes[0] === '1280', 'tüm parçalar 1280 bayt (40 ms @16 kHz PCM16)', sizes);
  check((await state()).first_message_types[0] === 'start', 'ilk mesaj "start"');
  const dChunks = await dbg(page);
  check(dChunks.chunksSent >= 10, 'widget gönderilen parça sayısını raporluyor', dChunks.chunksSent);
  // 40 ms parça hızı kabaca doğru mu? (2 sn'de ~50 parça)
  const c1 = (await state()).binary_count; await sleep(2000); const c2 = (await state()).binary_count;
  check(c2 - c1 >= 40 && c2 - c1 <= 60, '2 sn\'de ~50 parça (gerçek zamanlı hız)', c2 - c1);

  await push('speak');
  const userBubble = page.locator('#botfusions-voice-agent .msg.user');
  const agentBubble = page.locator('#botfusions-voice-agent .msg.agent');
  await agentBubble.first().waitFor({ timeout: 3000 });
  await sleep(200);
  check((await userBubble.count()) === 1, 'kısmi+final kullanıcı transkripti tek balonda', await userBubble.count());
  check((await userBubble.first().textContent()).includes('fiyatlarınızı öğrenebilir'), 'kullanıcı balonu final metne güncellendi');
  check((await agentBubble.count()) === 1, 'asistan kısmi transkriptleri tek balonda güncellendi', await agentBubble.count());
  check((await agentBubble.first().textContent()).includes('990 TL'), 'asistan balonu son kısmi metni gösteriyor');
  check(await agentBubble.first().evaluate((e) => e.classList.contains('partial')), 'asistan balonu hâlâ kısmi (final:false)');
  const dSpeak = await waitFor(async () => { const d = await dbg(page); return d.queueSize > 0 && d; }, 3000);
  check(!!dSpeak && dSpeak.queueSize > 0, 'çalma kuyruğunda ses var', await dbg(page));
  check(dSpeak && dSpeak.queuedSeconds > 1.5, 'kuyrukta >1.5 sn planlanmış ses (kesintisiz sıralama)', dSpeak && dSpeak.queuedSeconds);
  check((await dbg(page)).status === 'speaking', 'durum "Konuşuyor"');
  check((await dbg(page)).bytesReceived === 144000, '3 sn 24 kHz ses alındı (144000 bayt)', (await dbg(page)).bytesReceived);

  await push('interrupt');
  const dInt = await waitFor(async () => { const d = await dbg(page); return d.interruptions === 1 && d; }, 2000);
  check(dInt && dInt.queueSize === 0, 'interrupted sonrası kuyruk 0', dInt);
  check(dInt && dInt.status === 'listening', 'interrupted sonrası durum "Dinliyor"', dInt && dInt.status);
  check(!(await agentBubble.first().evaluate((e) => e.classList.contains('partial'))), 'kesilen asistan balonu sabitlendi');

  await push('tool');
  const toolLine = page.locator('#botfusions-voice-agent .toolline');
  await sleep(400);
  const toolText = await toolLine.textContent();
  check((await toolLine.isVisible()) && toolText.includes('randevu olustur tamamlandı'), 'araç durum satırı gösterildi', toolText);

  const input = page.locator('#botfusions-voice-agent input');
  await input.fill('Yarın için randevu almak istiyorum');
  await input.press('Enter');
  const st2 = await waitFor(async () => { const s = await state(); return s.texts.length === 1 && s; });
  check(st2 && st2.texts[0] === 'Yarın için randevu almak istiyorum', 'yazılı mesaj sunucuya gitti', await state());
  check((await userBubble.count()) === 2, 'yazılı mesaj kullanıcı balonu olarak eklendi');
  check((await input.inputValue()) === '', 'metin kutusu temizlendi');

  // Beklenmedik kopma → tek otomatik yeniden bağlanma
  await push('drop');
  const st3 = await waitFor(async () => { const s = await state(); return s.starts === 2 && s.active && s; }, 5000);
  check(!!st3, 'bağlantı koptuktan sonra otomatik yeniden bağlandı', await state());
  const dRe = await waitFor(async () => { const d = await dbg(page); return d.ready && d; }, 3000);
  check(dRe && dRe.connects === 2 && dRe.status === 'listening', 'yeniden bağlantı sonrası "Dinliyor"', dRe);

  // Klavye erişimi: kapat düğmesine odak ve görünür focus
  await page.locator('#botfusions-voice-agent .close').focus();
  const outline = await page.locator('#botfusions-voice-agent .close').evaluate((e) => getComputedStyle(e).outlineStyle);
  check(outline === 'solid', 'odakta görünür outline', outline);
  const live = await page.locator('#botfusions-voice-agent .log').getAttribute('aria-live');
  check(live === 'polite', 'transkript aria-live="polite"');

  await page.screenshot({ path: path.join(HERE, 'desktop.png') });

  // Kapat → end mesajı, mikrofon ve AudioContext kapanır
  await page.locator('#botfusions-voice-agent .close').click();
  const st4 = await waitFor(async () => { const s = await state(); return s.ends === 1 && !s.active && s; });
  check(!!st4, 'kapat → sunucu "end" aldı', await state());
  const dClose = await dbg(page);
  check(dClose.audioContextState === 'none' && !dClose.micActive && dClose.wsState === -1, 'mikrofon, AudioContext ve WS kapandı', dClose);
  check(await fab.isVisible(), 'kapattıktan sonra buton geri geldi');
  check(await fab.evaluate((e) => e.getRootNode().activeElement === e), 'odak butona döndü');
  await ctx.close();

  // ------------------------------------------------------------------ mobil 390 px
  console.log('Mobil (390 px) senaryosu');
  await reset();
  const mctx = await browser.newContext({
    viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true, permissions: ['microphone'],
  });
  const mpage = await mctx.newPage();
  watchConsole(mpage);
  await mpage.goto(`${BASE}/demo/${AGENT}`);
  const mfab = mpage.locator('#botfusions-voice-agent .fab');
  await mfab.waitFor({ state: 'visible' });
  await mpage.screenshot({ path: path.join(HERE, 'mobile-390-closed.png') });
  await mfab.tap();
  await waitFor(async () => (await dbg(mpage)).ready);
  await push('speak');
  await mpage.locator('#botfusions-voice-agent .msg.agent').first().waitFor();
  await push('tool');
  await sleep(300);
  const box = await mpage.locator('#botfusions-voice-agent .panel').boundingBox();
  check(box && Math.round(box.width) === 390 && Math.round(box.x) === 0, 'mobilde panel tam genişlik', box);
  check(box && Math.round(box.y + box.height) === 844, 'mobilde panel alta yapışık (alt sayfa)', box);
  const overflow = await mpage.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  check(!overflow, 'yatay kaydırma yok');
  await mpage.screenshot({ path: path.join(HERE, 'mobile-390.png') });

  // Limit → Türkçe mesaj, otomatik yeniden bağlanma yok, "Tekrar bağlan" düğmesi
  await push('limit');
  const retry = mpage.locator('#botfusions-voice-agent .retry');
  await retry.waitFor({ state: 'visible', timeout: 3000 });
  const noticeText = await mpage.locator('#botfusions-voice-agent .notice').textContent();
  check(noticeText.includes('Görüşme süresi doldu'), 'limit mesajı Türkçe gösterildi', noticeText);
  await sleep(1200);
  check((await state()).starts === 1, 'limit sonrası otomatik yeniden bağlanılmadı');
  await mpage.screenshot({ path: path.join(HERE, 'mobile-390-limit.png') });
  await retry.tap();
  const st5 = await waitFor(async () => { const s = await state(); return s.starts === 2 && s; });
  check(!!st5, '"Tekrar bağlan" yeni oturum açtı');
  await mctx.close();

  // ------------------------------------------------------------------ mikrofon izni reddedildi
  console.log('Mikrofon izni reddedildi senaryosu');
  await reset();
  // Headless Chromium izin istemini gösteremediği için gerçek tarayıcının "Engelle" yanıtı
  // (NotAllowedError) getUserMedia sahtelenerek taklit edilir.
  const dctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  await dctx.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('Permission denied', 'NotAllowedError'));
  });
  const dpage = await dctx.newPage();
  watchConsole(dpage);
  await dpage.goto(`${BASE}/demo/${AGENT}`);
  await dpage.locator('#botfusions-voice-agent .fab').click();
  const dText = await waitFor(async () => { const d = await dbg(dpage); return d.textMode && d.ready && d; }, 6000);
  check(!!dText && dText.status === 'text', 'izin yoksa yazılı moda düştü', await dbg(dpage));
  const dn = await dpage.locator('#botfusions-voice-agent .notice').textContent();
  check(dn.includes('Mikrofon izni verilmedi'), 'Türkçe açıklama gösterildi', dn);
  await dpage.locator('#botfusions-voice-agent input').fill('Sesli konuşamıyorum');
  await dpage.locator('#botfusions-voice-agent .send').click();
  const st6 = await waitFor(async () => { const s = await state(); return s.texts.length === 1 && s; });
  check(!!st6 && (await state()).binary_count === 0, 'yazılı modda mesaj gitti, ses gönderilmedi', await state());
  await dctx.close();

  const relevantErrors = consoleErrors.filter((e) => !/favicon/.test(e));
  check(relevantErrors.length === 0, 'sayfada JS hatası yok', relevantErrors);
} catch (e) {
  failed++;
  console.error('TEST HATASI:', e);
} finally {
  await browser.close();
}

console.log(`\nSonuç: ${passed} başarılı, ${failed} başarısız`);
process.exit(failed ? 1 : 0);
