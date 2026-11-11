# Linux tools for native Odoo integration tests (not the production image).
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
COPY --from=ghcr.io/astral-sh/uv:0.12.12 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pytest python3-venv postgresql-client-16 git ca-certificates restic \
    build-essential libffi-dev libjpeg-dev libldap2-dev libpq-dev \
    libsasl2-dev libxml2-dev libxslt1-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work
