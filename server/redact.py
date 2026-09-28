"""Kişisel veri maskeleme.

Transkript ve olay metinleri veritabanına yazılmadan önce buradan geçer.
Maskelenen türler: e-posta, TR telefon, 11 haneli TCKN (checksum ile), 16 haneli kart (Luhn ile).
"""

from __future__ import annotations

import re

__all__ = ["redact", "is_valid_tckn", "luhn_ok"]

# E-posta: yerel kısım + alan adı + en az 2 harfli uzantı
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])"
)

# 16 haneli kart: rakamlar arasında tek boşluk veya tire olabilir (4-4-4-4 veya bitişik)
_CARD_RE = re.compile(r"(?<![\d])\d(?:[ -]?\d){15}(?![\d])")

# 11 haneli TCKN: ilk hane 0 olamaz; bitişik yazılır (araya boşluk koyanlar da yakalanır)
_TCKN_RE = re.compile(r"(?<![\d])[1-9](?:[ ]?\d){10}(?![\d])")

# Ayırıcı: boşluk, tire, nokta (tekrarlı olabilir)
_SEP = r"[\s.\-]*"

# TR cep telefonu: (+90 | 0090 | 90 | 0)? (5xx) xxx xx xx — parantez ve ayırıcı varyasyonlarıyla.
# Önek yoksa da 5xx ile başlayan 10 hane telefon kabul edilir.
_MOBILE_RE = re.compile(
    r"(?<![\w+(])\(?"
    r"(?:(?:\+|00)?90" + _SEP + r"|0" + _SEP + r")?"
    r"\(?" + _SEP + r"5\d{2}" + _SEP + r"\)?" + _SEP +
    r"\d{3}" + _SEP + r"\d{2}" + _SEP + r"\d{2}"
    r"(?![\d])"
)

# TR sabit hat: önek ZORUNLU (+90 veya 0) + 2xx/3xx/4xx alan kodu + 7 hane (yanlış pozitifi azaltmak için)
_LANDLINE_RE = re.compile(
    r"(?<![\w+(])\(?"
    r"(?:(?:\+|00)?90" + _SEP + r"|0" + _SEP + r")"
    r"\(?" + _SEP + r"[2-4]\d{2}" + _SEP + r"\)?" + _SEP +
    r"\d{3}" + _SEP + r"\d{2}" + _SEP + r"\d{2}"
    r"(?![\d])"
)


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def luhn_ok(number: str) -> bool:
    """Luhn (mod 10) kontrolü; yalnızca rakamlara bakar."""
    digits = _digits(number)
    if not digits:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def is_valid_tckn(number: str) -> bool:
    """TC Kimlik No algoritmik doğrulaması (10. ve 11. hane kontrolü)."""
    digits = _digits(number)
    if len(digits) != 11 or digits[0] == "0":
        return False
    d = [int(c) for c in digits]
    odd = d[0] + d[2] + d[4] + d[6] + d[8]
    even = d[1] + d[3] + d[5] + d[7]
    if ((odd * 7) - even) % 10 != d[9]:
        return False
    return sum(d[:10]) % 10 == d[10]


def _card_sub(m: re.Match) -> str:
    return "[kart]" if luhn_ok(m.group(0)) else m.group(0)


def _tckn_sub(m: re.Match) -> str:
    return "[tckn]" if is_valid_tckn(m.group(0)) else m.group(0)


def redact(text: str) -> str:
    """Metindeki kişisel verileri etiketlerle değiştirir. None/boş girdi olduğu gibi döner."""
    if not text:
        return text
    if not isinstance(text, str):
        text = str(text)
    # Sıra önemli: e-posta rakam içerebilir; kart ve TCKN telefondan önce doğrulanır.
    out = _EMAIL_RE.sub("[e-posta]", text)
    out = _CARD_RE.sub(_card_sub, out)
    out = _TCKN_RE.sub(_tckn_sub, out)
    out = _MOBILE_RE.sub("[telefon]", out)
    out = _LANDLINE_RE.sub("[telefon]", out)
    return out
