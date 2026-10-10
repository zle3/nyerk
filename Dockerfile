FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NYERK_DATA_DIR=/data HOME=/tmp
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY nyerk ./nyerk
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# Survive copies through Windows: strip CRLF and restore the exec bit.
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && chmod 755 /usr/local/bin/docker-entrypoint.sh

VOLUME /data
EXPOSE 8443
# Starts as root only to fix /data ownership, then runs as PUID:PGID.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "nyerk"]
