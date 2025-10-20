# syntax=docker/dockerfile:1.7
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RELAY_PORT=5010

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        build-essential \
        cmake \
        git \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY config/requirements_relay.txt ./requirements.txt

RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY relay.py ./
COPY api ./api
COPY config ./config
COPY utils ./utils
COPY static ./static

RUN groupadd --system relay \
    && useradd --system --home /app --shell /usr/sbin/nologin --gid relay relay \
    && chown -R relay:relay /app

USER relay

ARG VCS_REF=""
ARG BUILD_DATE=""
ARG VERSION=""

LABEL org.opencontainers.image.title="token.place relay" \
      org.opencontainers.image.description="Relay service for token.place" \
      org.opencontainers.image.url="https://github.com/tokenplace/token.place" \
      org.opencontainers.image.source="https://github.com/tokenplace/token.place" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.created="$BUILD_DATE" \
      org.opencontainers.image.version="$VERSION"

EXPOSE 5010

ENTRYPOINT ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${RELAY_PORT:-5010} --workers ${GUNICORN_WORKERS:-2} --threads ${GUNICORN_THREADS:-4} relay:app"]
