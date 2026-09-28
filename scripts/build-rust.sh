#!/usr/bin/env bash
# build-rust.sh — Cross-compile rustical + dav-tls as static binaries.
#
# Usage:
#   scripts/build-rust.sh                       # aarch64-unknown-linux-musl (router)
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

case "$TARGET" in
	aarch64-unknown-linux-musl)
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
			export CC_aarch64_unknown_linux_musl="$BIN/zig-musl-cc"
			export CXX_aarch64_unknown_linux_musl="$BIN/zig-musl-cc"
			export AR="$BIN/zig-musl-ar"
			export AR_aarch64_unknown_linux_musl="$BIN/zig-musl-ar"
			export RANLIB="$BIN/zig-musl-ranlib"
			export RANLIB_aarch64_unknown_linux_musl="$BIN/zig-musl-ranlib"
			export NM="$BIN/zig-musl-nm"
			# rust-lld (rust's bundled lld) for the final link, using rust's
			# self-contained musl libc/crt — zig's own ELF linker duplicates
			# rustc's crt and rejects rustc's newer default flags.  This is
			# the same link recipe as upstream rustical's Dockerfile.
			export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_MUSL_LINKER=rust-lld
			export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_MUSL_RUSTFLAGS="-Clink-self-contained=yes -Clinker=rust-lld"
			;;
		clang)
			# clang 22 as CC with zig's musl include dirs (clang has no musl
			# sysroot on this host — zig's bundled musl headers replace it),
			# rust-lld as linker with rust's self-contained musl CRT
			# (upstream rustical Dockerfile link recipe).  Validated
			# 2026-09-04: clang objects carry no empty-name symbols, so
			# strict rust-lld accepts the final link.
			ZIG_MUSL_INC="$(zig libc -target aarch64-linux-musl -includes)"
			[ -n "$ZIG_MUSL_INC" ] || { echo "!! 'zig libc -includes' failed" >&2; exit 1; }
			# -nostdinc also drops clang's builtin/resource includes (arm_neon.h,
			# stddef.h, … needed by aws-lc), so re-add the resource dir first.
			CLANG_RESOURCE_INC="$(clang -print-resource-dir)/include"
			ZIG_MUSL_CFLAGS="-nostdinc -isystem $CLANG_RESOURCE_INC"
			while IFS= read -r d; do
				ZIG_MUSL_CFLAGS="$ZIG_MUSL_CFLAGS -isystem $d"
			done <<< "$ZIG_MUSL_INC"
			export CC_aarch64_unknown_linux_musl=clang
			export CXX_aarch64_unknown_linux_musl=clang++
			export AR_aarch64_unknown_linux_musl=llvm-ar
			export RANLIB_aarch64_unknown_linux_musl=llvm-ranlib
			export NM_aarch64_unknown_linux_musl=llvm-nm
			export CFLAGS_aarch64_unknown_linux_musl="$ZIG_MUSL_CFLAGS"
			export CXXFLAGS_aarch64_unknown_linux_musl="$ZIG_MUSL_CFLAGS"
			export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_MUSL_RUSTFLAGS="-Clink-self-contained=yes -Clinker=rust-lld"
			;;
		*)
			echo "!! unknown TOOLCHAIN '$TOOLCHAIN' (zig|clang)" >&2
			exit 1
			;;
		esac
		;;
	x86_64-unknown-linux-gnu)
		:  # host build: system toolchain, no cross env needed
		;;
	*)
		echo "!! unsupported target '$TARGET'" >&2
		exit 1
		;;
esac

mkdir -p "$OUT"

# Binaries are placed directly in out/ for the router target, and in
# out/<target>/ for other targets (host smoke-test builds).
collect() {
	local bin="$1"
	local src="$CARGO_TARGET_DIR/$TARGET/release/$bin"
	local dest="$OUT/$bin"
	[ "$TARGET" = "aarch64-unknown-linux-musl" ] || dest="$OUT/$TARGET/$bin"
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
		local_bin="$OUT/$bin"
		[ "$TARGET" = "aarch64-unknown-linux-musl" ] || local_bin="$OUT/$TARGET/$bin"
		"$LLVM_STRIP" --remove-section=.comment "$local_bin" 2>/dev/null || true
	done
fi

# UPX LZMA compression (post-build, ~40-60% additional size reduction)
if command -v upx >/dev/null 2>&1; then
	echo ">>> Compressing binaries with UPX --lzma"
	for bin in rustical dav-tls; do
		local_bin="$OUT/$bin"
		[ "$TARGET" = "aarch64-unknown-linux-musl" ] || local_bin="$OUT/$TARGET/$bin"
		upx --lzma "$local_bin" 2>/dev/null || true
	done
fi

if [ "$TARGET" = "aarch64-unknown-linux-musl" ]; then
	echo ">>> Size gate (rustical <= 35 MiB stripped, overlay budget C2)"
	bytes=$(stat -c%s "$OUT/rustical")
	if [ "$bytes" -gt "$RUSTICAL_BUDGET" ]; then
		echo "!! rustical is $((bytes / 1024 / 1024)) MiB — over the 35 MiB budget." >&2
		echo "   Next steps: CARGO_PROFILE_RELEASE_OPT_LEVEL=z LTO/panic=abort, then upx --lzma." >&2
		exit 1
	fi
	echo "    rustical fits: $((bytes / 1024 / 1024)) MiB of 35 MiB budget"
fi

echo ">>> Done. Binaries in $OUT"
