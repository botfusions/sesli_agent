/*
 * Botfusions Voice Agent — mikrofon AudioWorklet işlemcisi.
 *
 * Görev: cihazın örnekleme hızındaki (genelde 44.1/48 kHz) Float32 mikrofon sesini
 * doğrusal enterpolasyonla 16 kHz'e indirir, Int16 little-endian'a çevirir ve
 * 40 ms'lik (640 örnek = 1280 bayt) parçalar halinde ana iş parçacığına gönderir.
 *
 * Ana iş parçacığından gelen mesajlar:
 *   {type: "reset"} → birikmiş örnekleri ve yeniden örnekleme durumunu sıfırlar.
 * Ana iş parçacığına giden mesajlar:
 *   ArrayBuffer (1280 bayt, transfer edilir).
 */
/* global sampleRate, registerProcessor, AudioWorkletProcessor */
'use strict';

var TARGET_RATE = 16000;
var CHUNK_SAMPLES = 640; // 40 ms @ 16 kHz

class PcmDownsampler extends AudioWorkletProcessor {
  constructor() {
    super();
    // Giriş/çıkış hız oranı (ör. 48000 / 16000 = 3)
    this.ratio = sampleRate / TARGET_RATE;
    // Bir sonraki çıkış örneğinin, mevcut giriş bloğuna göre kesirli konumu.
    // -1 ile 0 arası: önceki bloğun son örneği ile bu bloğun ilk örneği arasında.
    this.pos = 0;
    this.last = 0;
    this.out = new Int16Array(CHUNK_SAMPLES);
    this.outLen = 0;
    var self = this;
    this.port.onmessage = function (ev) {
      if (ev.data && ev.data.type === 'reset') {
        self.pos = 0;
        self.last = 0;
        self.outLen = 0;
      }
    };
  }

  // Tek bir Float32 örneği Int16'ya çevirip çıkış tamponuna ekler; dolunca gönderir.
  push(v) {
    if (v > 1) v = 1;
    else if (v < -1) v = -1;
    this.out[this.outLen++] = v < 0 ? v * 0x8000 : v * 0x7fff;
    if (this.outLen === CHUNK_SAMPLES) {
      // Int16Array platformun bayt sırasını kullanır; tüm hedef tarayıcılar little-endian.
      var buf = this.out.buffer;
      this.port.postMessage(buf, [buf]);
      this.out = new Int16Array(CHUNK_SAMPLES);
      this.outLen = 0;
    }
  }

  process(inputs) {
    var input = inputs[0];
    if (!input || input.length === 0 || !input[0]) return true;
    // Mono: birden çok kanal varsa ilkini kullanır (mikrofon zaten mono istenir).
    var data = input[0];
    var n = data.length;
    var pos = this.pos;
    var ratio = this.ratio;
    // pos + 1 < n olduğu sürece enterpolasyon için iki komşu örnek mevcut.
    while (pos + 1 < n) {
      var i = Math.floor(pos);
      var frac = pos - i;
      var s0 = i < 0 ? this.last : data[i];
      var s1 = data[i + 1];
      this.push(s0 + (s1 - s0) * frac);
      pos += ratio;
    }
    this.pos = pos - n;
    this.last = data[n - 1];
    return true;
  }
}

registerProcessor('botfusions-pcm16', PcmDownsampler);
