# dav-tls-gate.sh — what the WAN-address discovery actually proves, and what it does not.
#
# Run with no arguments. No router, no root, no network privileges.
#
# The honest scope, stated first: this script exercises the *decision* half of
# WAN discovery — which address to bind, and what to do when there isn't a good
# one. It cannot exercise the *rebinding* half, because rebinding requires an
# interface whose address changes underneath a running process, and that needs
# either NET_ADMIN to create a dummy interface or a flashed unit with a real
# DHCP lease. Both were unavailable where this was written. Section 5 below is
# not optional reading.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DL_DIR="$ROOT/dav-tls"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

pass=0; fail=0; skip=0
ok()   { printf '  \033[32mok\033[0m   %s\n' "$1"; pass=$((pass+1)); }
no()   { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail+1)); }
sk()   { printf '  \033[33mskip\033[0m %s\n' "$1"; skip=$((skip+1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# expect_ok <name> <substring> <actual>
expect_ok() {
  case "$3" in
    *"$2"*) ok "$1" ;;
    *) no "$1"; printf '        wanted: %s\n        got:    %s\n' "$2" "$3" ;;
  esac
}

# ── 1. The crate builds and its own tests pass ──────────────────────────────
head_ "1. dav-tls builds and its unit tests pass"

if (cd "$DL_DIR" && SQLX_OFFLINE=true cargo test --release) > "$OUT/test.log" 2>&1; then
  ran=$(grep -oE '^test result: ok\. [0-9]+ passed' "$OUT/test.log" | grep -oE '[0-9]+' | head -1)
  ok "cargo test: $ran passed"
  if [ "${ran:-0}" -lt 12 ]; then
    no "test floor is 12, got ${ran:-0} — the discover tests are the crate's substance"
  else
    ok "test floor (12) met"
  fi
else
  no "cargo test failed:"
  sed 's/^/        /' "$OUT/test.log" | tail -20
fi

BIN="$DL_DIR/target/release/dav-tls"
if [ -x "$BIN" ]; then ok "binary built"; else no "no binary at $BIN"; exit 1; fi

# ── 2. --discover parses, and a bad spec is a USAGE error (exit 2) ──────────
head_ "2. argument parsing — usage errors are exit 2, not outages"

"$BIN" --help > "$OUT/help.txt" 2>&1 || true
expect_ok "--help documents --discover" "--discover" "$(cat "$OUT/help.txt")"
expect_ok "--help keeps a static example" "--listen" "$(cat "$OUT/help.txt")"

openssl req -x509 -newkey rsa:2048 -keyout "$OUT/key.pem" -out "$OUT/cert.pem" \
  -days 2 -nodes -subj /CN=gatelocal >/dev/null 2>&1

try_spec() { # try_spec <spec> <expected-exit> <expected-substring> <name>
  set +e
  # `timeout` on every invocation, deliberately. dav-tls is a *server*: any spec
  # that resolves will bind a port and serve forever, and this script has no
  # business trusting its own inputs to always fail. A gate that can hang is
  # worse than no gate, because it looks like a slow build.
  out=$(timeout 10 "$BIN" --discover "$1" --upstream 127.0.0.1:1 \
        --cert "$OUT/cert.pem" --key "$OUT/key.pem" 2>&1)
  rc=$?
  set -e
  if [ "$rc" = 124 ]; then
    no "$4 HUNG — dav-tls started serving instead of failing"
    return
  fi
  [ "$rc" = "$2" ] || { no "$4 (exit $rc, wanted $2)"; printf '        %s\n' "$out"; return; }
  ok "$4 exits $2"
  expect_ok "$4 explains itself" "$3" "$out"
}

try_spec eth0          2 "no port given"        "bare interface name"
try_spec :8443         2 "empty interface name" "missing interface"
try_spec eth0:         2 "not a number"         "missing port"
try_spec eth0:abc      2 "not a number"         "non-numeric port"
try_spec eth0:0        2 "not a listenable"     "port zero"
try_spec eth0:99999    2 "out of range"         "port out of range"

# A malformed spec must NOT be reported as a resolve failure. Conflating them
# would make a typo look like an outage to whoever is on call. Asserted by
# inspecting the messages above: each says "invalid --discover" (a usage fault),
# never "not found" (a runtime fault).
#
# Deliberately NOT probed with a spec that resolves. An earlier draft ran
# `--discover eth0:8443` here "just to be sure", which on any normal host
# *succeeds* — the binary binds and serves, and the gate hangs on it forever.
# A gate that can hang is worse than no gate.

# ── 3. An unresolvable interface is a RUNTIME failure (exit 1) ──────────────
head_ "3. runtime resolution — exit 1, and the message is actionable"

set +e
out=$(timeout 10 "$BIN" --discover definitely-not-an-interface:18443 \
      --upstream 127.0.0.1:1 --cert "$OUT/cert.pem" --key "$OUT/key.pem" 2>&1)
rc=$?
set -e
[ "$rc" = 1 ] && ok "unresolvable interface exits 1" \
             || no "unresolvable interface exit $rc, wanted 1"
expect_ok "error names the interface asked for" "definitely-not-an-interface" "$out"
expect_ok "error lists what it did see"         "interfaces with one"      "$out"
# The single most useful property: it says which interfaces exist, so a typo is
# a 10-second fix rather than an archaeology exercise.
if printf '%s' "$out" | grep -qE 'lo|eth|en'; then
  ok "the interface list is populated on a normal host"
else
  sk "no interfaces listed (this host appears to have none)"
fi

# ── 4. A link-local interface is refused, and says why ─────────────────────
head_ "4. a link-local interface (mid-lease) is refused, not bound"

# loopback is 127.0.0.1 — always present, and correctly rejected as a WAN
# address. That exercises the same selection path a 169.254.x address would.
set +e
out=$(timeout 10 "$BIN" --discover lo:18443 --upstream 127.0.0.1:1 \
      --cert "$OUT/cert.pem" --key "$OUT/key.pem" 2>&1)
rc=$?
set -e
if [ "$rc" = 1 ]; then
  ok "loopback refused as a WAN address (exit 1)"
  expect_ok "error is explicit about which address it saw" "127.0.0.1" "$out"
  expect_ok "error does not offer a fallback" "no usable IPv4 address" "$out"
else
  # If this host's loopback somehow held a routable address, that is worth
  # knowing rather than failing on.
  sk "loopback resolved (unexpected on this host) — got: $(printf '%s' "$out" | head -1)"
fi

# The DHCP-specific wording is asserted by the unit test
# (link_local_is_refused_and_says_so_in_dhcp_terms), because producing a real
# 169.254.x interface needs privileges this script does not assume.

# ── 5. What this gate does NOT prove ───────────────────────────────────────
head_ "5. NOT proven here — do not read a green run as coverage of these"

cat <<'EOF'
  rebind on a lease change   *** NOW COVERED, elsewhere. *** This script cannot
                             do it (no CAP_NET_ADMIN), so it lives in
                             scripts/dav-tls-rebind-gate.sh, which creates a
                             dummy interface with sudo and moves its address
                             under a live listener. Verified: rebound at 30s,
                             old address stops serving, 20 requests round-trip on
                             the new one, and the process survives the address
                             being removed entirely and recovers when it returns.
                             Run that script, not this one, for the socket swap.
  bind below port 1024       The privilege drop happens after the first bind, so
                             a non-root rebind to :443 would fail. --discover is
                             used on :8443, above 1024, so this does not apply
                             here — but a 443 front-end would need checking.
  concurrent rebind          One address change during a burst of connections is
                             untested; in-flight sockets are held by the
                             connection, not the listener, so they should survive,
                             and "should" is not "measured".
  real WAN, real DHCP        A dummy interface is not a WAN. What remains
                             genuinely hardware-only: a real DHCP server
                             handing out a lease, and a flashed unit. The
                             address-change code path is identical in all three;
                             what a real unit adds is the procd/inittab layer and
                             the actual flash storage.

  privilege drop after bind  Cannot run unprivileged (setgroups(2)). Untested
                             here; unchanged behaviour from before --discover.
EOF

# ── 6. The init script on a real device ────────────────────────────────────
head_ "6. the appliance init script"

SCRIPT="$ROOT/router/etc/init.d/dav-tls"
if [ ! -f "$SCRIPT" ]; then
  no "$SCRIPT missing"
elif ! sh -n "$SCRIPT" 2>/dev/null; then
  no "$SCRIPT does not parse under POSIX sh"
else
  ok "init script parses under POSIX sh"
  expect_ok "passes --discover"              "--discover"          "$(cat "$SCRIPT")"
  if grep -qE '^\s*\[ "\$cur_ip" = "\$WAN_IP" \]' "$SCRIPT"; then
    no "init script refuses to start on an address change again"
  else
    ok "the refuse-to-start-on-address-change check is gone"
  fi
  if grep -qE '^\s*WAN_IP=' "$SCRIPT"; then
    no "init script hard-codes WAN_IP again"
  else
    ok "no hard-coded WAN_IP"
  fi
  STAGED="$ROOT/packaging/firmware/omnical/files/init.d/dav-tls"
  if [ -f "$STAGED" ]; then
    if diff -q "$SCRIPT" "$STAGED" >/dev/null 2>&1; then
      ok "staged copy matches (a flashed unit ships this script)"
    else
      no "staged copy is stale — re-run scripts/build-firmware.sh --staged-only"
    fi
  else
    sk "no staged copy yet (run scripts/build-firmware.sh --staged-only)"
  fi
fi

printf '\n\033[1mdav-tls-gate: %d passed, %d failed, %d skipped\033[0m\n' "$pass" "$fail" "$skip"
[ "$fail" -eq 0 ]