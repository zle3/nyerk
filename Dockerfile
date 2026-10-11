# Exact base image, bumped by Dependabot. The weekly rebuild (see
# .github/workflows/docker.yml) also applies Debian security updates.
FROM python:3.12.15-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NYERK_DATA_DIR=/data HOME=/tmp
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# pip is only needed to build the image; removing it also removes the copies
# of urllib3/msgpack it bundles, which security scanners flag.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip setuptools wheel 2>/dev/null; \
    rm -rf /usr/local/lib/python3.12/site-packages/pip* /usr/local/lib/python3.12/site-packages/setuptools* \
           /usr/local/lib/python3.12/ensurepip /usr/local/bin/pip*
COPY nyerk ./nyerk
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# Survive copies through Windows: strip CRLF and restore the exec bit.
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && chmod 755 /usr/local/bin/docker-entrypoint.sh

VOLUME /data
EXPOSE 8443
# Starts as root only to fix /data ownership, then runs as PUID:PGID.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "nyerk"]
