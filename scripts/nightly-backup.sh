#!/bin/sh
# nightly-backup.sh — 02:30 cron on the dev machine. Pull a backup off the router.
#
# ## What this used to do, and why it was wrong
#
# It ran `sqlite3 /usr/local/share/rustical/db.sqlite3 ".backup …"` over ssh and
# tarred that plus /etc/rustical. That path is the **pre-tenancy** store. The
# deployed config has `[tenancy] enabled = true` with `data_root =
# /usr/local/share/rustical`, so the data the server actually serves lives at
# `tenants/<uuid>/db.sqlite3`, and `db.sqlite3` is the migration rollback copy.
#
# The two files were byte-identical on the day it was written, so nothing looked
# broken. That is the whole problem: the script would have kept archiving a
# database that nothing writes to, every night, indefinitely, and reported
# success. It is the shape of bug this plan has hit three times now — a second
# definition of a path that already has an owner.
#
# ## What it does now
#
# It asks the binary, which is the only thing that knows where the stores are.
# `rustical backup` resolves `db_url` properly (`SqliteDataStoreConfig::
# database_file`), `rustical backup-all` walks the control plane and writes one
# verified archive per tenant (`TenancyConfig::tenant_db_path`). Neither has a
# path hardcoded here, so neither can drift from what the server opens.
#
# Two other things come free and were worth having:
#
# - `backup-all` exits non-zero if **any** tenant failed, so a run that backed
#   up 3 of 200 tenants cannot look like a run that backed up 200. This script
#   propagates that: a failed tenant means no artifact at all, not a short one.
# - each archive carries a `manifest.json` with a SHA-256 per entry, so
#   `rustical restore` can verify before it replaces anything. The old tar had no
#   such check, and a tar that extracts into a corrupt database is the one
#   failure a backup exists to prevent.
#
# The config is still copied, because it holds the SMTP/IMAP passwords and the
# RSVP secret and a restore needs them. It lands in a 0700 directory.

set -eu

BACKUP_DIR="$HOME/backups/omnical"
LOG="$BACKUP_DIR/backup.log"
FLOOR_KB=5120

# Where the router puts the archives before they are streamed back. A fixed path
# rather than a mktemp so a half-finished run leaves something findable.
REMOTE_STAGE=/tmp/omnical-nightly

ROUTER=${ROUTER:-router}
RUSTICAL=${RUSTICAL:-/usr/sbin/rustical}
CONFIG_DIR=${CONFIG_DIR:-/etc/rustical}

umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
dest="$BACKUP_DIR/$(date +%F).tar.gz"
tmp="$dest.part"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

# The router side, as one shell. `set -eu` so a failure anywhere aborts before
# anything is streamed, and `backup-all`'s non-zero exit on a partial run is not
# swallowed. The stage is wiped first so a leftover from a failed run cannot be
# mistaken for this morning's.
#
# **No `ssh -n` here.** The script travels on stdin, and `-n` is exactly
# "redirect stdin from /dev/null" — it discards the script and then runs
# nothing, which fails silently into the empty-stream check below.
remote() {
    ssh "$ROUTER" "sh -s" <<EOF
set -eu
R="$RUSTICAL"
CFG="$CONFIG_DIR"
STAGE="$REMOTE_STAGE"

[ -x "\$R" ] || { echo "no rustical binary at \$R" >&2; exit 1; }

rm -rf "\$STAGE"
mkdir -p "\$STAGE/single" "\$STAGE/tenants"

# 1. The single-tenant store, whatever db_url says. On an N=1 self-hoster this
#    is the only database there is; on the appliance it is the rollback copy.
#    Both are worth keeping and neither is worth naming here.
#
#    **`>&2` on every invocation below.** `rustical backup` prints a human report
#    ("verified 12 tables, sha256 ok") to stdout, and stdout here *is* the tar
#    stream. Without this the archive begins with two lines of prose and every
#    tool that untars it stops at the first member, having quietly skipped the
#    data.
if [ -f "\$CFG/config.toml" ]; then
    "\$R" backup --out-dir "\$STAGE/single" --gzip >&2
else
    echo "no \$CFG/config.toml — skipping the single-tenant backup" >&2
fi

# 2. Every tenant, one verified archive each, in the tenant's own directory.
#    Exits non-zero if any tenant failed, which is the point of the command.
"\$R" backup-all --out-dir "\$STAGE/tenants" --gzip >&2

# 3. The config, because a restore needs the RSVP secret and the mail
#    credentials. 0700: it is the most sensitive thing in the archive.
mkdir -p "\$STAGE/config"
cp -a "\$CFG" "\$STAGE/config/rustical"
chmod -R go-rwx "\$STAGE/config"

# A file with no name in the set is not a backup; say what was taken and how big.
# **On stderr.** stdout carries the tar stream and nothing else — a summary line
# in front of it produces an archive that most tools will still untar, quietly,
# having ignored a corrupt first member.
find "\$STAGE" -name 'omnical-backup-*.tar.gz' | sort >&2
du -sh "\$STAGE" | cut -f1 >&2

tar -C "\$STAGE" -czf - .
EOF
}

# The router's own report of what it did, into the log. stderr, not stdout: see
# the note above. This is filenames and sizes only — nothing that is a secret
# goes through it.
remote 2>>"$LOG" > "$tmp" || {
    rm -f "$tmp"
    log "BACKUP FAILED (ssh/router error); no artifact written"
    exit 1
}

if [ -s "$tmp" ]; then
    mv -f "$tmp" "$dest"
    find "$BACKUP_DIR" -name '*.tar.gz' -mtime +30 -delete

    # Free space on the overlay, reported rather than acted on. This is the same
    # number PLAN.md §8.4 warns about; a full overlay takes the *next* backup with
    # it, so it is worth a line in a log somebody reads.
    dfs="$(ssh -n "$ROUTER" 'df -h / | tail -1; df -k / | tail -1' 2>/dev/null || true)"
    dfh_line="$(printf '%s\n' "$dfs" | sed -n 1p)"
    avail_kb="$(printf '%s\n' "$dfs" | sed -n 2p | awk '{print $4}')"
    if [ -n "$avail_kb" ] && [ "$avail_kb" -lt "$FLOOR_KB" ]; then
        log "$dfh_line  WARNING: overlay free below 5 MB floor (PLAN.md 8.4)"
    else
        log "$dfh_line"
    fi

    # What was actually taken, counted out of the file that now exists rather
    # than out of the router's log — so this line is true about the artifact, and
    # a stream that came back empty or truncated shows up as 0.
    n=$(tar -tzf "$dest" | grep -c 'omnical-backup-.*\.tar\.gz$' || true)
    log "ok  $dest  ($n verified archive(s))"
else
    rm -f "$tmp"
    log "BACKUP PRODUCED AN EMPTY STREAM; no artifact written"
    exit 1
fi