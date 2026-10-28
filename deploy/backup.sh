#!/bin/bash
# Hourly SQLite snapshot of the gateway DB onto hydra's backup volume
# (Hetzner Volume at /mnt/backup — NOT covered by Hetzner Backups, which snapshot
# the VM's own disk only). Installed as /usr/local/bin/missingmcp-backup, run by
# missingmcp-backup.timer; see deploy/README.md → Backups.
#
# Layout under $DEST: hourly/, daily/, monthly/ — each file a zstd-compressed,
# integrity-checked copy. daily/ and monthly/ entries are hard links to the
# first hourly snapshot of their day/month, so they cost no extra space until
# the hourly copy is pruned.
set -euo pipefail
cd /

DB=${DB:-/opt/missingmcp/data/gateway.db}
MOUNT=${MOUNT:-/mnt/backup}
DEST=${DEST:-$MOUNT/missingmcp}
KEEP_HOURLY=${KEEP_HOURLY:-48}
KEEP_DAILY=${KEEP_DAILY:-30}
KEEP_MONTHLY=${KEEP_MONTHLY:-12}

die() { echo "missingmcp-backup: $*" >&2; exit 1; }

# fstab mounts the volume with `nofail`: if it didn't attach, $MOUNT is a plain
# directory on the root disk and we'd silently fill that instead.
mountpoint -q "$MOUNT" || die "$MOUNT is not a mountpoint (volume not attached?)"
[ -f "$DB" ] || die "no database at $DB"

install -d -m 700 "$DEST" "$DEST/hourly" "$DEST/daily" "$DEST/monthly"
exec 9>"$DEST/.lock"
flock -n 9 || die "another backup is still running"

# Room for the raw snapshot plus its compressed copy.
need_kb=$(( $(du -k "$DB" | cut -f1) * 2 ))
free_kb=$(df -Pk "$DEST" | awk 'NR==2 {print $4}')
[ "$free_kb" -gt "$need_kb" ] || die "not enough space on $MOUNT: ${free_kb} KB free, need ${need_kb} KB"

now=$(date -u +%Y%m%dT%H%MZ)
tmp="$DEST/.tmp-$now.db"
trap 'rm -f "$tmp" "$tmp.zst"' EXIT

# The online backup API: a consistent snapshot under a read transaction, which
# in WAL mode never blocks the gateway's writers.
sqlite3 "$DB" ".timeout 10000" ".backup '$tmp'"
check=$(sqlite3 "$tmp" "PRAGMA integrity_check")
[ "$check" = "ok" ] || die "integrity_check on the snapshot failed: $check"

zstd -q -10 "$tmp" -o "$tmp.zst"
chmod 600 "$tmp.zst"
snap="$DEST/hourly/gateway-$now.db.zst"
mv "$tmp.zst" "$snap"

day="$DEST/daily/gateway-${now:0:8}.db.zst"
month="$DEST/monthly/gateway-${now:0:6}.db.zst"
[ -e "$day" ] || ln "$snap" "$day"
[ -e "$month" ] || ln "$snap" "$month"

# Names sort chronologically; keep the newest N of each tier. Only after a new
# snapshot landed, so a failing run never thins out the old ones.
prune() { ls -1 "$1"/gateway-*.db.zst 2>/dev/null | sort -r | tail -n +$(( $2 + 1 )) | xargs -r rm -f; }
prune "$DEST/hourly" "$KEEP_HOURLY"
prune "$DEST/daily" "$KEEP_DAILY"
prune "$DEST/monthly" "$KEEP_MONTHLY"

echo "missingmcp-backup: $snap ($(du -h "$snap" | cut -f1)), $(df -h "$MOUNT" | awk 'NR==2 {print $4}') free on $MOUNT"
