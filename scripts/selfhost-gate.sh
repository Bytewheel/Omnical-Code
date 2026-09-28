#!/usr/bin/env bash
# selfhost-gate.sh — the gate for the two self-hosting channels
# (PLAN_DEPLOYMENTS.md §8.1, verification-matrix rows 40 and 41).
#
#   scripts/selfhost-gate.sh <path-to-rustical-binary>
#
# Row 41 is "tarball + install.sh on a bare VM, same end state as row 40". A
# GitHub runner *is* a bare VM, so this script is that gate: it installs with
# `packaging/native/install.sh`, boots the server on the config the wizard
# produced, and drives a real client round trip and a real registration.
#
# Row 40 is the Compose path. **Running a container runtime is a stretch goal,
# not a gate** (PLAN_DEPLOYMENTS.md §18.7, user decision 2026-09-28): the
# self-host channel has to stand on its own, and `packaging/native/install.sh`
# reaches the identical end state with no container in the picture.
#
# So this script does not need Docker, and deliberately does not pretend to
# check it. What it checks instead is the part of the Compose channel that is
# ours and that actually rots without a runtime — that `compose.omnical.yml`
# and the wizard still agree on every answer, on where the database lives, and
# on the order the two services start in — plus the whole substance of the row,
# run against the release binary with the same answers the container is given.
#
# Nothing here is a mock. The binary under test is the release build, the
# database is a real SQLite file, and the requests are real HTTP.

set -euo pipefail

BIN="${1:?usage: selfhost-gate.sh <path-to-rustical-binary>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALLER="$ROOT/packaging/native/install.sh"
COMPOSE="$ROOT/compose.omnical.yml"

[ -x "$BIN" ] || { echo "not executable: $BIN" >&2; exit 1; }
[ -f "$INSTALLER" ] || { echo "missing $INSTALLER" >&2; exit 1; }
[ -f "$COMPOSE" ]  || { echo "missing $COMPOSE" >&2; exit 1; }

WORK="$(mktemp -d)"
PREFIX="$WORK/usr"
CONF="$WORK/etc/omnical/config.toml"
DATA="$WORK/var/lib/omnical"
PORT="${OMNICAL_GATE_PORT:-14000}"
PID=""
fails=0

cleanup() {
	[ -n "$PID" ] && kill "$PID" 2>/dev/null
	wait "$PID" 2>/dev/null
	rm -rf "$WORK"
}
trap cleanup EXIT

step()  { printf '\n\033[1m=== %s\033[0m\n' "$*"; }
ok()    { printf '\033[32m  ok\033[0m    %s\n' "$*"; }
bad()   { printf '\033[31m  FAIL\033[0m  %s\n' "$*"; fails=$((fails + 1)); }
note()  { printf '        %s\n' "$*"; }

# `grep -q` in a pipeline under `set -o pipefail` is a footgun (SIGPIPE kills grep's
# upstream and the pipeline reports failure), hence the command substitution.
expect_eq() { # <label> <actual> <expected>
	if [ "$2" = "$3" ]; then ok "$1 = $3"; else bad "$1: got '$2', want '$3'"; fi
}
expect_contains() { # <label> <haystack-file> <needle>
	if grep -qF -- "$3" "$2"; then ok "$1"; else bad "$1 (no '$3' in $2)"; fi
}
expect_absent() { # <label> <haystack-file> <needle>
	if [ -z "$3" ]; then
		note "$label: skipped, nothing to look for"
	elif grep -qF -- "$3" "$2"; then
		bad "$1 (found '$3' in $2)"
	else
		ok "$1"
	fi
}

# ── 1. The two channels speak the same wizard ────────────────────────────────
# Every OMNICAL_SETUP_* the Compose file passes must be a flag `rustical setup`
# actually accepts, and the data directory it passes must be the one the server
# is told to open. Both are silent-typo classes: a misspelled variable does
# nothing, and a mismatched db_url gives the server an empty database.
step "The Compose file and the wizard agree"

help_vars="$("$BIN" setup --help 2>&1 | grep -oE 'OMNICAL_SETUP_[A-Z_]+' | sort -u || true)"
compose_vars="$(grep -oE 'OMNICAL_SETUP_[A-Z_]+' "$COMPOSE" | sort -u)"

if [ -z "$help_vars" ]; then
	bad "'$BIN setup --help' mentions no OMNICAL_SETUP_* variables — did the unattended \
mode get dropped?"
else
	note "the binary accepts: $(echo "$help_vars" | tr '\n' ' ')"
fi

while read -r var; do
	[ -n "$var" ] || continue
	case "$var" in
		# Present in this file as documentation, not as an answer for setup.
		OMNICAL_SETUP_UNATTENDED) continue ;;
		# Deliberately hidden from --help (see SetupArgs::admin_password), so it
		# is checked the other way round, below.
		OMNICAL_SETUP_ADMIN_PASSWORD) continue ;;
	esac
	if echo "$help_vars" | grep -qxF "$var"; then
		ok "$var is accepted by the wizard"
	else
		bad "$var is in compose.omnical.yml but the wizard has no such flag"
	fi
done <<< "$compose_vars"

# The reverse direction: a wizard flag nobody uses is a feature the self-hoster
# cannot reach from the file they were told to copy.
while read -r var; do
	[ -n "$var" ] || continue
	[ "$var" = OMNICAL_SETUP_UNATTENDED ] && continue
	# The password is env-only by design (a flag would be visible in `ps`), so
	# it is the one variable legitimately absent from --help. Assert the reason
	# instead of the absence: the flag must not be accepted.
	if [ "$var" = OMNICAL_SETUP_ADMIN_PASSWORD ]; then
		if echo "$compose_vars" | grep -qxF "$var"; then
			ok "$var is documented in compose.omnical.yml"
		else
			bad "$var is accepted by the wizard but is not in compose.omnical.yml"
		fi
		continue
	fi
	if echo "$compose_vars" | grep -qxF "$var"; then
		ok "$var is documented in compose.omnical.yml"
	else
		bad "$var exists on the wizard but is not in compose.omnical.yml — an \
operator reading the compose file cannot discover it"
	fi
done <<< "$help_vars"

# The administrator's password must not be passable as an argument. A password
# in argv is in `ps` output for every user on the host, so a self-hoster who
# follows the flag-shaped help would leak it. Enforced, not just documented.
if "$BIN" setup --admin-password 'a-long-enough-password' >/dev/null 2>&1; then
	bad "--admin-password is accepted: an administrator password would land in \`ps\`"
else
	ok "--admin-password is rejected (the password is environment-only, by design)"
fi

# The database path, once per place it is written, has to be the same file.
data_dir_anchor="$(sed -n 's/^x-omnical-data-dir: &data-dir //p' "$COMPOSE")"
db_url_anchor="$(sed -n 's/^x-omnical-db-url: &db-url //p' "$COMPOSE")"
expect_eq "compose db-url is data-dir + /db.sqlite3" \
	"$db_url_anchor" "$data_dir_anchor/db.sqlite3"
if grep -q 'RUSTICAL_DATA_STORE__SQLITE__DB_URL: \*db-url' "$COMPOSE"; then
	ok "the server is pointed at the db-url anchor, not a literal"
else
	bad "the server service does not use the db-url anchor — the image's baked-in \
RUSTICAL_DATA_STORE__SQLITE__DB_URL (rustical/Dockerfile:54) would override the \
wizard's config, and the server would run on an empty database"
fi
if grep -q 'OMNICAL_SETUP_DATA_DIR: \*data-dir' "$COMPOSE"; then
	ok "the wizard is pointed at the data-dir anchor"
else
	bad "the setup service does not use the data-dir anchor"
fi

# The installer's defaults and the unit template's placeholders are one contract.
step "The installer and the unit template agree"
"$BIN" setup --help >/dev/null 2>&1 || bad "'setup --help' failed"

# ── 2. The native path: install.sh, unattended, on a clean prefix ────────────
step "packaging/native/install.sh --no-start (unattended)"
OMNICAL_SETUP_DATA_DIR="$DATA" \
OMNICAL_SETUP_BIND="127.0.0.1:$PORT" \
OMNICAL_SETUP_TLS="none" \
OMNICAL_SETUP_REGISTRATION="invite-only" \
OMNICAL_SETUP_ADMIN_EMAIL="gate@example.com" \
OMNICAL_SETUP_ADMIN_PASSWORD="a-long-enough-password" \
	"$INSTALLER" --from-file "$BIN" --no-start --prefix "$PREFIX" \
		--config "$CONF" --data-dir "$DATA" --unattended >"$WORK/install.log" 2>&1 \
	|| { bad "install.sh failed:"; tail -30 "$WORK/install.log"; exit 1; }
note "install.sh exited 0"

[ -x "$PREFIX/bin/rustical" ] && ok "the binary is installed" || bad "no $PREFIX/bin/rustical"
[ -f "$CONF" ] && ok "the wizard wrote a config" || bad "no config at $CONF"
[ -f "$DATA/db.sqlite3" ] && ok "the database exists" || bad "no database at $DATA/db.sqlite3"
[ -f "$PREFIX/etc/systemd/system/omnical.service" ] \
	&& ok "the unit is installed" || bad "no unit in $PREFIX/etc/systemd/system"

conf_mode="$(stat -c '%a' "$CONF")"
expect_eq "config mode" "$conf_mode" "600"
data_mode="$(stat -c '%a' "$DATA")"
expect_eq "data dir mode" "$data_mode" "700"

# No placeholder may survive into the unit: an unexpanded @BINARY@ is an ExecStart
# that points nowhere, and systemd reports it as a generic start failure.
if grep -qE '@(BINARY|CONFIG|DATA|USER|GROUP)@' "$PREFIX/etc/systemd/system/omnical.service"; then
	bad "the installed unit still has unsubstituted placeholders"
else
	ok "every unit placeholder was substituted"
fi
expect_contains "ExecStart names the installed binary" \
	"$PREFIX/etc/systemd/system/omnical.service" "$PREFIX/bin/rustical --config-file $CONF serve"
expect_contains "StateDirectory is declared" \
	"$PREFIX/etc/systemd/system/omnical.service" "StateDirectory=omnical"

# The wizard's own answers, read back out of the config it wrote.
step "The config the wizard wrote"
expect_contains "the bind answer is in the config" "$CONF" "127.0.0.1:$PORT"
expect_contains "registration is invite-only" "$CONF" "invite_required = true"
# The administrator's *email* is a database row, not a config key. If it ever
# lands in the config, the 0600 mode stops being the only thing between a
# leaked config and a leaked account list.
expect_absent "the admin email is not written to the config" "$CONF" "gate@example.com"
# The RSVP secret is generated into the config and must never be printed.
expect_contains "an RSVP secret was generated into the config" "$CONF" "rsvp_secret"
expect_absent "the RSVP secret did not reach stdout" "$WORK/install.log" \
	"$(sed -n 's/^rsvp_secret = "\(.*\)"/\1/p' "$CONF" | head -1)"

# ── 3. It actually serves, and a real client round-trips ─────────────────────
step "Booting the server on the generated config"
"$BIN" --config-file "$CONF" serve >"$WORK/server.log" 2>&1 &
PID=$!

waited=0
until "$BIN" --config-file "$CONF" health >/dev/null 2>&1; do
	waited=$((waited + 1))
	if [ "$waited" -gt 30 ]; then
		bad "the server never answered /ping"
		tail -30 "$WORK/server.log"
		exit 1
	fi
	sleep 1
done
ok "the server answers /ping (after ${waited}s)"

# `rustical health` parses the config and GETs /ping, so reaching this line means
# the wizard's config loaded under `deny_unknown_fields` (row 45) *and* the
# migrations ran. Row 40's first claim.
expect_eq "/ping body" "$(curl -fsS "http://127.0.0.1:$PORT/ping")" "Pong!"

# A real CalDAV client round trip, as vdirsyncer/DAVx5 would do it.
#
# The URL shape is /caldav/principal/<principal>/<calendar>/ — the literal
# `principal/` segment is part of the RFC 6578 collection layout that
# CalDavPrincipalUri builds (crates/caldav/src/lib.rs:38). A self-hoster
# following the per-client instructions in the portal will use exactly this
# shape, so the gate does too.
BASE="http://127.0.0.1:$PORT"
ADMIN="gate@example.com"
ADMIN_HOME="$BASE/caldav/principal/$ADMIN/"
# Note the braces when a path is appended. "$PERSONAL"selfhost-gate.ics" looks
# like a concatenation and is not: the trailing quote *opens* a new string, and
# the error surfaces as "unexpected EOF while looking for matching `"'" hundreds
# of lines away from the cause. Braces everywhere a variable is glued to
# something.
PERSONAL="$BASE/caldav/principal/$ADMIN/personal/"

# `principals app-token create` prints "<id>_<secret>"; the whole string is the
# credential, and it is the only time the secret is ever shown.
token="$("$BIN" --config-file "$CONF" principals app-token create "$ADMIN" --name gate | tail -1)"
if [ -n "$token" ]; then ok "issued an app token (${#token} chars)"; else bad "could not issue an app token"; fi
auth="$ADMIN:$token"

code="$(curl -s -o /dev/null -w '%{http_code}' -u "$auth" -X PROPFIND -H 'Depth: 0' "$BASE/caldav/")"
expect_eq "PROPFIND /caldav/ (root)" "$code" "207"

code="$(curl -s -o /dev/null -w '%{http_code}' -u "$ADMIN:wrong-token-value" -X PROPFIND -H 'Depth: 0' "$BASE/caldav/")"
expect_eq "PROPFIND with a wrong token" "$code" "401"

# An unauthenticated request must not be served, or the whole server is public.
code="$(curl -s -o /dev/null -w '%{http_code}' -X PROPFIND -H 'Depth: 0' "$BASE/caldav/")"
expect_eq "PROPFIND with no credentials" "$code" "401"

# The collections the wizard seeded. Before seed_collections was shared with
# `rustical setup` (this gate found it) the administrator had none and this 404'd
# — on the account the installer had just created for them.
for cal in personal tasks; do
	code="$(curl -s -o /dev/null -w '%{http_code}' -u "$auth" -X PROPFIND -H 'Depth: 1' "$ADMIN_HOME$cal/")"
	expect_eq "PROPFIND the seeded '$cal' calendar" "$code" "207"
done

code="$(curl -s -o /dev/null -w '%{http_code}' -u "$auth" -X PROPFIND -H 'Depth: 1' "$BASE/carddav/principal/$ADMIN/personal/")"
expect_eq "PROPFIND the seeded addressbook" "$code" "207"

# …and they are not empty: a welcome object is what makes a first sync feel like
# it worked rather than like a silent failure.
curl -s -u "$auth" -X PROPFIND -H 'Depth: 1' "$PERSONAL" > "$WORK/personal-propfind.xml"
welcome_href="$(tr '<' '\n' < "$WORK/personal-propfind.xml" \
	| grep -E '^href>/caldav/principal/.*\.ics$' \
	| head -1 | cut -d'>' -f2-)"
if [ -n "$welcome_href" ]; then
	ok "the personal calendar lists an object ($welcome_href)"
	curl -s -u "$auth" "$BASE$welcome_href" > "$WORK/welcome.ics"
	expect_contains "the welcome object downloads" "$WORK/welcome.ics" "SUMMARY:Welcome"
else
	bad "the personal calendar is empty — a self-hoster's first sync would look broken"
fi

# A REPORT, because that is what a real client sends and a PROPFIND is not a
# substitute for one. The heredoc keeps the XML readable and keeps the shell out
# of the quoting.
code="$(curl -s -o "$WORK/report.xml" -w '%{http_code}' -u "$auth" -X REPORT \
	-H 'Depth: 1' -H 'Content-Type: application/xml' --data-binary @- "$PERSONAL" <<'XML'
<?xml version="1.0" encoding="utf-8" ?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop><D:getetag/></D:prop>
  <C:filter><C:comp-filter name="VCALENDAR"/></C:filter>
</C:calendar-query>
XML
)"
expect_eq "REPORT calendar-query on 'personal'" "$code" "207"
if grep -q "<response" "$WORK/report.xml"; then
	ok "the REPORT returns at least one object"
else
	bad "the REPORT returned no objects"
fi

# A write, and a read-back.
cat > "$WORK/event.ics" <<'ICS'
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Omnical self-host gate//EN
BEGIN:VEVENT
UID:selfhost-gate@example.com
DTSTAMP:20260928T120000Z
DTSTART:20260929T120000Z
DTEND:20260929T130000Z
SUMMARY:Self-host gate
END:VEVENT
END:VCALENDAR
ICS
code="$(curl -s -o /dev/null -w '%{http_code}' -u "$auth" -X PUT \
	-H 'Content-Type: text/calendar' --data-binary "@$WORK/event.ics" \
	"${PERSONAL}selfhost-gate.ics")"
expect_eq "PUT an event" "$code" "201"
curl -s -u "$auth" "${PERSONAL}selfhost-gate.ics" > "$WORK/fetched.ics"
expect_contains "GET the event back" "$WORK/fetched.ics" "SUMMARY:Self-host gate"

# ── 4. A user registers, which is row 40's second claim ─────────────────────
step "Registration (the invite-only path compose.omnical.yml ships)"
code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/register")"
expect_eq "GET /register" "$code" "200"

# The form is CSRF-protected and the token lives in the session, so this has to
# be done the way a browser does it: GET with a cookie jar, scrape the token,
# POST with the same jar. A gate that posts a guessed token would prove nothing.
jar="$WORK/cookies"
curl -fsS -c "$jar" "http://127.0.0.1:$PORT/register" > "$WORK/register-form.html"
csrf="$(sed -n 's/.*name="csrf" value="\([^"]*\)".*/\1/p' "$WORK/register-form.html" | head -1)"
[ -n "$csrf" ] && ok "the form carries a CSRF token" || bad "no CSRF token in the form"
expect_contains "the form asks for an invite code" "$WORK/register-form.html" 'name="invite"'

invites="$("$BIN" --config-file "$CONF" invites create --expires 2030-01-01)"
invite="$(echo "$invites" | grep -oE '[A-Za-z0-9]{12}' | head -1)"
[ -n "$invite" ] && ok "minted an invite code" || { bad "could not mint an invite"; echo "$invites"; }

post_register() { # <invite-code> <email> <outfile>
	curl -s -o "$3" -w '%{http_code}' -b "$jar" -c "$jar" -X POST \
		--data-urlencode "csrf=$csrf" \
		--data-urlencode "email=$2" \
		--data-urlencode 'displayname=' \
		--data-urlencode 'password=a-long-enough-password' \
		--data-urlencode 'password_confirm=a-long-enough-password' \
		--data-urlencode "invite=$1" \
		"http://127.0.0.1:$PORT/register"
}

# Wrong code -> the form comes back with an error, not an account. A
# registration form that accepts a blank or wrong invite is the difference
# between "invite-only" and "open", so this is asserted rather than assumed.
code="$(post_register 'wrongcodewrong1' 'someone@example.com' "$WORK/bad.html")"
expect_eq "a wrong invite is rejected" "$code" "400"
if grep -qi 'gate@example.com' "$WORK/bad.html"; then
	bad "the rejected form leaked the administrator's address"
else
	ok "the rejected form leaks no account information"
fi

# Wrong code twice over, to be sure the first attempt did not burn it.
code="$(post_register "$invite" 'newuser@example.com' "$WORK/good.html")"
expect_eq "a valid invite registers a user" "$code" "200"
expect_contains "the success card is shown" "$WORK/good.html" 'newuser@example.com'

principals="$("$BIN" --config-file "$CONF" principals list)"
echo "$principals" | grep -q 'newuser@example.com' \
	&& ok "the new principal is in the database" \
	|| bad "the new principal is missing:"$'\n'"$principals"

# The invite is single-use, which is the whole security property of the mode.
code="$(post_register "$invite" 'thief@example.com' "$WORK/reuse.html")"
expect_eq "the same invite cannot be reused" "$code" "400"

# The new user can actually sync, which is the last claim in row 40.
new_token="$("$BIN" --config-file "$CONF" principals app-token create newuser@example.com --name gate | tail -1)"
code="$(curl -s -o /dev/null -w '%{http_code}' -u "newuser@example.com:$new_token" \
	-X PROPFIND -H 'Depth: 1' "$BASE/caldav/principal/newuser@example.com/personal/")"
expect_eq "the registered user syncs their own calendar" "$code" "207"

# ── 5. Re-running the installer is an upgrade, not a reinstall ───────────────
step "Re-running install.sh over a live install"
# The administrator's password hash, so the re-run can be *proved* not to have
# reset it. That is row 44's property, checked through the installer rather than
# through the wizard's own test — an installer that re-ran the wizard wrongly
# would not be caught by the wizard's tests at all.
hash_before=""
if command -v sqlite3 >/dev/null 2>&1; then
	hash_before="$(sqlite3 "$DATA/db.sqlite3" \
		"select password_hash from principals where id='gate@example.com'" 2>/dev/null || echo "")"
	[ -n "$hash_before" ] && ok "read the administrator's password hash" \
		|| note "could not read the hash; the re-run is still checked for data loss"
else
	note "no sqlite3 on this host; the password-hash assertion is skipped"
fi

OMNICAL_SETUP_DATA_DIR="$DATA" \
OMNICAL_SETUP_BIND="127.0.0.1:$PORT" \
OMNICAL_SETUP_TLS="none" \
OMNICAL_SETUP_REGISTRATION="invite-only" \
OMNICAL_SETUP_ADMIN_EMAIL="gate@example.com" \
	"$INSTALLER" --from-file "$BIN" --no-start --prefix "$PREFIX" \
		--config "$CONF" --data-dir "$DATA" --unattended >"$WORK/reinstall.log" 2>&1 \
	|| { bad "the re-run failed:"; tail -30 "$WORK/reinstall.log"; fails=$((fails + 1)); }
# The password is deliberately absent from the second run's environment, so this
# also proves the claim install.sh's output makes about being able to drop it.
if grep -q 'already exists' "$WORK/reinstall.log"; then
	ok "the re-run left the existing administrator alone"
else
	bad "the re-run did not report 'already exists' — did it create a second one?"
fi

if [ -n "$hash_before" ]; then
	hash_after="$(sqlite3 "$DATA/db.sqlite3" \
		"select password_hash from principals where id='gate@example.com'" 2>/dev/null || echo "")"
	expect_eq "the password hash across the re-run" "$hash_after" "$hash_before"
fi

rsvp_before="$(sed -n '/rsvp_secret/ s/.*= "\(.*\)"/\1/p' "$CONF" | head -1)"
[ -n "$rsvp_before" ] && ok "the config carries an RSVP secret" \
	|| bad "no RSVP secret in the config — every invitation link would be unverifiable"
rsvp_after="$(sed -n '/rsvp_secret/ s/.*= "\(.*\)"/\1/p' "$CONF" | head -1)"
expect_eq "the RSVP secret across the re-run" "$rsvp_after" "$rsvp_before"

principals_after="$("$BIN" --config-file "$CONF" principals list)"
echo "$principals_after" | grep -q 'newuser@example.com' \
	&& ok "the registered user survived the re-run" \
	|| bad "the re-run lost the registered user"
expect_eq "still exactly 2 principals" \
	"$(echo "$principals_after" | grep -c '@')" "2"

# ── 6. The attended path — the DEFAULT, and the one a self-hoster uses ──────
step "install.sh without --unattended (the default: the wizard asks)"
# This section exists because the unattended work nearly broke it, in the worst
# possible way. The answer flags are `env`-backed, so an OMNICAL_SETUP_* variable
# in the environment pre-answered question 1 of the *interactive* wizard: the
# operator's typed answers shifted by one, and because `HttpBindConfig::from_str`
# accepts almost any string as a host, the data directory was accepted as the
# listen address. The run reported success at every step and wrote
# `bind = "/var/lib/omnical"`.
#
# The wizard now discards the environment unless `--unattended`, and install.sh
# passes `--data-dir` as a flag only when unattended. Both halves are asserted
# here, and the two variables are set below on purpose.
ATT="$WORK/attended"
mkdir -p "$ATT"
printf '%s\n' \
	"$ATT/data" \
	"127.0.0.1:$PORT" \
	"https://cal.example.com" \
	"c" \
	"n" \
	"n" \
	"i" \
	"attended@example.com" \
	"a-long-enough-password" \
	"a-long-enough-password" \
	| OMNICAL_SETUP_DATA_DIR="$ATT/data-from-the-environment" \
	  OMNICAL_SETUP_BIND="10.0.0.1:1" \
	  "$INSTALLER" --from-file "$BIN" --no-start --prefix "$ATT/usr" \
		--config "$ATT/config.toml" --data-dir "$ATT/data" \
		> "$WORK/attended.log" 2>&1 \
	|| { bad "the attended install failed:"; tail -20 "$WORK/attended.log"; }

if [ -f "$ATT/config.toml" ]; then
	att_bind="$(sed -n 's/^bind = "\(.*\)"/\1/p' "$ATT/config.toml" | head -1)"
	expect_eq "the attended bind is the operator's answer" "$att_bind" "127.0.0.1:$PORT"
	att_db="$(sed -n 's/^db_url = "\(.*\)"/\1/p' "$ATT/config.toml" | head -1)"
	expect_eq "the attended data dir is the operator's answer" "$att_db" "$ATT/data/db.sqlite3"
	expect_absent "the wizard ignored OMNICAL_SETUP_DATA_DIR" \
		"$ATT/config.toml" "data-from-the-environment"
	expect_absent "the wizard ignored OMNICAL_SETUP_BIND" \
		"$ATT/config.toml" "10.0.0.1"
else
	bad "the attended install wrote no config at $ATT/config.toml"
fi

# ── 7. The unattended path refuses to guess ─────────────────────────────────
step "An unattended install with a missing answer fails loudly"
out="$(OMNICAL_SETUP_DATA_DIR="$WORK/var/lib/other" \
OMNICAL_SETUP_BIND="127.0.0.1:$PORT" \
OMNICAL_SETUP_TLS="none" \
OMNICAL_SETUP_REGISTRATION="open" \
OMNICAL_SETUP_ADMIN_PASSWORD="a-long-enough-password" \
	"$BIN" --config-file "$WORK/etc/other.toml" setup --unattended 2>&1)" && rc=0 || rc=$?
if [ "$rc" != 0 ]; then
	ok "a missing OMNICAL_SETUP_ADMIN_EMAIL exits non-zero ($rc)"
	echo "$out" | grep -q 'OMNICAL_SETUP_ADMIN_EMAIL' \
		&& ok "and the error names the variable" \
		|| bad "the error does not name the variable: $out"
else
	bad "a missing required answer succeeded — an unattended install that invents \
an administrator is the failure mode this mode exists to prevent"
fi

# ── Verdict ─────────────────────────────────────────────────────────────────
echo
if [ "$fails" -eq 0 ]; then
	cat <<EOF
$(printf '\033[1;32mself-host gate: PASS\033[0m')

  row 41  native path    install.sh -> wizard -> server -> DAV round trip
  row 40  compose path   unattended install, registration and client sync, all
                         driven by the same wizard answers the container gets,
                         plus every container-free assertion in the compose file

  Running a container runtime is a stretch goal, not a gate
  (PLAN_DEPLOYMENTS.md §18.7). The self-host channel stands on its own: the
  native path above reaches the identical end state with no container, which is
  why row 40 was never allowed to depend on one.
EOF
	exit 0
fi
printf '\033[1;31mself-host gate: %d FAILURE(S)\033[0m\n' "$fails"
exit 1
