#!/bin/sh
# Rollback helper: upload ./gateway.db onto the parked Railway volume as
# /data/gateway.db (run from the repo root). Only while the Railway service runs
# scripts/railway_park.py — never under a live gateway. See deploy/README.md →
# Rollback.
set -eu

[ -f gateway.db ] || { echo "no ./gateway.db" >&2; exit 1; }
sqlite3 gateway.db "pragma integrity_check" | grep -qx ok || { echo "local DB fails integrity_check" >&2; exit 1; }
local_sum=$(shasum -a 256 gateway.db | awk '{print $1}')

remote() {
	railway ssh --service gateway "$1"
}

# Parked? The gateway process must be gone, or it would overwrite what we upload.
remote "for p in /proc/[0-9]*; do tr '\\0' ' ' < \$p/cmdline; echo; done" < /dev/null | tr -d '\r' | grep -q 'railway_park.py' \
	|| { echo "Railway is not parked on railway_park.py" >&2; exit 1; }
if remote "for p in /proc/[0-9]*; do tr '\\0' ' ' < \$p/cmdline; echo; done" < /dev/null | tr -d '\r' | grep -q 'missingmcp'; then
	echo "a gateway process is still running on Railway" >&2; exit 1
fi

base64 < gateway.db | tr -d '\n' | remote "base64 -d > /data/gateway.db.upload"
remote_sum=$(remote "sha256sum /data/gateway.db.upload" < /dev/null | tr -d '\r' | awk '{print $1}')
[ "$remote_sum" = "$local_sum" ] || {
	echo "checksum mismatch ($remote_sum vs $local_sum) — Railway DB left untouched" >&2
	remote "rm -f /data/gateway.db.upload" < /dev/null
	exit 1
}
# Stale WAL/SHM from the pre-cutover DB must not be replayed onto the new file.
remote "rm -f /data/gateway.db-wal /data/gateway.db-shm && mv /data/gateway.db.upload /data/gateway.db && ls -l /data/gateway.db" < /dev/null
