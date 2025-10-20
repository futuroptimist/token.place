# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RELAY_PORT=5010 \
    RELAY_WORKERS=2 \
    RELAY_GRACEFUL_TIMEOUT=30 \
    RELAY_TIMEOUT=120

WORKDIR /app

RUN groupadd --gid 1000 relay \
    && useradd --uid 1000 --gid 1000 --home-dir /app --create-home \
       --shell /usr/sbin/nologin relay

COPY config/requirements_relay.txt ./config/requirements_relay.txt

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r config/requirements_relay.txt

COPY --chown=relay:relay . .

USER relay

EXPOSE ${RELAY_PORT}

ENTRYPOINT ["/bin/sh", "-c"]
CMD ["exec gunicorn --bind 0.0.0.0:${RELAY_PORT} --graceful-timeout ${RELAY_GRACEFUL_TIMEOUT} --timeout ${RELAY_TIMEOUT} --workers ${RELAY_WORKERS} --worker-tmp-dir /tmp relay:create_app()"]
