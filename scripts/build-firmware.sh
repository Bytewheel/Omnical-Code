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

# Which kind of builder is this, and does it have the layout we need?
#
# An ImageBuilder ships a *frozen* .packageinfo and .targetinfo generated at
# release time, and `make image` never re-scans package/.  So a stock
# ImageBuilder structurally cannot see a custom package or a custom device
# profile — "omnical (no such package)" and "Profile omnical does not exist"
# are not fixable by hand-editing, because nothing regenerates those indexes.
#
# An SDK does re-scan (include/toplevel.mk's prepare-tmpinfo) and regenerates
# .targetinfo from target/linux/*/image/*.mk, so it is the only one of the two
# that can carry an appliance package and profile.  Prefer it when adding them.
#
# Both layouts have Makefile + rules.mk + scripts/, so key the structural check
# on those and report the flavour separately.
if [ -f "$IMAGEBUILDER/Makefile" ] && [ -f "$IMAGEBUILDER/rules.mk" ] &&
	[ -d "$IMAGEBUILDER/scripts" ]; then
	if [ -f "$IMAGEBUILDER/Config.in" ]; then
		IB_KIND="SDK"
	else
		IB_KIND="ImageBuilder"
	fi
else
	die "$IMAGEBUILDER is neither an OpenWrt SDK nor an ImageBuilder (no Makefile+rules.mk+scripts/)"
fi
step "builder: $IB_KIND ($IMAGEBUILDER)"

# An SDK ships no .config; without one it cannot resolve the profile or the
# package list, and fails with a confusing error rather than a clear one.
if [ ! -f "$IMAGEBUILDER/.config" ]; then
	step "generating .config (make defconfig)"
	(cd "$IMAGEBUILDER" && make defconfig) ||
		die "make defconfig failed in $IMAGEBUILDER"
fi

VERSION="$(grep -m1 -oE 'rustical [0-9]+\.[0-9]+\.[0-9]+' "$ROOT/router/etc/rustical/config.toml" 2>/dev/null |
	grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo 0.0.0)"
step "building the sysupgrade image (omnical $VERSION)"

# The package tree has to sit inside the ImageBuilder dir, because ImageBuilder
# builds from its own feed/ tree and will not follow a symlink out of it.
# profile.mk is deliberately excluded: it is an *image* profile, not package
# content, and belongs in the image/ dir (below).
PKGDIR="$IMAGEBUILDER/package/omnical"
mkdir -p "$PKGDIR"
for f in "$ROOT/packaging/firmware/omnical/"*; do
	[ "$(basename "$f")" = "profile.mk" ] && continue
	cp -r "$f" "$PKGDIR/"
done

# `PROFILE=omnical` only resolves if the ImageBuilder knows a profile by that
# name, and it does not: its profiles come from target/linux/<t>/image/<sub>.mk.
# So install ours there and include it from the subtarget.  Both the target and
# the subtarget are read out of the ImageBuilder's own .config rather than
# guessed from the directory name, so a differently-named tarball still works.
IB_TARGET="$(sed -n 's/^CONFIG_TARGET_BOARD="\([^"]*\)".*/\1/p' "$IMAGEBUILDER/.config" 2>/dev/null)"
IB_SUBTARGET="$(sed -n 's/^CONFIG_TARGET_SUBTARGET="\([^"]*\)".*/\1/p' "$IMAGEBUILDER/.config" 2>/dev/null)"
[ -n "$IB_TARGET" ] && [ -n "$IB_SUBTARGET" ] ||
	die "could not read CONFIG_TARGET_BOARD/SUBTARGET from $IMAGEBUILDER/.config"

IMAGEDIR="$IMAGEBUILDER/target/linux/$IB_TARGET/image"
SUBMK="$IMAGEDIR/$IB_SUBTARGET.mk"
[ -f "$SUBMK" ] || die "no subtarget makefile at $SUBMK (wrong ImageBuilder for this profile?)"

PROFILE_SRC="$ROOT/packaging/firmware/omnical/profile.mk"
[ -f "$PROFILE_SRC" ] || die "missing appliance profile: $PROFILE_SRC"
cp "$PROFILE_SRC" "$IMAGEDIR/omnical.mk"

# Idempotent: re-running must not append the include a second time.
if ! grep -qx 'include omnical.mk' "$SUBMK"; then
	printf '\n# added by scripts/build-firmware.sh — the appliance profile\ninclude omnical.mk\n' >>"$SUBMK"
	step "installed appliance profile into $IB_TARGET/$IB_SUBTARGET (PROFILE=omnical)"
else
	step "appliance profile already installed in $IB_TARGET/$IB_SUBTARGET"
fi

cd "$IMAGEBUILDER"
# The package list lives in the profile's DEVICE_PACKAGES, not here: which
# firmware blobs an appliance needs is a property of the board, and the board
# specifics belong in the one device-specific file.
OMNICAL_VERSION="$VERSION" make image \
	PROFILE="omnical" \
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
