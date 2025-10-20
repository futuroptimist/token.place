# syntax=docker/dockerfile:1.6
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake \
    && rm -rf /var/lib/apt/lists/*

COPY config/requirements_relay.txt /tmp/requirements.txt
RUN pip install --upgrade pip \
    && pip install --no-cache-dir --prefix /opt/venv -r /tmp/requirements.txt

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RELAY_PORT=5010

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY relay.py ./
COPY api ./api
COPY config ./config
COPY static ./static
COPY utils ./utils
COPY encrypt.py ./encrypt.py
COPY docker/relay/entrypoint.sh /usr/local/bin/relay-entrypoint

RUN addgroup --system relay \
    && adduser --system --ingroup relay relay \
    && chown -R relay:relay /app /usr/local/bin/relay-entrypoint \
    && chmod 0555 /usr/local/bin/relay-entrypoint

USER relay

EXPOSE 5010

ENTRYPOINT ["/usr/local/bin/relay-entrypoint"]
CMD []
