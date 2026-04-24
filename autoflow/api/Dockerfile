FROM python:3.12-slim

# Docker CLI static binary — lets the API start/stop the bot container via
# the mounted socket. We use the official static tarball instead of
# `apt-get install docker.io` because the Debian package silently fails on
# some slim images, and the static binary is small (~60MB) and arch-aware.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && arch="$(uname -m)" \
 && case "$arch" in \
      x86_64)  darch=x86_64 ;; \
      aarch64) darch=aarch64 ;; \
      *) echo "unsupported arch $arch" && exit 1 ;; \
    esac \
 && curl -fsSL "https://download.docker.com/linux/static/stable/${darch}/docker-27.3.1.tgz" -o /tmp/docker.tgz \
 && tar -xzf /tmp/docker.tgz -C /tmp \
 && mv /tmp/docker/docker /usr/local/bin/docker \
 && chmod +x /usr/local/bin/docker \
 && rm -rf /tmp/docker /tmp/docker.tgz \
 && apt-get purge -y curl \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
