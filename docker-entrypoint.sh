#!/bin/sh
# Bind-mounted ./data is often created root-owned by Docker (Dockge, Portainer,
# plain compose). Fix ownership, then drop root before starting Nyerk.
# If the data folder can't be handed over (NFS/SMB share, SELinux, odd
# filesystem), say why and keep running as root rather than crash.
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

echo "nyerk-entrypoint: v3, container uid $(id -u), target $PUID:$PGID" >&2

if [ "$(id -u)" != "0" ]; then
    exec "$@"
fi

mkdir -p /data/logs /data/backups /data/tls
if ! chown -R "$PUID:$PGID" /data 2>/tmp/chown.err; then
    echo "nyerk-entrypoint: chown of /data to $PUID:$PGID failed: $(head -n1 /tmp/chown.err)" >&2
fi

if setpriv --reuid="$PUID" --regid="$PGID" --clear-groups \
        sh -c 'touch /data/.write-test /data/logs/.write-test && rm -f /data/.write-test /data/logs/.write-test' 2>/dev/null; then
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

echo "nyerk-entrypoint: /data is not writable as $PUID:$PGID ($(stat -c 'owner %u:%g mode %a' /data/logs))." >&2
echo "nyerk-entrypoint: running as root instead. See https://github.com/zle3/nyerk/wiki/Running-and-updating" >&2
exec "$@"
