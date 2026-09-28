#!/bin/sh
# Asterisk konteyner girişi: şablonları ortam değişkenleriyle doldurur,
# eksik/hatalı ayarda anlaşılır Türkçe hata verip çıkar, sonra Asterisk'i başlatır.
set -eu

TEMPLATES=/opt/asterisk/templates
ETC=/etc/asterisk

hata() {
    echo "HATA: $*" >&2
    exit 1
}

# Asterisk yapılandırmasında ';' yorum başlatır; yeni satır yapıyı bozar.
guvenli_mi() {
    ad="$1"; deger="$2"
    case "$deger" in
        *";"*|*"
"*) hata "$ad içinde ';' veya satır sonu olamaz (Asterisk yapılandırmasını bozar)." ;;
    esac
}

# --- Varsayılanlar ----------------------------------------------------------
: "${RTP_START:=10000}"
: "${RTP_END:=10100}"
: "${TELEPHONY_AGENT:=botfusions-satis}"
: "${TELEPHONY_API_URL:=http://voice-agent:8090/telephony/asterisk/call}"
: "${AUDIOSOCKET_TARGET:=voice-agent:9092}"
: "${NETGSM_SIP_USER:=}"
: "${NETGSM_SIP_PASSWORD:=}"
: "${NETGSM_SIP_SERVER:=}"
: "${SOFTPHONE_PASSWORD:=}"
: "${EXTERNAL_IP:=}"
: "${TELEPHONY_SECRET:=}"
export RTP_START RTP_END TELEPHONY_AGENT TELEPHONY_API_URL AUDIOSOCKET_TARGET \
       NETGSM_SIP_USER NETGSM_SIP_PASSWORD NETGSM_SIP_SERVER SOFTPHONE_PASSWORD \
       EXTERNAL_IP TELEPHONY_SECRET

# --- Doğrulama --------------------------------------------------------------
[ -n "$TELEPHONY_SECRET" ] || hata "TELEPHONY_SECRET boş. .env dosyasına voice-agent ile AYNI uzun rastgele değeri yazın (ör. openssl rand -hex 32)."
[ -n "$EXTERNAL_IP" ] || hata "EXTERNAL_IP boş. VPS'te sunucunun sabit genel IP'sini, bilgisayarda test için 127.0.0.1 yazın."

case "$RTP_START$RTP_END" in
    *[!0-9]*) hata "RTP_START/RTP_END yalnızca rakam olmalı (ör. 10000 ve 10100)." ;;
esac
[ "$RTP_START" -lt "$RTP_END" ] || hata "RTP_START ($RTP_START) RTP_END'den ($RTP_END) küçük olmalı."

if [ -z "$NETGSM_SIP_USER" ] && [ -z "$SOFTPHONE_PASSWORD" ]; then
    hata "Ne Netgsm (NETGSM_SIP_USER) ne de test softphone'u (SOFTPHONE_PASSWORD) ayarlı; açılacak hat yok."
fi

if [ -n "$NETGSM_SIP_USER" ]; then
    [ -n "$NETGSM_SIP_PASSWORD" ] || hata "NETGSM_SIP_USER verilmiş ama NETGSM_SIP_PASSWORD boş."
    [ -n "$NETGSM_SIP_SERVER" ] || hata "NETGSM_SIP_USER verilmiş ama NETGSM_SIP_SERVER boş (Netgsm panelindeki SIP sunucu adresi)."
    guvenli_mi NETGSM_SIP_USER "$NETGSM_SIP_USER"
    guvenli_mi NETGSM_SIP_PASSWORD "$NETGSM_SIP_PASSWORD"
    guvenli_mi NETGSM_SIP_SERVER "$NETGSM_SIP_SERVER"
fi

if [ -n "$SOFTPHONE_PASSWORD" ]; then
    [ "${#SOFTPHONE_PASSWORD}" -ge 12 ] || hata "SOFTPHONE_PASSWORD en az 12 karakter olmalı (5060 portu internete açıksa tarayıcı botlar dener)."
    guvenli_mi SOFTPHONE_PASSWORD "$SOFTPHONE_PASSWORD"
fi
guvenli_mi EXTERNAL_IP "$EXTERNAL_IP"

# --- Şablonları işle --------------------------------------------------------
# envsubst'e değişken listesi AÇIKÇA verilir; aksi halde dialplan'daki
# ${EXTEN} gibi Asterisk değişkenleri de silinirdi.
render() {
    envsubst "$2" < "$TEMPLATES/$1" > "$ETC/$3"
}

# Değişken içermeyen dosyalar olduğu gibi kopyalanır.
for f in "$TEMPLATES"/*.conf; do
    case "$(basename "$f")" in
        pjsip.conf|rtp.conf) ;;
        *) cp "$f" "$ETC/" ;;
    esac
done
render pjsip.conf '${EXTERNAL_IP}' pjsip.conf
render rtp.conf '${RTP_START} ${RTP_END}' rtp.conf

if [ -n "$NETGSM_SIP_USER" ]; then
    render pjsip_netgsm.conf.tmpl '${NETGSM_SIP_USER} ${NETGSM_SIP_PASSWORD} ${NETGSM_SIP_SERVER}' pjsip_netgsm.conf
    echo "Netgsm trunk etkin: $NETGSM_SIP_USER@$NETGSM_SIP_SERVER"
else
    : > "$ETC/pjsip_netgsm.conf"
    echo "Netgsm trunk kapalı (NETGSM_SIP_USER boş)."
fi

if [ -n "$SOFTPHONE_PASSWORD" ]; then
    render pjsip_softphone.conf.tmpl '${SOFTPHONE_PASSWORD}' pjsip_softphone.conf
    echo "Test softphone'u etkin: kullanıcı 1001, aranacak numara 100."
else
    : > "$ETC/pjsip_softphone.conf"
fi
# Şifre içeren dosyaları yalnızca asterisk kullanıcısı okuyabilsin.
chmod 600 "$ETC"/pjsip*.conf

echo "Dış IP: $EXTERNAL_IP · RTP: $RTP_START-$RTP_END/udp · AudioSocket: $AUDIOSOCKET_TARGET"

if [ "$#" -eq 0 ]; then
    set -- asterisk -f -n -vvv
fi
exec "$@"
