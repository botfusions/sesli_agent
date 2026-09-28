# Botfusions telefon hattı — Asterisk 20 (Ubuntu 24.04 "noble" universe paketi)
# Pakette gerekli modüller var: app_audiosocket, res_audiosocket, func_curl,
# res_pjsip*, chan_pjsip, codec_alaw, codec_ulaw.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        asterisk asterisk-modules gettext-base ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 # Paketin örnek yapılandırmaları yerine yalnızca bizimkiler kullanılır.
 && rm -rf /etc/asterisk/* \
 && mkdir -p /var/run/asterisk /var/log/asterisk /var/lib/asterisk /var/spool/asterisk \
 && chown -R asterisk:asterisk /etc/asterisk /var/run/asterisk /var/log/asterisk \
        /var/lib/asterisk /var/spool/asterisk

COPY conf/ /opt/asterisk/templates/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0644 /opt/asterisk/templates/* && chmod 0755 /usr/local/bin/entrypoint.sh

# root olmayan kullanıcı (paketin oluşturduğu "asterisk")
USER asterisk
WORKDIR /var/lib/asterisk

EXPOSE 5060/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD asterisk -rx "core show uptime" >/dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["asterisk", "-f", "-n", "-vvv"]
