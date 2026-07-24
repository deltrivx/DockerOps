# DockerOps — built only via GitHub Actions (remote). Do not rely on local build+push.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    DOCKEROPS_HOST=0.0.0.0 \
    DOCKEROPS_PORT=8080 \
    DOCKEROPS_DATA_DIR=/data \
    DEBIAN_FRONTEND=noninteractive

# System + Docker CLI (for `docker compose` takeover against mounted socket)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tzdata gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
         | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
         > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && docker --version \
    && docker compose version \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /app/requirements.txt

COPY app/ /app/

RUN mkdir -p /data /unraid/templates-user \
    && useradd --create-home --shell /bin/bash --uid 1000 appuser \
    && chown -R appuser:appuser /app /data

# Docker socket is typically root-owned; run as root for socket + compose CLI.
USER root

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=8s --start-period=25s --retries=3 \
  CMD python -c "import os,urllib.request; p=os.environ.get('DOCKEROPS_PORT','8080'); urllib.request.urlopen(f'http://127.0.0.1:{p}/api/health', timeout=5)" || exit 1

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
