#!/usr/bin/env bash
# build-rust.sh — Cross-compile rustical + dav-tls as static binaries.
#
# Usage:
#   scripts/build-rust.sh                       # aarch64-unknown-linux-musl (router)
#   scripts/build-rust.sh armv7-unknown-linux-musleabihf   # armv7 (ipq806x class)
#   scripts/build-rust.sh x86_64-unknown-linux-gnu   # host build for smoke tests
#   TOOLCHAIN=zig scripts/build-rust.sh         # legacy zig-CC route (broken at link)
#
# C dependencies of rustical 0.16.1 (all statically linked in):
#   - libsqlite3-sys  (bundled SQLite, via sqlx)
#   - openssl-src     (vendored OpenSSL, via dav_push's ece/web-push)
#   - aws-lc-sys      (rustls crypto provider, via reqwest/oidc/web-push)
#
# Default toolchain (clang, 2026-09-04): clang 22 as CC with the musl include
# dirs taken from zig 0.16 (`zig libc -target aarch64-linux-musl -includes`),
# linked with rust-lld + rust's self-contained musl CRT (upstream rustical's
# own Dockerfile recipe).  This is the only combination found working:
#   - zig-CC objects carry empty-name undefined SECTION symbols (e.g. inside
#     sqlite3.o) which strict lld rejects at the final link;
#   - plain clang has no musl sysroot on this host and falls back to glibc
#     headers (aws-lc-sys: "__float128 is not supported on this target");
#   - zig's own ELF linker collides with rustc's self-contained crt1.o.
#
# The same three failure modes apply to every musl cross target below; the
# clang recipe is the one that works, so `configure_musl_cross` is shared
# rather than duplicated per target.  See docs/hardware/CROSS_COMPILE_PLAN.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
OUT="$ROOT/out"
TARGET="${1:-aarch64-unknown-linux-musl}"
TOOLCHAIN="${TOOLCHAIN:-clang}"
RUSTICAL_BUDGET=$((35 * 1024 * 1024))  # stripped size budget (plan C2)

export PATH="$HOME/.cargo/bin:$PATH"
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$ROOT/build/cargo-target}"
export SQLX_OFFLINE=true
# Never leave this unset for a build: it is exported so that a build launched
# from a shell where it was not set cannot silently write somewhere else.
export CARGO_INCREMENTAL="${CARGO_INCREMENTAL:-0}"

# The router target: its binaries land directly in out/ because that is what
# deploy.sh and build-firmware.sh consume.  Every other target goes to
# out/<target>/ so a host or secondary build can never clobber a router binary.
ROUTER_TARGET=aarch64-unknown-linux-musl

# Configure a static-musl cross toolchain for one Rust target.
#   $1  Rust target triple      (drives the CC_<triple> / CARGO_TARGET_<triple> vars)
#   $2  zig C triple            (provides the musl headers clang lacks)
configure_musl_cross() {
	local rust_target="$1"
	local zig_target="$2"
	# aarch64-unknown-linux-musl -> aarch64_unknown_linux_musl
	local suffix="${rust_target//-/_}"
	local upper="${suffix^^}"

	case "$TOOLCHAIN" in
	zig)
		BIN="$ROOT/build/bin"
		chmod +x "$BIN/zig-musl-cc"
		ln -sf zig-musl-cc "$BIN/zig-musl-ar"
		ln -sf zig-musl-cc "$BIN/zig-musl-ranlib"
		ln -sf zig-musl-cc "$BIN/zig-musl-nm"
		# zig as C compiler: it bundles complete musl headers for every
		# target, so vendored OpenSSL / aws-lc / SQLite cross-build cleanly
		# (clang alone cannot: it falls back to host glibc headers).
		export "CC_${suffix}=$BIN/zig-musl-cc"
		export "CXX_${suffix}=$BIN/zig-musl-cc"
		export AR="$BIN/zig-musl-ar"
		export "AR_${suffix}=$BIN/zig-musl-ar"
		export RANLIB="$BIN/zig-musl-ranlib"
		export "RANLIB_${suffix}=$BIN/zig-musl-ranlib"
		export NM="$BIN/zig-musl-nm"
		# rust-lld (rust's bundled lld) for the final link, using rust's
		# self-contained musl libc/crt — zig's own ELF linker duplicates
		# rustc's crt and rejects rustc's newer default flags.  This is
		# the same link recipe as upstream rustical's Dockerfile.
		export "CARGO_TARGET_${upper}_LINKER=rust-lld"
		export "CARGO_TARGET_${upper}_RUSTFLAGS=-Clink-self-contained=yes -Clinker=rust-lld"
		;;
	clang)
		# clang as CC with zig's musl include dirs (clang has no musl
		# sysroot on this host — zig's bundled musl headers replace it),
		# rust-lld as linker with rust's self-contained musl CRT
		# (upstream rustical Dockerfile link recipe).  Validated
		# 2026-09-04: clang objects carry no empty-name symbols, so
		# strict rust-lld accepts the final link.
		ZIG_MUSL_INC="$(zig libc -target "$zig_target" -includes)"
		[ -n "$ZIG_MUSL_INC" ] || { echo "!! 'zig libc -includes' failed for $zig_target" >&2; exit 1; }
		# -nostdinc also drops clang's builtin/resource includes (arm_neon.h,
		# stddef.h, … needed by aws-lc), so re-add the resource dir first.
		CLANG_RESOURCE_INC="$(clang -print-resource-dir)/include"
		ZIG_MUSL_CFLAGS="-nostdinc -isystem $CLANG_RESOURCE_INC"
		while IFS= read -r d; do
			ZIG_MUSL_CFLAGS="$ZIG_MUSL_CFLAGS -isystem $d"
		done <<< "$ZIG_MUSL_INC"
		export "CC_${suffix}=clang"
		export "CXX_${suffix}=clang++"
		export "AR_${suffix}=llvm-ar"
		export "RANLIB_${suffix}=llvm-ranlib"
		export "NM_${suffix}=llvm-nm"
		export "CFLAGS_${suffix}=$ZIG_MUSL_CFLAGS"
		export "CXXFLAGS_${suffix}=$ZIG_MUSL_CFLAGS"
		export "CARGO_TARGET_${upper}_RUSTFLAGS=-Clink-self-contained=yes -Clinker=rust-lld"
		;;
	*)
		echo "!! unknown TOOLCHAIN '$TOOLCHAIN' (zig|clang)" >&2
		exit 1
		;;
	esac
}

case "$TARGET" in
	aarch64-unknown-linux-musl)
		configure_musl_cross "$TARGET" aarch64-linux-musl
		;;
	armv7-unknown-linux-musleabihf)
		# armv7 hard-float: ipq806x / ipq40xx class (e.g. Netgear R7800,
		# whose SoC is a dual Cortex-A15).  Tier 2 with a prebuilt std, and
		# aws-lc-sys, openssl-sys, ring and bundled SQLite all cross-compile
		# for it — see docs/hardware/CROSS_COMPILE_PLAN.md §2.
		configure_musl_cross "$TARGET" arm-linux-musleabihf
		# rustc puts `-latomic` on every armv7 link line, because a 32-bit
		# ARM core cannot do 64-bit atomics natively — but its
		# self-contained musl sysroot ships no libatomic.a, so the link
		# fails with "unable to find library -latomic" before symbol
		# resolution ever happens.
		#
		# cortex-a15 has LPAE, so LLVM lowers 8-byte atomics to
		# ldrexd/strexd inline rather than calling out to __atomic_*.  That
		# leaves the library unreferenced, and an empty archive is enough
		# to satisfy the linker's search.  `-L` points at it; the `llvm-ar`
		# invocation is idempotent and creates the archive only if absent.
		ARMV7_LIBDIR="$ROOT/build/lib/armv7"
		mkdir -p "$ARMV7_LIBDIR"
		[ -f "$ARMV7_LIBDIR/libatomic.a" ] || llvm-ar crs "$ARMV7_LIBDIR/libatomic.a"
		export CARGO_TARGET_ARMV7_UNKNOWN_LINUX_MUSLEABIHF_RUSTFLAGS="$CARGO_TARGET_ARMV7_UNKNOWN_LINUX_MUSLEABIHF_RUSTFLAGS -Ctarget-cpu=cortex-a15 -Clink-arg=-L$ARMV7_LIBDIR"
		;;
	x86_64-unknown-linux-gnu)
		:  # host build: system toolchain, no cross env needed
		;;
	*)
		echo "!! unsupported target '$TARGET'" >&2
		echo "   supported: $ROUTER_TARGET, armv7-unknown-linux-musleabihf, x86_64-unknown-linux-gnu" >&2
		exit 1
		;;
esac

# Preflight: every musl target needs its rust-std installed, and the stable
# toolchain ships none for the exotic ones.  Fail with the exact command rather
# than letting cargo die deep inside a C build script.
if [ "$TARGET" != "x86_64-unknown-linux-gnu" ]; then
	if ! rustup target list --installed 2>/dev/null | grep -qx "$TARGET"; then
		echo "!! rust-std for '$TARGET' is not installed." >&2
		echo "   Next steps: rustup target add $TARGET" >&2
		exit 1
	fi
fi

mkdir -p "$OUT"

# Where a finished binary goes for the current $TARGET: out/ for the router
# target, out/<target>/ for everything else.
bin_dest() {
	local bin="$1"
	if [ "$TARGET" = "$ROUTER_TARGET" ]; then
		printf '%s/%s' "$OUT" "$bin"
	else
		printf '%s/%s/%s' "$OUT" "$TARGET" "$bin"
	fi
}

# Binaries are placed directly in out/ for the router target, and in
# out/<target>/ for other targets (host smoke-test builds, armv7).
collect() {
	local bin="$1"
	local src="$CARGO_TARGET_DIR/$TARGET/release/$bin"
	local dest; dest="$(bin_dest "$bin")"
	mkdir -p "$(dirname "$dest")"
	cp "$src" "$dest"
	chmod 755 "$dest"
	echo "  -> $dest ($(du -h "$dest" | cut -f1))"
}

echo ">>> Building rustical ($TARGET, toolchain=$TOOLCHAIN)"
(cd "$ROOT/rustical" && cargo build --release --locked --target "$TARGET")
collect rustical

echo ">>> Building dav-tls ($TARGET, toolchain=$TOOLCHAIN)"
(cd "$ROOT/dav-tls" && cargo build --release --target "$TARGET")
collect dav-tls

# Strip the .comment section (compiler version strings, ~4 KiB)
LLVM_STRIP="${LLVM_STRIP:-/usr/lib/llvm/22/bin/llvm-strip}"
if [ -x "$LLVM_STRIP" ]; then
	echo ">>> Stripping .comment section from binaries"
	for bin in rustical dav-tls; do
		"$LLVM_STRIP" --remove-section=.comment "$(bin_dest "$bin")" 2>/dev/null || true
	done
fi

# UPX LZMA compression (post-build, ~40-60% additional size reduction)
if command -v upx >/dev/null 2>&1; then
	echo ">>> Compressing binaries with UPX --lzma"
	for bin in rustical dav-tls; do
		upx --lzma "$(bin_dest "$bin")" 2>/dev/null || true
	done
fi

# The overlay budget applies to every device target, not just the router: a
# 32-bit build is the one that has to fit the smaller devices' flash.
if [ "$TARGET" != "x86_64-unknown-linux-gnu" ]; then
	echo ">>> Size gate (rustical <= 35 MiB stripped, overlay budget C2)"
	bytes=$(stat -c%s "$(bin_dest rustical)")
	if [ "$bytes" -gt "$RUSTICAL_BUDGET" ]; then
		echo "!! rustical is $((bytes / 1024 / 1024)) MiB — over the 35 MiB budget." >&2
		echo "   Next steps: CARGO_PROFILE_RELEASE_OPT_LEVEL=z LTO/panic=abort, then upx --lzma." >&2
		exit 1
	fi
	echo "    rustical fits: $((bytes / 1024 / 1024)) MiB of 35 MiB budget"
fi

echo ">>> Done. Binaries in $OUT"
