"""audio_codec testleri: uzunluk oranları, parçalı tutarlılık, frekans yanıtı, kırpma, reset."""

import numpy as np
import pytest

from server.audio_codec import Downsampler24to8, Upsampler8to16


def _sine(freq: float, rate: int, seconds: float, amp: float = 10000.0) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    return np.rint(amp * np.sin(2 * np.pi * freq * t)).astype("<i2").tobytes()


def _samples(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float64)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x**2)))


def _chunked(conv, data: bytes, rng: np.random.Generator) -> bytes:
    """Veriyi rastgele (tek bayt dahil) parçalara bölerek işler."""
    out = []
    i = 0
    while i < len(data):
        size = int(rng.choice([1, 1, 2, 3, 5, 7, int(rng.integers(1, 400))]))
        out.append(conv.process(data[i:i + size]))
        i += size
    return b"".join(out)


@pytest.mark.parametrize("cls", [Upsampler8to16, Downsampler24to8])
def test_empty_input(cls):
    conv = cls()
    assert conv.process(b"") == b""
    # Tek bayt: henüz tam örnek yok
    assert conv.process(b"\x01") == b""


def test_upsampler_length_ratio():
    conv = Upsampler8to16()
    pcm = _sine(500, 8000, 0.02)  # 160 örnek
    out = conv.process(pcm)
    assert len(out) == 2 * len(pcm)


def test_downsampler_length_ratio():
    conv = Downsampler24to8()
    pcm = _sine(1000, 24000, 0.02)  # 480 örnek
    out = conv.process(pcm)
    assert len(out) == len(pcm) // 3
    # Toplam çıkış yalnızca toplam girdiye bağlı (3'e bölünmeyen artıklar taşınır)
    conv.reset()
    total = b"".join(conv.process(pcm[i:i + 2]) for i in range(0, len(pcm), 2))
    assert len(total) == len(pcm) // 3


@pytest.mark.parametrize("cls,rate", [(Upsampler8to16, 8000), (Downsampler24to8, 24000)])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_chunked_equals_oneshot(cls, rate, seed):
    rng = np.random.default_rng(seed)
    noise = rng.integers(-20000, 20000, size=rate // 4).astype("<i2").tobytes()
    data = noise + _sine(700, rate, 0.1)
    whole = _samples(cls().process(data))
    parts = _samples(_chunked(cls(), data, rng))
    assert whole.size == parts.size
    assert np.max(np.abs(whole - parts)) <= 1


def test_downsampler_passband_1khz_amplitude():
    amp = 10000.0
    out = _samples(Downsampler24to8().process(_sine(1000, 24000, 0.5, amp)))
    steady = out[200:]  # filtre geçişini at
    measured = np.sqrt(2) * _rms(steady)
    assert abs(measured - amp) / amp < 0.10


def test_downsampler_rejects_6khz_alias():
    amp = 10000.0
    ref = _samples(Downsampler24to8().process(_sine(1000, 24000, 0.5, amp)))[200:]
    out = _samples(Downsampler24to8().process(_sine(6000, 24000, 0.5, amp)))[200:]
    attenuation_db = 20 * np.log10(_rms(ref) / max(_rms(out), 1e-9))
    assert attenuation_db >= 20


def test_upsampler_500hz_frequency_and_amplitude():
    amp = 10000.0
    out = _samples(Upsampler8to16().process(_sine(500, 8000, 0.5, amp)))
    steady = out[100:]
    measured = np.sqrt(2) * _rms(steady)
    assert abs(measured - amp) / amp < 0.10
    spectrum = np.abs(np.fft.rfft(steady * np.hanning(steady.size)))
    freqs = np.fft.rfftfreq(steady.size, d=1 / 16000)
    assert abs(freqs[np.argmax(spectrum)] - 500) < 20
    # 4 kHz üzeri görüntü (imaging) bileşenleri bastırılmış olmalı
    image = spectrum[freqs > 5000].max()
    assert 20 * np.log10(spectrum.max() / image) >= 40


@pytest.mark.parametrize("cls,rate", [(Upsampler8to16, 8000), (Downsampler24to8, 24000)])
def test_clipping_no_overflow(cls, rate):
    # Tam ölçekli kare dalga: FIR aşımı (overshoot) int16 sınırını geçer, kırpılmalı
    period = rate // 200
    square = np.where((np.arange(rate // 4) // (period // 2)) % 2 == 0, 32767, -32768)
    out = _samples(cls().process(square.astype("<i2").tobytes()))
    assert out.max() <= 32767 and out.min() >= -32768
    # Taşma olsaydı işaret dönerdi: tepe değerler kırpılmış olarak doymalı
    assert out.max() == 32767 and out.min() == -32768


@pytest.mark.parametrize("cls,rate", [(Upsampler8to16, 8000), (Downsampler24to8, 24000)])
def test_reset_restores_initial_state(cls, rate):
    data = _sine(440, rate, 0.05) + b"\x07"  # sonda artık tek bayt
    conv = cls()
    first = conv.process(data)
    conv.reset()
    again = conv.process(data)
    assert first == again
    assert first == cls().process(data)
