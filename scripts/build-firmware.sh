#!/usr/bin/env bash
# build-firmware.sh — build the appliance sysupgrade image (PLAN §9.2, item 12).
#
#   scripts/build-firmware.sh [--staged-only] <path-to-ImageBuilder-dir>
#
# Two phases, and the first one is useful without an OpenWrt SDK:
#
#   1. STAGE.  Collect the payload into packaging/firmware/omnical/files/ so the
#      package recipe has something to install, and check the overlay budget.
#      This needs only the aarch64 binaries, so it runs anywhere.
#   2. IMAGE.  Drive the OpenWrt ImageBuilder to produce a sysupgrade image.
#      This needs the SDK, and it is the only step that cannot be faked.
#
# The staged inputs are deliberately NOT committed: they are build outputs
# (a 4.85 MiB binary does not belong in git), and .gitignore covers them. The
# committed tree holds the recipe, the postinst, the keep list and this script.
#
# Why an image at all: the README's sysupgrade runbook records that a
# `sysupgrade` wipes the binaries, both init scripts and the watchdog, so every
# firmware update currently needs a laptop, SSH, the `pass` secret store and a
# re-run of deploy.sh. A retail unit has none of those. An image makes the box
# re-provision itself, and the postinst is what does it.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FILES="$ROOT/packaging/firmware/omnical/files"
STAGED_ONLY=0
IMAGEBUILDER=""

for a in "$@"; do
	case "$a" in
		--staged-only) STAGED_ONLY=1 ;;
		-*) echo "unknown flag: $a" >&2; exit 2 ;;
		*) IMAGEBUILDER="$a" ;;
	esac
done

RUSTICAL_BIN="${RUSTICAL_BIN:-$ROOT/out/rustical}"
DAVTLS_BIN="${DAVTLS_BIN:-$ROOT/out/dav-tls}"
# §9.5: the 35 MiB overlay budget.  build-rust.sh enforces the stripped size;
# this is the *combined* check, because the overlay pays for both binaries plus
# the watchdog, and the watchdog is what people forget.
OVERLAY_BUDGET=$((35 * 1024 * 1024))

die() { echo "!! $*" >&2; exit 1; }
step() { echo ">>> $*"; }

# ── phase 1: stage ───────────────────────────────────────────────────────────
step "staging the payload into packaging/firmware/omnical/files"
for f in "$RUSTICAL_BIN" "$DAVTLS_BIN"; do
	[ -f "$f" ] || die "missing $f — run scripts/build-rust.sh aarch64-unknown-linux-musl first"
done

mkdir -p "$FILES/init.d"
cp "$RUSTICAL_BIN" "$FILES/rustical"
cp "$DAVTLS_BIN"  "$FILES/dav-tls"
cp "$ROOT/router/usr/bin/rustical-watchdog" "$FILES/rustical-watchdog"
cp "$ROOT/router/etc/init.d/rustical" "$FILES/init.d/rustical"
cp "$ROOT/router/etc/init.d/dav-tls"  "$FILES/init.d/dav-tls"
cp "$ROOT/router/etc/rustical/config.toml" "$FILES/config.toml"
chmod 755 "$FILES/rustical" "$FILES/dav-tls" "$FILES/rustical-watchdog" \
	"$FILES/init.d/rustical" "$FILES/init.d/dav-tls"

total=0
for f in rustical dav-tls rustical-watchdog init.d/rustical init.d/dav-tls config.toml; do
	# Busybox has no stat (constraint D5) — and neither should a build script
	# that runs on a build host pretending to be the target.
	bytes=$(wc -c < "$FILES/$f")
	total=$((total + bytes))
	printf '    %-22s %8d bytes\n' "$f" "$bytes"
done
printf '    %-22s %8d bytes of %d budget\n' "TOTAL" "$total" "$OVERLAY_BUDGET"
[ "$total" -le "$OVERLAY_BUDGET" ] ||
	die "payload is $((total / 1024 / 1024)) MiB, over the $((OVERLAY_BUDGET / 1024 / 1024)) MiB overlay budget (§9.5)"

# A packaged init script that is not executable is the single most likely way
# this ships broken: procd would not start it, and the unit would come up with
# a preserved database and no server.
for f in init.d/rustical init.d/dav-tls rustical rustical-watchdog; do
	[ -x "$FILES/$f" ] || die "$f is not executable in the staged payload"
done

step "staged"
[ "$STAGED_ONLY" = 1 ] && { echo "(--staged-only: not building an image)"; exit 0; }

# ── phase 2: image ───────────────────────────────────────────────────────────
[ -n "$IMAGEBUILDER" ] || die "pass the OpenWrt ImageBuilder directory, or --staged-only"
[ -d "$IMAGEBUILDER" ] || die "no such ImageBuilder directory: $IMAGEBUILDER"
[ -x "$IMAGEBUILDER/scripts/imagebuilder" ] || die "$IMAGEBUILDER does not look like an ImageBuilder"

VERSION="$(grep -m1 -oE 'rustical [0-9]+\.[0-9]+\.[0-9]+' "$ROOT/router/etc/rustical/config.toml" 2>/dev/null |
	grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo 0.0.0)"
step "building the sysupgrade image (omnical $VERSION)"

# The package tree has to sit inside the ImageBuilder dir, because ImageBuilder
# builds from its own feed/ tree and will not follow a symlink out of it.
PKGDIR="$IMAGEBUILDER/package/omnical"
mkdir -p "$PKGDIR"
cp -r "$ROOT/packaging/firmware/omnical/." "$PKGDIR/"

cd "$IMAGEBUILDER"
OMNICAL_VERSION="$VERSION" make image \
	PROFILE="omnical" \
	PACKAGES="omnical sqlite3-cli" \
	V=s

step "done"
echo "    image: $IMAGEBUILDER/bin/targets/*/openwrt-*-sysupgrade.bin"
echo
echo "    Flashing it must NOT need a laptop:"
echo "      1. the sysupgrade.conf lines (/etc/rustical, /usr/local/share/rustical)"
echo "         are re-asserted by the postinst on every install"
echo "      2. the init scripts are package-owned, so a flash restores them"
echo "      3. omnical.keep.d makes an *in-place* sysupgrade preserve them too"
echo
echo "    Gate §12 row 46/48 still needs a flashed unit — see"
echo "    scripts/firmware-gate.sh for what is provable without one."
