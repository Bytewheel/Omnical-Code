#!/usr/bin/env bash
# render-router-config.sh — Render the router's /etc/rustical/config.toml with
# the [scheduling] section enabled, the 8 SMTP accounts injected from pass,
# the 7 IMAP accounts for inbound iMIP reply ingestion, and the RSVP-link
# HMAC secret (auto-generated into pass on first render).
#
# Rationale (§17.2 user decision 2026-09-05): SMTP passwords are deployed ONLY
# into the router's 0600 /etc/rustical/config.toml (same protection class as
# the TLS key); ~/router-dav stays secret-free.  This script therefore prints
# the rendered config to STDOUT — pipe it straight into ssh:
#
#   scripts/render-router-config.sh | ssh router 'cat > /etc/rustical/config.toml'
#
# The secret-free base template lives at router/etc/rustical/config.toml.
# The SMTP account table below mirrors the §17.2 inventory (recon from
# ~/.config/msmtp/config, all STARTTLS, verified working senders).
#   NB: zero@novo-ordo.com and burningserenity@novo-ordo.com share
#   nfcalaway@novo-ordo.com's pass entry — all novo-ordo identities
#   authenticate as nfcalaway@novo-ordo.com (same credentials as the
#   existing msmtp accounts).
#   NB: ORDER MATTERS — the FIRST entry is the From address the app itself
#   sends from (registration invites, guest-share credentials, password
#   reset links; iMIP invitations always match the organizer instead).
#   burningserenity@novo-ordo.com took over that role from
#   burningserenity@gmail.com on 2026-09-22.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE="$ROOT/router/etc/rustical/config.toml"
[ -f "$BASE" ] || { echo "!! missing base template $BASE" >&2; exit 1; }

# identity|host|port|username|pass-entry
ACCOUNTS="
burningserenity@novo-ordo.com|smtp.novo-ordo.com|587|nfcalaway@novo-ordo.com|secrets/email/nfcalaway@novo-ordo.com/smtp
burningserenity@gmail.com|smtp.gmail.com|587|burningserenity@gmail.com|secrets/email/burningserenity@gmail.com/smtp
nfcarlton@gmail.com|smtp.gmail.com|587|nfcarlton@gmail.com|secrets/email/nfcarlton@gmail.com/smtp
nfcalaway@gmail.com|smtp.gmail.com|587|nfcalaway@gmail.com|secrets/email/nfcalaway@gmail.com/smtp
nicholas@hawksnestsoftware.com|smtp.gmail.com|587|nicholas@hawksnestsoftware.com|secrets/email/nicholas@hawksnestsoftware.com/smtp
nicholas@carltonaudio.com|netsol-smtp-oxcs.hostingplatform.com|587|nicholas@carltonaudio.com|secrets/email/nicholas@carltonaudio.com/smtp
nfcalaway@novo-ordo.com|smtp.novo-ordo.com|587|nfcalaway@novo-ordo.com|secrets/email/nfcalaway@novo-ordo.com/smtp
zero@novo-ordo.com|smtp.novo-ordo.com|587|nfcalaway@novo-ordo.com|secrets/email/nfcalaway@novo-ordo.com/smtp
"

# identity|host|port|username|pass-entry|ca-file (optional)
#
# The ca-file column pins extra TLS trust anchors for providers serving an
# incomplete chain: imap.novo-ordo.com:993 omits its Sectigo intermediate
# (every poll failed with UnknownIssuer), so the two novo-ordo accounts
# point at the intermediate shipped in router/etc/rustical/certs/ (deployed
# by deploy.sh to /etc/rustical/certs/). Leave empty for complete chains.
IMAP_ACCOUNTS="
burningserenity@gmail.com|imap.gmail.com|993|burningserenity@gmail.com|secrets/email/burningserenity@gmail.com/imap|
nfcarlton@gmail.com|imap.gmail.com|993|nfcarlton@gmail.com|secrets/email/nfcarlton@gmail.com/imap|
nfcalaway@gmail.com|imap.gmail.com|993|nfcalaway@gmail.com|secrets/email/nfcalaway@gmail.com/imap|
nicholas@hawksnestsoftware.com|imap.gmail.com|993|nicholas@hawksnestsoftware.com|secrets/email/nicholas@hawksnestsoftware.com/imap|
nicholas@carltonaudio.com|netsol-imap-oxcs.hostingplatform.com|993|nicholas@carltonaudio.com|secrets/email/nicholas@carltonaudio.com/imap|
nfcalaway@novo-ordo.com|imap.novo-ordo.com|993|nfcalaway@novo-ordo.com|secrets/email/nfcalaway@novo-ordo.com/imap|/etc/rustical/certs/imap-novo-ordo.pem
zero@novo-ordo.com|imap.novo-ordo.com|993|nfcalaway@novo-ordo.com|secrets/email/nfcalaway@novo-ordo.com/imap|/etc/rustical/certs/imap-novo-ordo.pem
"

toml_escape() {  # basic-string escaping: backslash then double quote
	sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' <<<"$1"
}

cat "$BASE"

# One-click RSVP links (PLAN_SHARING.md §9 item 3): the HMAC secret minting
# the /rsvp/{token} response links in invitation emails. AUTO-GENERATED into
# pass on first render (32 random bytes, hex) so the deploy flow stays one
# command; re-renders reuse the stored secret — rotating it would invalidate
# every outstanding link (the endpoint 404s until attendees are re-invited).
# rsvp_base_url is deliberately NOT rendered: build_extensions falls back to
# [subscriptions] public_url. pass insert stdout is discarded — its prompts
# and mkdir chatter must never leak into the rendered config on stdout.
RSVP_ENTRY="secrets/omnical/rsvp-secret"
# NB: the probes need "|| true" — pass exits 1 on a missing entry and
# set -o pipefail would abort the pipeline before the generate branch runs.
rsvp_secret="$(pass show "$RSVP_ENTRY" 2>/dev/null | head -n1 | tr -d '\r\n' || true)"
if [ -z "$rsvp_secret" ]; then
	echo "generating pass entry '$RSVP_ENTRY' (32 random bytes, hex)" >&2
	rsvp_secret="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
	pass insert -m -f "$RSVP_ENTRY" <<<"$rsvp_secret" >/dev/null
	rsvp_secret="$(pass show "$RSVP_ENTRY" 2>/dev/null | head -n1 | tr -d '\r\n' || true)"
fi
[ -n "$rsvp_secret" ] || {
	echo "!! pass entry '$RSVP_ENTRY' missing or empty — aborting render" >&2
	exit 1
}

cat <<'EOF'

[scheduling]
# Omnical RFC 6638 implicit-scheduling extension (§17.2).
enabled = true
EOF
# HMAC key for the one-click /rsvp/{token} response links (§9 item 3).
echo "# HMAC key for the one-click /rsvp/{token} response links (§9 item 3)."
echo "rsvp_secret = \"$(toml_escape "$rsvp_secret")\""

n=0
while IFS='|' read -r identity host port username pass_entry; do
	[ -n "$identity" ] || continue
	password="$(pass show "$pass_entry" 2>/dev/null | head -n1 | tr -d '\r\n')"
	[ -n "$password" ] || {
		echo "!! pass entry '$pass_entry' (for $identity) missing or empty — aborting render" >&2
		exit 1
	}
	n=$((n + 1))
	echo ""
	echo "[[scheduling.smtp]]"
	echo "identity = \"$(toml_escape "$identity")\""
	echo "host = \"$host\""
	echo "port = $port"
	echo "username = \"$(toml_escape "$username")\""
	echo "password = \"$(toml_escape "$password")\""
done <<<"$ACCOUNTS"

m=0
while IFS='|' read -r identity host port username pass_entry ca_file; do
	[ -n "$identity" ] || continue
	password="$(pass show "$pass_entry" 2>/dev/null | head -n1 | tr -d '\r\n')"
	[ -n "$password" ] || {
		echo "!! pass entry '$pass_entry' (for $identity) missing or empty — aborting render" >&2
		exit 1
	}
	m=$((m + 1))
	echo ""
	echo "[[scheduling.imap]]"
	echo "identity = \"$(toml_escape "$identity")\""
	echo "host = \"$host\""
	echo "port = $port"
	echo "username = \"$(toml_escape "$username")\""
	echo "password = \"$(toml_escape "$password")\""
	# Optional extra trust anchors (incomplete-chain providers). The file
	# must exist on the router — deploy.sh pushes it to this exact path.
	if [ -n "$ca_file" ]; then
		[ -f "$ROOT/router/etc/rustical/certs/$(basename "$ca_file")" ] || {
			echo "!! ca_file '$ca_file' has no counterpart in router/etc/rustical/certs/ — aborting render" >&2
			exit 1
		}
		echo "ca_file = \"$(toml_escape "$ca_file")\""
	fi
done <<<"$IMAP_ACCOUNTS"

# §17.7 share-links extension: unauthenticated /export/<token>.{ics,vcf} feeds
# (the token in the URL is the only credential).  Managed server-side via
# `rustical subscriptions add|list|remove` (§17.7 item 3).  public_url is what
# the CLI prefixes to printed export URLs — the public dav-tls front end,
# NOT the 127.0.0.1:4000 HTTP bind.
cat <<'EOF'

[subscriptions]
enabled = true
public_url = "https://0115d8cf.duckdns.org:8443"
EOF

# §17.8 invitation-gated self-service registration: the public /register form
# mounts only while enabled = true, and every account is gated behind a
# single-use invite (CLI `rustical invites` or the portal Share section).
# Defaults are spelled out explicitly (§17.8.5) — the binary's own serde
# defaults match, but the live router must not depend on them silently.
cat <<'EOF'

[registration]
enabled = true                  # public self-service registration (§17.8)
invite_required = true          # single-use invite codes required (public default)
min_password_length = 12
auto_app_tokens = ["vdirsyncer", "davx5", "thunderbird", "apple", "i3status"]
auto_subscription = true        # create personal share feeds on registration
default_group = ""              # "" = no auto group (2026-09-07 decision)
rate_limit_per_hour = 10        # per-IP; plus a fixed global bucket 60/h
EOF

echo "rendered $n SMTP accounts, $m IMAP accounts, rsvp_secret from pass" >&2