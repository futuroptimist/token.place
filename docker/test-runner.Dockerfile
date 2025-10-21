# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        git \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY requirements.txt ./
COPY config/requirements_server.txt config/requirements_server.txt
COPY config/requirements_relay.txt config/requirements_relay.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r config/requirements_server.txt \
    && pip install --no-cache-dir -r config/requirements_relay.txt

COPY package.json package-lock.json ./
RUN npm ci --omit=optional \
    && npx playwright install --with-deps chromium

COPY . .

CMD ["./run_all_tests.sh"]
