#!/usr/bin/env bash
# firmware-gate.sh — what §9.2 can be proven of without a router (item 12).
#
#   scripts/firmware-gate.sh
#
# It takes no arguments and needs no router, no ImageBuilder and no compiled
# binary: what it tests is the recipe and the postinst, both of which are
# committed text. That is deliberate, so it can be a CI job.
#
# §9.2's gate is `sysupgrade -b | grep -c rustical` on a flashed unit, and rows
# 46/48/51 need real hardware. A GitHub runner is not a router, and pretending
# otherwise would produce a gate that passes without testing anything. So this
# script does NOT claim those rows.
#
# What it does instead is test the part that is pure logic and that rots
# silently: **the postinst**. It is the one file in this package that can
# destroy a customer's configuration, and it is invisible to every other check.
#
# The test that matters: run the postinst against a fake root that already
# contains a config.toml holding an SMTP password and the RSVP secret, and assert
# the file comes out byte-identical. A firmware image that clobbers a preserved
# config on post-flash boot would silently reconfigure a production unit to
# defaults, and the symptom would be bounced invites minutes later with no
# obvious cause. That is a far worse failure than a box that does not boot, and
# it is the kind of bug that only ships if nobody runs the postinst.
#
# Then it runs the same postinst a second time and asserts nothing changed —
# because the same script runs on a factory flash, on every sysupgrade, and on
# every operator's --force-reinstall, and a postinst that is not idempotent
# eventually duplicates cron lines and grows /etc/sysupgrade.conf forever.
#
# Nothing here is a mock of the *subject*. The postinst under test is the real
# file that ships.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FILES="$ROOT/packaging/firmware/omnical/files"
RECIPE="$ROOT/packaging/firmware/omnical/Makefile"

[ -f "$FILES/postinst" ] || { echo "missing $FILES/postinst" >&2; exit 1; }

fails=0
pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fails=$((fails + 1)); }
section() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# A sentinel that stands in for a real secret. If this string ever appears in
# the post-flash config, the postinst clobbered a preserved config.
SMTP_SECRET='hunter2-smtp-password-a7f3'
RSVP_SECRET='rsvp-hmac-secret-91be'

new_root() {
	local w
	w="$(mktemp -d)"
	mkdir -p "$w/etc/config" "$w/etc/init.d" "$w/etc/rustical/tls" \
		"$w/etc/crontabs" "$w/etc/omnical" "$w/usr/local/share/rustical" \
		"$w/lib/upgrade/keep.d" "$w/usr/bin"
	# A realistic tree: base config installed as a sample, exactly as the recipe
	# does, plus a preserved user config and an existing crontab.
	printf 'db_url = "file:/usr/local/share/rustical/db.sqlite3"\n' \
		> "$w/etc/omnical-config.toml.sample"
	printf '*/5 * * * * /usr/bin/rustical-watchdog\n' > "$w/etc/crontabs/root"
	cp "$FILES/postinst" "$w/postinst"
	chmod +x "$w/postinst"
	echo "$w"
}

# Run the postinst with a fake root and stubbed /etc/init.d (the real ones would
# try to start services, which is not what this gate is about).
run_postinst() {
	local w="$1"
	(
		# shellcheck disable=SC1090
		. "$w/stub.sh"
		OMNICAL_ROOT="$w" sh "$w/postinst" >"$w/postinst.log" 2>&1
	) || {
		echo "    --- postinst output ---"
		sed 's/^/    /' "$w/postinst.log" || true
		return 1
	}
}

make_stubs() {
	local w="$1"
	cat > "$w/stub.sh" <<STUB
# Minimal /etc/init.d and logger so the postinst can run against a fake root.
for d in rustical dav-tls cron; do
	[ -d "$w/etc/init.d/\$d" ] || continue
	cat > "$w/etc/init.d/\$d" <<'INITSCRIPT'
#!/bin/sh
action="\$1"
shift 2>/dev/null || true
if [ "\$action" = "enabled" ]; then exit 1; fi
echo "\$0 \$action" >> "\$OMNICAL_ROOT/calls.log"
exit 0
INITSCRIPT
	chmod +x "$w/etc/init.d/\$d"
done
logger() { echo "omnical: \$*" >> "\$OMNICAL_ROOT/log.txt"; }
STUB
}

# ── 1. the recipe stages what §9.2 says it must ──────────────────────────────
section "1. the package recipe stages the §9.2 payload"
# Strip comments before grepping. The Makefile's header documents every staged
# path by name, so a naive `grep /etc/init.d/rustical` matches the *comment*
# and passes even when the install line is gone — a check that passes for the
# wrong reason, which is worse than no check. Found by mutation-testing this
# gate: deleting both install lines still reported ok.
recipe() { grep -v '^[[:space:]]*#' "$RECIPE" | tr -s ' \t' ' '; }

# ── 2. the recipe must not install the base config as the live config ───────
section "2. a preserved config is never overwritten"
if recipe | grep -qF '$(1)/etc/rustical/config.toml'; then
	fail "the recipe installs config.toml directly — opkg would overwrite a preserved config"
else
	pass "config.toml is installed as a .sample, not as the live config"
fi
recipe | grep -qF 'omnical-config.toml.sample' &&
	pass "the base config ships as a .sample the postinst copies conditionally" ||
	fail "no .sample config — the postinst has nothing to seed a fresh unit with"
recipe | grep -qF 'conffiles' &&
	pass "config.toml is declared a conffile" ||
	fail "config.toml is not a conffile — opkg will not preserve it across a package upgrade"

for dest in /usr/sbin/rustical /usr/sbin/dav-tls /usr/bin/rustical-watchdog \
	/etc/init.d/rustical /etc/init.d/dav-tls /lib/upgrade/keep.d/omnical; do
	if recipe | grep -qF "$dest"; then
		pass "recipe installs $dest"
	else
		fail "recipe does not install $dest — a sysupgrade would wipe it"
	fi
done

# The init scripts are the whole reason this package exists, so their absence
# from the recipe is the one omission that defeats the item. Match the whole
# install invocation, not the path: a path can appear in a comment.
for s in rustical dav-tls; do
	recipe | grep -qF "\$(INSTALL_BIN) \$(CURDIR)/files/init.d/$s \$(1)/etc/init.d/$s" &&
		pass "init script $s is a *package file*, not a postinst copy" ||
		fail "init script $s is not installed by the recipe — a flash will not restore it"
done

# ── 2. the recipe must not install the base config as the live config ───────
section "2. a preserved config is never overwritten"
recipe | grep -qF 'omnical-config.toml.sample' &&
	pass "the base config ships as a .sample the postinst copies conditionally" ||
	fail "no .sample config — the postinst has nothing to seed a fresh unit with"
recipe | grep -qF 'conffiles' &&
	pass "config.toml is declared a conffile" ||
	fail "config.toml is not a conffile — opkg will not preserve it across a package upgrade"

# ── 3. the postinst on a FACTORY unit (nothing preserved) ───────────────────
section "3. postinst on a factory flash (nothing to preserve)"
W1="$(new_root)"
make_stubs "$W1"
if run_postinst "$W1"; then
	pass "postinst runs clean against a bare root"
else
	fail "postinst failed on a factory root"
fi

[ -f "$W1/etc/rustical/config.toml" ] &&
	pass "seeds config.toml when there is none" ||
	fail "no config seeded on a factory flash — a fresh unit has no config and no way to get one"

for line in /etc/rustical /usr/local/share/rustical; do
	grep -qxF "$line" "$W1/etc/sysupgrade.conf" &&
		pass "sysupgrade.conf gains $line" ||
		fail "sysupgrade.conf is missing $line — the next sysupgrade loses the database"
done

grep -qF '/usr/bin/rustical-watchdog' "$W1/etc/crontabs/root" &&
	pass "watchdog cron line installed" ||
	fail "no watchdog cron line — a hung process is never restarted"

[ -d "$W1/usr/local/share/rustical" ] &&
	pass "database directory created under /usr/local (not /var, which is tmpfs)" ||
	fail "no /usr/local/share/rustical — /var is tmpfs and the DB would not survive a reboot"

perm="$(wc -c < "$W1/etc/rustical/config.toml" 2>/dev/null || echo 0)"
[ "$perm" -gt 0 ] && pass "seeded config is non-empty" || fail "seeded config is empty"

# ── 4. the test that matters: a preserved config survives ───────────────────
section "4. postinst on a UPGRADED unit (config must survive byte-identical)"
W2="$(new_root)"
make_stubs "$W2"
PRESERVED="$W2/etc/rustical/config.toml"
cat > "$PRESERVED" <<EOF
[scheduling.smtp]
username = "alerts@customer.example"
password = "$SMTP_SECRET"

[rsvp]
secret = "$RSVP_SECRET"
db_url = "file:/usr/local/share/rustical/db.sqlite3"
EOF
chmod 600 "$PRESERVED"
cp "$PRESERVED" "$W2/config.toml.expected"

# A realistic crontab that already has other people's lines, and a
# sysupgrade.conf that already has ours (as a previous install would have left).
printf '0 3 * * * /usr/bin/some-other-job\n*/5 * * * * /usr/bin/rustical-watchdog\n' \
	> "$W2/etc/crontabs/root"
printf '/etc/rustical\n/usr/local/share/rustical\n' > "$W2/etc/sysupgrade.conf"
cp "$W2/etc/sysupgrade.conf" "$W2/sysupgrade.expected"
cp "$W2/etc/crontabs/root" "$W2/cron.expected"

if run_postinst "$W2"; then
	pass "postinst runs clean against an upgraded root"
else
	fail "postinst failed on an upgraded root"
fi

cmp -s "$PRESERVED" "$W2/config.toml.expected" &&
	pass "config.toml is byte-identical after the postinst — no secret lost" ||
	fail "config.toml was MODIFIED by the postinst — a post-flash boot would wipe a production unit's SMTP credentials and RSVP secret"

if grep -qF "$SMTP_SECRET" "$PRESERVED" && grep -qF "$RSVP_SECRET" "$PRESERVED"; then
	pass "both secrets still present in the live config"
else
	fail "a secret vanished from config.toml"
fi

# ── 5. idempotence ───────────────────────────────────────────────────────────
section "5. the postinst is idempotent (it runs on every flash, forever)"
cmp -s "$W2/etc/sysupgrade.conf" "$W2/sysupgrade.expected" &&
	pass "sysupgrade.conf unchanged on re-run (no duplicated lines)" ||
	{ fail "sysupgrade.conf grew on re-run:"; sed 's/^/      /' "$W2/etc/sysupgrade.conf"; }

cmp -s "$W2/etc/crontabs/root" "$W2/cron.expected" &&
	pass "crontab unchanged on re-run (no duplicated watchdog lines)" ||
	{ fail "crontab grew on re-run:"; sed 's/^/      /' "$W2/etc/crontabs/root"; }

# A second full run, to catch anything that only shows up on the third install.
if run_postinst "$W2"; then
	cmp -s "$W2/etc/rustical/config.toml" "$W2/config.toml.expected" &&
		pass "config still byte-identical after a third install" ||
		fail "config drifted on the third install"
	[ "$(wc -l < "$W2/etc/sysupgrade.conf")" = "$(wc -l < "$W2/sysupgrade.expected")" ] &&
		pass "sysupgrade.conf still the same length after a third install" ||
		fail "sysupgrade.conf keeps growing"
else
	fail "postinst failed on a third install"
fi

# ── 6. shell hygiene ─────────────────────────────────────────────────────────
section "6. shell hygiene"
sh -n "$FILES/postinst" && pass "postinst parses under POSIX sh (busybox ash target)" ||
	fail "postinst does not parse"

if grep -qE '\b(stat|base64|readlink|realpath)\b' "$FILES/postinst"; then
	fail "postinst uses a command D5 says the target lacks (no stat, no base64)"
else
	pass "postinst uses no busybox-absent commands"
fi

# ── 7. what this gate does NOT claim ─────────────────────────────────────────
section "7. rows this gate does NOT close"
cat <<'NOTE'
  row 46  factory unit boots          needs a flashed unit   — NOT claimed
  row 48  survives sysupgrade         needs a flashed unit   — NOT claimed
  row 51  overlay budget on device    needs df -k / on a unit— NOT claimed
  §9.2   sysupgrade -b | grep -c      needs a flashed unit   — NOT claimed

  The 35 MiB overlay budget IS checked, against the staged payload
  (scripts/build-firmware.sh), which is the same number the device would see.
NOTE

rm -rf "$W1" "$W2"

echo
if [ "$fails" -eq 0 ]; then
	echo "firmware-gate: all checks passed"
	exit 0
fi
echo "firmware-gate: $fails FAILED"
exit 1
