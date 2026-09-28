"""Telefon hattı ile Gemini Live arasında PCM örnekleme hızı dönüşümü.

- Telefon (Asterisk AudioSocket): 16-bit LE mono, 8 kHz.
- Gemini Live girişi 16 kHz, çıkışı 24 kHz (16-bit LE mono).

İki durumlu (streaming) dönüştürücü sağlanır:

- `Upsampler8to16`: 8 kHz → 16 kHz. 31 taplı yarım bant (half-band) pencereli sinc
  FIR ile çok fazlı (polyphase) 2x ara değerleme. Gecikme ~0.9 ms.
- `Downsampler24to8`: 24 kHz → 8 kHz. 63 taplı pencereli sinc alçak geçiren FIR
  (kesim ~3.4 kHz, Blackman penceresi) + 3'te 1 seçme. Gecikme ~1.3 ms.

Her iki sınıf da filtre geçmişini ve tamamlanmamış baytları/örnekleri çağrılar arasında
taşır; böylece parçalı işlem ile tek seferde işlem aynı (en fazla ±1 LSB) sonucu verir.
`audioop` kullanılmaz (Python 3.13'te kaldırıldı); yalnızca numpy.
"""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

__all__ = ["Upsampler8to16", "Downsampler24to8"]

_INT16_MIN = -32768
_INT16_MAX = 32767

UP_TAPS = 31  # 16 kHz'de yarım bant ara değerleme filtresi (tek sayı olmalı)
DOWN_TAPS = 63  # 24 kHz'de anti-alias filtresi
DOWN_CUTOFF_HZ = 3400.0
DOWN_RATE_HZ = 24000.0


# ---- yardımcılar -------------------------------------------------------------


def _split_bytes(pending: bytes, pcm: bytes) -> tuple[np.ndarray, bytes]:
    """Önceki artık bayt + yeni veriyi int16 örneklere çevirir; tek kalan baytı döndürür."""
    data = pending + pcm if pending else pcm
    usable = len(data) - (len(data) % 2)
    samples = np.frombuffer(data[:usable], dtype="<i2").astype(np.float64)
    return samples, data[usable:]


def _to_pcm(values: np.ndarray) -> bytes:
    """Kayan noktalı örnekleri yuvarlayıp int16 aralığına kırpar ve LE bayta çevirir."""
    out = np.clip(np.rint(values), _INT16_MIN, _INT16_MAX)
    return out.astype("<i2").tobytes()


def _halfband_phases(taps: int) -> tuple[np.ndarray, np.ndarray]:
    """2x ara değerleme için yarım bant filtresinin iki fazını (ters çevrilmiş) üretir.

    Dönen diziler `sliding_window_view` pencereleriyle doğrudan çarpılacak şekilde
    zamanda ters sıradadır (en eski örnek başta). Her faz DC kazancı 1 olacak şekilde
    normalize edilir; böylece sabit sinyal birebir korunur.
    """
    if taps % 2 == 0:
        raise ValueError("Yarım bant filtre tap sayısı tek olmalı.")
    center = (taps - 1) / 2
    n = np.arange(taps)
    # Kesim fs/4 (8 kHz'lik orijinal Nyquist'i = 4 kHz), sıfır ekleme kaybı için kazanç 2
    h = np.sinc((n - center) / 2.0) * np.blackman(taps + 2)[1:-1]
    phase0 = h[0::2].copy()  # çift indisler
    phase1 = h[1::2].copy()  # tek indisler
    phase0 /= phase0.sum()
    phase1 /= phase1.sum()
    # Her iki fazı da aynı pencere uzunluğuna getir (kısa olanı sonuna sıfırla doldur)
    length = max(len(phase0), len(phase1))
    phase0 = np.pad(phase0, (0, length - len(phase0)))
    phase1 = np.pad(phase1, (0, length - len(phase1)))
    return phase0[::-1].copy(), phase1[::-1].copy()


def _lowpass(taps: int, cutoff_hz: float, rate_hz: float) -> np.ndarray:
    """Blackman pencereli sinc alçak geçiren FIR (DC kazancı 1, simetrik)."""
    center = (taps - 1) / 2
    n = np.arange(taps)
    fc = cutoff_hz / rate_hz  # normalize kesim (döngü/örnek)
    h = 2 * fc * np.sinc(2 * fc * (n - center)) * np.blackman(taps + 2)[1:-1]
    return h / h.sum()


# ---- 8 kHz → 16 kHz ----------------------------------------------------------


class Upsampler8to16:
    """8 kHz → 16 kHz durumlu dönüştürücü (16-bit LE mono).

    Her giriş örneği için iki çıkış örneği üretilir. Filtre geçmişi ve tek sayıda bayt
    gelirse artık bayt sonraki `process` çağrısına taşınır.
    """

    _PHASE0, _PHASE1 = _halfband_phases(UP_TAPS)
    _HIST = len(_PHASE0) - 1

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Filtre geçmişini ve artık baytı temizler (yeni çağrı başlangıcı)."""
        self._history = np.zeros(self._HIST, dtype=np.float64)
        self._pending = b""

    def process(self, pcm: bytes) -> bytes:
        """8 kHz PCM parçasını 16 kHz PCM'e çevirir; boş/yetersiz girdide b"" döner."""
        samples, self._pending = _split_bytes(self._pending, pcm)
        if samples.size == 0:
            return b""
        buf = np.concatenate((self._history, samples))
        windows = sliding_window_view(buf, len(self._PHASE0))
        out = np.empty(samples.size * 2, dtype=np.float64)
        out[0::2] = windows @ self._PHASE0
        out[1::2] = windows @ self._PHASE1
        self._history = buf[-self._HIST:].copy()
        return _to_pcm(out)


# ---- 24 kHz → 8 kHz ----------------------------------------------------------


class Downsampler24to8:
    """24 kHz → 8 kHz durumlu dönüştürücü (16-bit LE mono).

    Önce ~3.4 kHz kesimli alçak geçiren FIR (aliasing önleme), ardından her 3 örnekten
    biri seçilir. Filtre geçmişi, 3'e bölünmeyen artık örnekler ve tek kalan bayt
    parçalar arasında taşınır.
    """

    _TAPS_REV = _lowpass(DOWN_TAPS, DOWN_CUTOFF_HZ, DOWN_RATE_HZ)[::-1].copy()
    _HIST = DOWN_TAPS - 1
    _FACTOR = 3

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Filtre geçmişini, artık örnekleri ve artık baytı temizler."""
        # _buf: sıradaki çıkışın en yeni örneğinden önceki _HIST örnek + bekleyen örnekler
        self._buf = np.zeros(self._HIST, dtype=np.float64)
        self._pending = b""

    def process(self, pcm: bytes) -> bytes:
        """24 kHz PCM parçasını 8 kHz PCM'e çevirir; boş/yetersiz girdide b"" döner."""
        samples, self._pending = _split_bytes(self._pending, pcm)
        if samples.size == 0:
            return b""
        buf = np.concatenate((self._buf, samples))
        available = buf.size - self._HIST  # en yeni örnek olarak kullanılabilecek örnek sayısı
        n_out = (available + self._FACTOR - 1) // self._FACTOR
        if n_out <= 0:
            self._buf = buf
            return b""
        windows = sliding_window_view(buf, DOWN_TAPS)[:: self._FACTOR][:n_out]
        out = windows @ self._TAPS_REV
        self._buf = buf[n_out * self._FACTOR:].copy()
        return _to_pcm(out)
