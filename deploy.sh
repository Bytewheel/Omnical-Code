#!/usr/bin/env bash
# deploy.sh — Push the cross-built Omnical stack (rustical + dav-tls) to the
# libreCMC router and wire up services/persistence.  Modeled on router-nym's
# deploy.sh.  Idempotent — safe to re-run (also after every sysupgrade).
#
# History / constraints that shaped this script:
#   - Phase 2: initial deploy (virgin router, 42 MB free) — plain scp worked.
#   - Phase 3: dav-tls enabled+running (192.168.1.21:8443 -> 127.0.0.1:4000).
#   - §17.2 item 5 (2026-09-05): config render enables the scheduling
#     extension with the 7 SMTP identities from pass.  By then the f2fs overlay
#     had ~9 MB free vs 28 MB binaries: scp'ing a second copy next to the
#     running binary fails (ENOSPC, "write remote: Failure"), and a RUNNING
#     binary's blocks are only reclaimed when its process stops.  Hence:
#     stage binaries in /tmp (tmpfs, RAM) -> tight stop/flash-copy/start swap.
#   - Order-critical: the stock binary's Config is serde deny_unknown_fields,
#     so the new [scheduling] config must never meet the old binary — the
#     binary is swapped while stopped, the config lands before the start.
#   - Phase 8.3 (2026-09-06): post-sysupgrade hardening, verified against the
#     REAL preservation list (`sysupgrade -b` ground truth): custom init.d
#     scripts and /usr binaries are wiped (stop must tolerate a missing
#     init script), while /etc/crontabs/root IS preserved via
#     /lib/upgrade/keep.d/busybox — the cron block is belt-and-suspenders
#     and touches /etc/crontabs when it appends (busybox crond rescans
#     only on directory-mtime change, Phase 8.2).
#
# Requires:
#   - out/rustical + out/dav-tls built by scripts/build-rust.sh
#   - pass entries for the 7 SMTP identities (render script aborts otherwise)
#   - SSH alias `router` (root@192.168.10.1, key ~/.ssh/router)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="$ROOT/out"
ROUTER="${ROUTER:-router}"
BINS=(rustical dav-tls)

say() { printf '\033[1m%s\033[0m\n' "$*"; }

say "==> Checking binaries"
for b in "${BINS[@]}"; do
	[ -f "$OUT/$b" ] || { echo "!! missing $OUT/$b — run scripts/build-rust.sh first" >&2; exit 1; }
	file "$OUT/$b" | grep -q "aarch64" || {
		echo "!! $OUT/$b is not an aarch64 binary:" >&2
		file "$OUT/$b" >&2
		exit 1
	}
	echo "   OK $b ($(du -h "$OUT/$b" | cut -f1))"
done

say "==> Rendering config (fail-fast: pass must resolve before any service stop)"
# Rendered into a shell variable — SMTP passwords go straight from pass into
# the router's 0600 /etc/rustical/config.toml, never to disk on this machine.
RENDERED_CONFIG="$("$ROOT/scripts/render-router-config.sh")"

say "==> Staging binaries on $ROUTER:/tmp (tmpfs — zero overlay cost)"
for b in "${BINS[@]}"; do
	scp -q "$OUT/$b" "$ROUTER:/tmp/$b.new"
done

# Deploy fence: rustical-watchdog no-ops while this lock exists, so its
# 5-min health check can neither exec the binary mid-swap (a health-check
# exec once held the old inode through the rm → freed nothing → cp died
# with ENOSPC, incident 2026-09-08 17:20) nor restart half-deployed
# services.  /tmp is tmpfs: a hard-crashed deploy leaves the fence until
# reboot or the next deploy — exactly when the watchdog should stay quiet.
ssh "$ROUTER" 'rm -f /tmp/rustical-deploy.lock && touch /tmp/rustical-deploy.lock'

DAVTLS_OLD_SHA="$(ssh "$ROUTER" 'sha256sum /usr/sbin/dav-tls 2>/dev/null | cut -d" " -f1')"
DAVTLS_NEW_SHA="$(sha256sum "$OUT/dav-tls" | cut -d' ' -f1)"

say "==> Brief rustical stop + binary swap (running binary's blocks free only on stop)"
ssh "$ROUTER" '
	set -e
	rm -f /usr/sbin/rustical.new                        # stale partial from a failed scp
	# Post-sysupgrade the init script is wiped (NOT preserved — only
	# /etc/rustical + the DB are, via /etc/sysupgrade.conf): nothing to
	# stop then; a running service is stopped for the flash-copy swap.
	[ -f /etc/init.d/rustical ] && /etc/init.d/rustical stop || true
	rm -f /usr/sbin/rustical
	# ENOSPC guard (incident 2026-09-08): the rm above only freed space if
	# nothing holds the old inode (stopped service + fenced watchdog ⇒
	# nothing does).  Assert before the cp so a failure aborts with the
	# staging file intact in /tmp instead of leaving a truncated
	# /usr/sbin/rustical and a dead service.
	# NB: busybox here has no `stat` — sizes via `wc -c`.
	new_size="$(wc -c < /tmp/rustical.new | tr -d " ")"
	need_kib=$(( (new_size + 1023) / 1024 + 2048 ))
	avail_kib=$(df -k / | awk "NR==2 {print \$4}")
	if [ "$avail_kib" -lt "$need_kib" ]; then
		logger -t deploy "ENOSPC guard: avail=${avail_kib}KiB need=${need_kib}KiB — swap aborted"
		echo "!! overlay too tight after rm (avail=${avail_kib}KiB need=${need_kib}KiB)" >&2
		echo "   staged binary kept at /tmp/rustical.new — free space and retry" >&2
		exit 1
	fi
	cp /tmp/rustical.new /usr/sbin/rustical
	chmod 755 /usr/sbin/rustical
	[ "$(wc -c < /usr/sbin/rustical | tr -d " ")" = "$new_size" ] || {
		echo "!! rustical copy incomplete (size mismatch)" >&2
		exit 1
	}
	rm -f /tmp/rustical.new
	# dav-tls: file swapped in place; the RUNNING process keeps serving from
	# the old inode — it is restarted after only if the content changed.
	rm -f /usr/sbin/dav-tls
	cp /tmp/dav-tls.new /usr/sbin/dav-tls
	chmod 755 /usr/sbin/dav-tls
	rm -f /tmp/dav-tls.new
'

say "==> Pushing config + init scripts"
# create target dirs first — scp cannot create /etc/rustical on a virgin router
ssh "$ROUTER" 'mkdir -p /etc/rustical/tls /etc/rustical/certs /usr/local/share/rustical'
printf '%s\n' "$RENDERED_CONFIG" | ssh "$ROUTER" 'cat > /etc/rustical/config.toml'
# extra TLS trust anchors for incomplete-chain IMAP providers (referenced by
# the ca_file key the render script emits; /etc/rustical is preserved via
# sysupgrade.conf, so this survives sysupgrades too)
scp -q "$ROOT/router/etc/rustical/certs/imap-novo-ordo.pem" "$ROUTER:/etc/rustical/certs/imap-novo-ordo.pem"
scp -q "$ROOT/router/etc/init.d/rustical" "$ROUTER:/etc/init.d/rustical"
scp -q "$ROOT/router/etc/init.d/dav-tls" "$ROUTER:/etc/init.d/dav-tls"
scp -q "$ROOT/router/usr/bin/rustical-watchdog" "$ROUTER:/usr/bin/rustical-watchdog"

say "==> Router-side preparation (idempotent)"
ssh "$ROUTER" '
	set -e
	chmod 755 /usr/sbin/rustical /usr/sbin/dav-tls /etc/init.d/rustical /etc/init.d/dav-tls
	chmod 755 /usr/bin/rustical-watchdog
	chmod 600 /etc/rustical/config.toml
	mkdir -p /usr/local/share/rustical /etc/rustical/tls /etc/rustical/certs

	# persistence across sysupgrade
	while IFS= read -r line; do
		grep -qxF "$line" /etc/sysupgrade.conf || echo "$line" >> /etc/sysupgrade.conf
	done <<EOF
/etc/rustical
/usr/local/share/rustical
EOF

	# 8.2 monitoring: 5-min health watchdog cron (screech-watchdog
	# pattern).  busybox crond does NOT rescan on file appends — it
	# rescans only when the /etc/crontabs DIRECTORY mtime changes, so
	# touch it whenever a line was appended (Phase 8.2, verified on this
	# router).  NB: /etc/crontabs/root IS preserved across sysupgrade
	# (/lib/upgrade/keep.d/busybox lists /etc/crontabs/) — this block is
	# belt-and-suspenders for a restored/older crontab or a firmware that
	# drops the keep.d entry.
	grep -qxF "*/5 * * * * /usr/bin/rustical-watchdog" /etc/crontabs/root \
		|| { echo "*/5 * * * * /usr/bin/rustical-watchdog" >> /etc/crontabs/root
		     touch /etc/crontabs; }

	# online-backup tool for the SQLite DB
	if ! command -v sqlite3 >/dev/null 2>&1; then
		opkg update && opkg install sqlite3-cli
	fi

	/etc/init.d/rustical enable
	/etc/init.d/dav-tls enable
	echo "prep done"
'

say "==> Starting rustical (HTTP on 127.0.0.1:4000 only)"
if [ "$DAVTLS_NEW_SHA" = "$DAVTLS_OLD_SHA" ]; then
	echo "   dav-tls binary unchanged — no dav-tls restart (running process keeps serving)"
else
	echo "   dav-tls binary changed — restarting dav-tls"
	ssh "$ROUTER" '/etc/init.d/dav-tls restart'
fi
ssh "$ROUTER" '
	/etc/init.d/rustical start
	sleep 2
	# NB: --config-file precedes the subcommand (top-level clap option)
	/usr/sbin/rustical --config-file /etc/rustical/config.toml health && echo "rustical healthy" || echo "!! rustical health check failed — check: logread -e rustical"
	netstat -tlnp 2>/dev/null | grep :4000 || true
	logread -e rustical | grep -i "scheduling" | tail -2 || true
'

say "==> Releasing deploy fence (watchdog re-enabled)"
ssh "$ROUTER" 'rm -f /tmp/rustical-deploy.lock'

say "==> Done. Service states:"
ssh "$ROUTER" '/etc/init.d/rustical status; /etc/init.d/dav-tls status; df -k / | tail -1'
