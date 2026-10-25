#!/bin/sh
# Cutover helper: copy a consistent snapshot of the Railway gateway DB to
# ./gateway.db (run from the repo root, railway CLI linked to the project).
# Run it only while the Railway service is parked on `sleep infinity` — the
# gateway must have shut down (and persisted rotated Garmin tokens) first,
# see deploy/README.md → Cutover.
set -eu

snap() {
	railway ssh --service gateway "$1" < /dev/null
}

snap "python3 -c \"import sqlite3; s=sqlite3.connect('file:/data/gateway.db?mode=ro', uri=True); d=sqlite3.connect('/tmp/snap.db'); s.backup(d); d.close(); print('snapshot ok')\""
remote_sum=$(snap "sha256sum /tmp/snap.db" | tr -d '\r' | awk '{print $1}')

# base64 so the bytes survive the ssh pty; python strips any \r it adds.
snap "base64 -w0 /tmp/snap.db" | python3 -c "import base64, sys; sys.stdout.buffer.write(base64.b64decode(''.join(sys.stdin.read().split())))" > gateway.db

local_sum=$(shasum -a 256 gateway.db | awk '{print $1}')
[ "$remote_sum" = "$local_sum" ] || { echo "checksum mismatch: $remote_sum vs $local_sum" >&2; exit 1; }
sqlite3 gateway.db "pragma integrity_check"
ls -l gateway.db
