# Exact base image, bumped by Dependabot. Alpine rather than Debian slim: it
# has a fraction of the system packages (no login, PAM, perl, systemd or
# ncurses), so far less for a scanner to find and an attacker to use. The
# weekly rebuild (see .github/workflows/docker.yml) also applies Alpine
# security updates. setpriv drops root in docker-entrypoint.sh.
FROM python:3.12.15-alpine3.24

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NYERK_DATA_DIR=/data HOME=/tmp
RUN apk upgrade --no-cache && apk add --no-cache setpriv

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

# The commit this image was built from, shown in the web page footer. Last,
# so a new commit doesn't invalidate the cached layers above.
ARG NYERK_VERSION=dev
ENV NYERK_VERSION=$NYERK_VERSION

VOLUME /data
EXPOSE 8443
# Starts as root only to fix /data ownership, then runs as PUID:PGID.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "nyerk"]
