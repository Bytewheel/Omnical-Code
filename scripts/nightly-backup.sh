#!/bin/sh
set -eu

BACKUP_DIR="$HOME/backups/omnical"
LOG="$BACKUP_DIR/backup.log"
FLOOR_KB=5120
REMOTE='sqlite3 /usr/local/share/rustical/db.sqlite3 "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null && sqlite3 /usr/local/share/rustical/db.sqlite3 ".backup /tmp/omnical-bu.db" && tar -C /tmp -czf - omnical-bu.db /etc/rustical && rm -f /tmp/omnical-bu.db'

umask 077
mkdir -p "$BACKUP_DIR"
dest="$BACKUP_DIR/$(date +%F).tar.gz"
tmp="$dest.part"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

if ssh -n router "$REMOTE" > "$tmp" 2>/dev/null; then
    mv -f "$tmp" "$dest"
    find "$BACKUP_DIR" -name '*.tar.gz' -mtime +30 -delete
    dfs="$(ssh -n router 'df -h / | tail -1; df -k / | tail -1' 2>/dev/null || true)"
    dfh_line="$(printf '%s\n' "$dfs" | sed -n 1p)"
    avail_kb="$(printf '%s\n' "$dfs" | sed -n 2p | awk '{print $4}')"
    if [ -n "$avail_kb" ] && [ "$avail_kb" -lt "$FLOOR_KB" ]; then
        log "$dfh_line  WARNING: overlay free below 5 MB floor (PLAN.md 8.4)"
    else
        log "$dfh_line"
    fi
else
    rm -f "$tmp"
    log "BACKUP FAILED (ssh/router error); no artifact written"
    exit 1
fi
