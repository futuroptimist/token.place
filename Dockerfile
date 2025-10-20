FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system relay \
    && useradd --system --create-home --home /home/relay --gid relay --uid 1000 relay

COPY config/requirements_relay.txt /tmp/requirements.txt

RUN python -m pip install --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

COPY --chown=relay:relay api /app/api
COPY --chown=relay:relay config /app/config
COPY --chown=relay:relay utils /app/utils
COPY --chown=relay:relay static /app/static
COPY --chown=relay:relay relay.py config.py encrypt.py /app/
COPY --chown=relay:relay docker/relay/entrypoint.sh /usr/local/bin/relay-entrypoint.sh

RUN chmod +x /usr/local/bin/relay-entrypoint.sh

USER relay

ENV RELAY_HOST=0.0.0.0 \
    RELAY_PORT=5010

EXPOSE 5010

ENTRYPOINT ["/usr/local/bin/relay-entrypoint.sh"]
