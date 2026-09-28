"""redact.py testleri: pozitif ve negatif örnekler."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from server.redact import is_valid_tckn, luhn_ok, redact

VALID_TCKN = "12345678950"   # algoritmik olarak geçerli (gerçek kişiye ait değil)
VALID_CARD = "4111111111111111"  # standart test kartı


@pytest.mark.parametrize(
    "text",
    [
        "ali.veli@example.com",
        "a.b+etiket@ornek.com.tr",
        "IletISIM_1@alt.alan-adi.io",
    ],
)
def test_email_masked(text):
    assert redact(f"yazın: {text} lütfen") == "yazın: [e-posta] lütfen"


@pytest.mark.parametrize(
    "phone",
    [
        "+90 532 123 45 67",
        "+905321234567",
        "0090 532 1234567",
        "90 532 123 45 67",
        "0532 123 45 67",
        "05321234567",
        "0532-123-45-67",
        "(0532) 123 45 67",
        "0 (532) 123-45-67",
        "+90 (532) 123 45 67",
        "532 123 45 67",
        "5321234567",
        "0532.123.45.67",
        "0212 555 12 34",
        "+90 216 555 12 34",
    ],
)
def test_phone_masked(phone):
    assert redact(f"numaram {phone}.") == "numaram [telefon]."


def test_tckn_masked_only_when_checksum_valid():
    assert is_valid_tckn(VALID_TCKN)
    assert redact(f"TC: {VALID_TCKN}") == "TC: [tckn]"
    # Checksum tutmayan 11 hane maskelenmez
    assert not is_valid_tckn("12345678901")
    assert redact("TC: 12345678901") == "TC: 12345678901"
    # İlk hane 0 olamaz
    assert not is_valid_tckn("02345678950")


@pytest.mark.parametrize(
    "card",
    [VALID_CARD, "4111 1111 1111 1111", "4111-1111-1111-1111", "5500 0000 0000 0004"],
)
def test_card_masked_with_luhn(card):
    assert luhn_ok(card)
    assert redact(f"kart {card} son") == "kart [kart] son"


def test_card_not_masked_when_luhn_fails():
    assert not luhn_ok("4111111111111112")
    assert redact("kart 4111-1111-1111-1112") == "kart 4111-1111-1111-1112"


@pytest.mark.parametrize(
    "text",
    [
        "Sipariş numarası 12345, toplam 2500 TL.",
        "Randevu 2026-09-28 saat 14:30.",
        "Fiyat 1.250,00 TL",
        "Yıl 1999 ve 2024 arası",
        "ürün kodu AB-532-1234",
        "e-posta adresim yok @ işareti var",
        "",
    ],
)
def test_negatives_untouched(text):
    assert redact(text) == text


def test_mixed_text():
    text = f"Ben Ayşe, tel 0532 123 45 67, mail ayse@ornek.com, TC {VALID_TCKN}, kart {VALID_CARD}."
    out = redact(text)
    assert out == "Ben Ayşe, tel [telefon], mail [e-posta], TC [tckn], kart [kart]."


def test_none_passthrough():
    assert redact(None) is None
