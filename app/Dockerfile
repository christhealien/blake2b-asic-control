# Blake2b ASIC Control: Maveth's web dashboard + login, fan curves, presets,
# schedules and the per-chip auto-tuner (one run per miner).
#
# Build context is this app/ folder. GitHub Actions builds and publishes it for every v* tag
# (.github/workflows/image.yml). By hand, from a checkout:  docker build -t blake2b-asic-control app/
#
# The dashboard it builds on (Maveth's, MIT) is downloaded at a pinned commit at
# build time, not copied into this repo; its LICENSE files come along into /app/webui and /app/python.
# To use your own mirror of it:  --build-arg UPSTREAM_REPO=https://example.com/you/your-mirror.git
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "pycryptodome>=3.19.1,<4"

ARG UPSTREAM_REPO=https://github.com/Maveth/goldshell-config.git
ARG UPSTREAM_REF=92838b350f324bfca2d1b70eef48b0861927fbe5
RUN git clone "$UPSTREAM_REPO" /src \
 && git -C /src checkout "$UPSTREAM_REF" \
 && mkdir -p /app \
 && cp -r /src/sc-lite/webui /app/webui \
 && cp -r /src/sc-lite/python /app/python \
 && rm -rf /src \
 && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY tuner_addon.py tuner.html fan_addon.py profiles.html schedule_addon.py shares_addon.py health_addon.py miner_safety.py notify_addon.py rejects_addon.py hashrate_addon.py report_addon.py schedule.html addon.js \
     auth_addon.py login.html theme.css theme.js widget_server.py install_addons.py /app/webui/
COPY asic_tuner.py /app/python/
COPY entrypoint.sh /app/entrypoint.sh
RUN cd /app/webui \
 && python install_addons.py \
 && python -m py_compile server.py tuner_addon.py fan_addon.py schedule_addon.py shares_addon.py health_addon.py miner_safety.py notify_addon.py rejects_addon.py hashrate_addon.py auth_addon.py widget_server.py /app/python/asic_tuner.py \
 && chmod 755 /app/entrypoint.sh

# Everything that changes lives in /data (the app's data folder on the Umbrel)
ENV SCLITE_WEBUI_HOST=0.0.0.0 \
    SCLITE_WEBUI_PORT=8787 \
    SCLITE_WEBUI_MINERS=/data/miners.json \
    SCLITE_TUNER_DATA=/data/tuner \
    PYTHONUNBUFFERED=1 \
    B2AC_VERSION=1.15.3

LABEL org.opencontainers.image.title="Blake2b ASIC Control" \
      org.opencontainers.image.version="1.15.3" \
      org.opencontainers.image.source="https://github.com/christhealien/blake2b-asic-control" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app/webui
USER 1000:1000
EXPOSE 8787
CMD ["/app/entrypoint.sh"]
