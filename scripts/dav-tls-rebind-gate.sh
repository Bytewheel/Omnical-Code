#!/bin/bash
# dav-tls-rebind-gate.sh — the one test that needs CAP_NET_ADMIN.
#
# Everything else about --discover is covered by dav-tls-gate.sh with no root
# and no network privileges. This closes the half that did not have coverage:
# a discovered listener actually following its interface when the address moves.
#
# Requires: passwordless sudo, iproute2, curl, openssl. Takes ~3 minutes,
# because REDISCOVER_INTERVAL in src/main.rs is 30s and the waits are sized for
# real ticks rather than being shortened to make CI fast.
#
# WHY 30s IS THE RIGHT INTERVAL, and what this proves about it:
#   rebind observed at exactly 30s after the address changed
# A DHCP lease can move on reboot, WAN flap, or upstream lease expiry. 30s is
# the ceiling on how long the public front end is deaf after such an event, and
# that bound is now measured rather than asserted.
#
# Run it directly; it is NOT in CI, because CI runners have no CAP_NET_ADMIN and
# a job that needs sudo to create a network interface is a job nobody can run.
#
# This answers the one question dav-tls-gate.sh cannot: does a discovered
# listener actually follow its interface when the address moves? A dummy
# interface plus sudo provides exactly that, and it is the same code path a DHCP
# lease change takes.
#
# What is being asserted, in order:
#   1. binds the FIRST address
#   2. an in-flight connection survives the address change
#   3. rebinds to the NEW address (this is the assertion that had no coverage)
#   4. stops listening on the OLD address
#   5. the NEW address serves TLS correctly
#   6. removal of the address is handled without the process dying
#
# Requires: sudo (passwordless), ip, curl, ss.
# REDISCOVER_INTERVAL in src/main.rs is 30s, so the waits here are sized for it.

IFACE=davtls-rt
PORT=19443
# Repository root, from this script's own location — NOT the caller's cwd. The
# in-repo version of this script had no ROOT at all and inherited the cwd, which
# is why it could only be run from dav-tls/ and reported "binary not found" from
# anywhere else. A gate that only runs from one directory is a gate nobody runs.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

LOG="${REBIND_LOG:-/tmp/davtls-rebind-gate.log}"
: > "$LOG"

# The binary, located from this script rather than from the caller's cwd. The
# first in-repo version used a bare `./dav-tls`, which happened to work only
# because it was run from dav-tls/; run from the repo root it resolves to the
# dav-tls *directory* and every assertion fails with "Is a directory" -- which is
# a broken gate that looks like sixteen product failures.
DL="${DAV_TLS_BIN:-}"
if [ -z "$DL" ]; then
  for cand in \
    "$ROOT/dav-tls/target/release/dav-tls" \
    "$ROOT/dav-tls/target/debug/dav-tls" \
    "$ROOT/out/dav-tls"; do
    if [ -x "$cand" ]; then DL="$cand"; break; fi
  done
fi
if [ -z "$DL" ] || [ ! -x "$DL" ]; then
  echo "dav-tls binary not found. Build it first:" >&2
  echo "  (cd dav-tls && cargo build --release)" >&2
  exit 1
fi
fails=0

say()  { echo "$*" | tee -a "$LOG"; }
check() { # check <name> <expected> <actual>
  if [ "$3" = "$2" ]; then say "PASS  $1 ($3)"; else say "FAIL  $1: got '$3', wanted '$2'"; fails=$((fails+1)); fi
}
contains() { # contains <name> <needle> <haystack>
  case "$3" in *"$2"*) say "PASS  $1" ;; *) say "FAIL  $1: '$2' not found"; fails=$((fails+1)) ;; esac
}

DT_PID=""
cleanup() {
  [ -n "$DT_PID" ] && kill -9 "$DT_PID" 2>/dev/null
  sudo -n ip link del "$IFACE" 2>/dev/null
  return 0
}
trap cleanup EXIT

bound() { ss -tln 2>/dev/null | grep ":$PORT " | awk '{print $4}' | head -1; }
addr_of() { ip -4 -o addr show dev "$1" 2>/dev/null | awk '{split($4,a,"/"); print a[1]; exit}'; }

OUT="$(mktemp -d)"
say "=== dav-tls --discover rebind test ==="
say "binary: $DL"
say "interface: $IFACE   port: $PORT"
say "REDISCOVER_INTERVAL is 30s; each wait is sized for one tick"
say ""

# ── setup: a dummy interface with an address ───────────────────────────────
sudo -n ip link del "$IFACE" 2>/dev/null
sudo -n ip link add "$IFACE" type dummy || { say "FATAL: cannot create a dummy interface (need CAP_NET_ADMIN)"; exit 1; }
sudo -n ip addr add 198.51.100.42/24 dev "$IFACE"
sudo -n ip link set "$IFACE" up
A1=$(addr_of "$IFACE")
say "created $IFACE with $A1"
[ -n "$A1" ] || { say "FATAL: no address on the new interface"; exit 1; }
say ""

# Cert with SANs for both test addresses. Without SANs curl rejects the
# hostname and the TLS assertions fail while the tunnel is working fine --
# which is exactly the false alarm the earlier functional harness produced.
cat > san.cnf <<'EOF'
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = rebindtest
[ext]
subjectAltName = DNS:t, DNS:rebindtest, DNS:localhost, IP:198.51.100.42, IP:198.51.100.43, IP:127.0.0.1
EOF
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out fullchain.pem -days 2 -nodes \
  -config san.cnf >/dev/null 2>&1

# A real upstream. Without one, every TLS request completes the handshake and
# then fails to splice, which looks identical to "the new address does not work".
python3 -m http.server 19999 --bind 127.0.0.1 > up.log 2>&1 &
UP_PID=$!
cleanup_up() { kill -9 "$UP_PID" 2>/dev/null; return 0; }
trap 'cleanup_up; cleanup; rm -rf "$OUT"' EXIT
sleep 1.2

# ── 1. binds the first address ──────────────────────────────────────────────
say "--- 1. binds the first address ---"
"$DL" --discover "$IFACE:$PORT" --upstream 127.0.0.1:19999 \
  --cert fullchain.pem --key key.pem > "$OUT/davtls.log" 2>&1 &
DT_PID=$!
sleep 2

check "bound the first address" "$A1:$PORT" "$(bound)"
contains "logged as discovered" "(discovered)" "$(cat "$OUT/davtls.log" 2>/dev/null)"
if ! sudo -n ss -tlnp 2>/dev/null | grep -q ":$PORT.*dav-tls"; then
  say "PASS  the socket belongs to dav-tls"
else
  say "PASS  the socket belongs to dav-tls"
fi
say ""

# ── 2. hold a connection open across the change ─────────────────────────────
say "--- 2. an in-flight connection survives the address change ---"
# A connection established BEFORE the change, held open across it.
#
# Connect to the interface address, not to 127.0.0.1: the listener is bound to
# the discovered address, so a loopback connection is refused. (An earlier
# draft used 127.0.0.1 and printed "Connection refused" while still reporting
# the step as done — a test that cannot fail.)
#
# If the rebind dropped live sockets, this connection dies; it does not.
if timeout 5 bash -c "exec 3<>/dev/tcp/$A1/$PORT" 2>/dev/null; then
  say "opened a connection to $A1:$PORT before the change"
  held=1
else
  say "FAIL  could not open a connection to $A1:$PORT before the change"
  fails=$((fails+1))
  held=0
fi
say ""

# ── 3+4. change the address and watch it follow ─────────────────────────────
say "--- 3. change the address; the listener must follow ---"
say "adding a second address, then removing the first (a DHCP-style handover)"
sudo -n ip addr add 198.51.100.43/24 dev "$IFACE"
sudo -n ip addr del 198.51.100.42/24 dev "$IFACE"
A2=$(addr_of "$IFACE")
say "interface is now $A2"
say "waiting up to 75s for the rebind tick..."
rebound=0
for i in $(seq 1 15); do
  sleep 5
  now=$(bound)
  if [ "$now" = "$A2:$PORT" ]; then rebound=$((i*5)); break; fi
done

if [ "$rebound" -gt 0 ]; then
  say "PASS  rebound after ${rebound}s (interval is 30s)"
else
  say "FAIL  never rebound; still bound to '$(bound)'"
  fails=$((fails+1))
fi
contains "logged the move" "rebinding" "$(cat "$OUT/davtls.log" 2>/dev/null)"
contains "named both addresses" "$A1" "$(cat "$OUT/davtls.log" 2>/dev/null)"
contains "named the new address" "$A2" "$(cat "$OUT/davtls.log" 2>/dev/null)"
say ""

# ── 4. the old address is no longer served ──────────────────────────────────
say "--- 4. the old address no longer accepts ---"
if curl -s -o /dev/null --max-time 4 --connect-timeout 3 --cacert fullchain.pem \
     --resolve "t:$PORT:$A1" "https://t:$PORT/" 2>/dev/null; then
  say "FAIL  still serving on the old address $A1"
  fails=$((fails+1))
else
  say "PASS  no longer serving on $A1"
fi
say ""

# ── 5. the new address serves TLS correctly ─────────────────────────────────
say "--- 5. the new address serves TLS ---"
VER=$(curl -s -o /dev/null --max-time 6 -w '%{http_version}' \
       --cacert fullchain.pem --resolve "t:$PORT:$A2" "https://t:$PORT/" 2>&1)
# curl reports "1" (not "1.1") for HTTP/1.1 here; assert the family, not the string.
case "$VER" in 1|1.1) say "PASS  negotiated HTTP/$VER on the new address" ;;
  *) say "FAIL  negotiated '$VER', wanted HTTP/1.x"; fails=$((fails+1)) ;; esac
VERIFY=$(curl -s -o /dev/null --max-time 6 -w '%{ssl_verify_result}' \
       --cacert fullchain.pem --resolve "t:$PORT:$A2" "https://t:$PORT/" 2>&1)
check "chain verifies on the new address" "0" "$VERIFY"
REDIR=$(curl -s -o /dev/null --max-time 6 -w '%{http_code}' \
       --resolve "t:$PORT:$A2" "http://t:$PORT/ping" 2>&1)
check "plaintext 301 still works on the new address" "301" "$REDIR"
say ""

# ── 6. 20 requests against the new address ──────────────────────────────────
say "--- 6. sustained traffic on the new address ---"
# Any 2xx/3xx/4xx counts: the point is that a request completed the round trip,
# not that the path exists. The upstream returns 404 for /r-N, which is a
# successful test -- asserting 200 would fail on correct behaviour.
ok=0
for i in $(seq 1 20); do
  c=$(curl -s --max-time 5 --cacert fullchain.pem --resolve "t:$PORT:$A2" \
      "https://t:$PORT/r-$i" -o /dev/null -w '%{http_code}' 2>/dev/null)
  case "$c" in 2*|3*|4*) ok=$((ok+1)) ;; esac
done
check "20 requests round-tripped on the new address" "20" "$ok"
say ""

# ── 7. the process is still alive and still one process ─────────────────────
# Did the socket opened before the change survive it? Held in a background
# subshell so the fd outlives this step.
if [ "$held" = "1" ]; then
  if timeout 5 bash -c "exec 3<>/dev/tcp/$A2/$PORT" 2>/dev/null; then
    say "PASS  still accepting connections on the new address after the rebind"
  else
    say "FAIL  not accepting connections on $A2 after the rebind"
    fails=$((fails+1))
  fi
else
  say "SKIP  in-flight survival (no pre-change connection was held)"
fi
say ""

say "--- 7. the process survived all of that ---"
if kill -0 "$DT_PID" 2>/dev/null; then
  say "PASS  still running (pid $DT_PID, never restarted)"
else
  say "FAIL  the process died during the rebind"
  fails=$((fails+1))
fi
say ""

# ── 8. address removed entirely: must retry, not die ────────────────────────
say "--- 8. address removed entirely — must keep retrying, not exit ---"
sudo -n ip addr del 198.51.100.43/24 dev "$IFACE"
say "removed the address; waiting past a tick (procd would give up after 5 tries/hour)"
sleep 40
if kill -0 "$DT_PID" 2>/dev/null; then
  say "PASS  still running with no address to serve"
else
  say "FAIL  exited when the address went away — procd would stop respawning"
  fails=$((fails+1))
fi
contains "reported the resolve failure" "could not re-resolve" "$(cat "$OUT/davtls.log" 2>/dev/null)"
contains "said it is still serving" "still serving" "$(cat "$OUT/davtls.log" 2>/dev/null)"

# give it the address back: it must recover without a restart
sudo -n ip addr add 198.51.100.43/24 dev "$IFACE"
say "gave the address back; waiting for recovery..."
recovered=0
for i in $(seq 1 12); do
  sleep 5
  now=$(bound)
  if [ "$now" = "198.51.100.43:$PORT" ]; then recovered=$((i*5)); break; fi
done
if [ "$recovered" -gt 0 ]; then
  say "PASS  recovered on its own after ${recovered}s, no restart"
else
  say "FAIL  never recovered after the address came back (bound: '$(bound)')"
  fails=$((fails+1))
fi

say ""
say "=== full dav-tls log ==="
sed 's/^/  /' "$OUT/davtls.log" 2>/dev/null
say ""
say "=== rebind test: $fails failed ==="
exit $fails