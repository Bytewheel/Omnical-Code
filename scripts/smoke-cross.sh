#!/usr/bin/env bash
# smoke-cross.sh — prove a cross-built binary actually runs on its target ISA.
#
#   scripts/smoke-cross.sh <target>
#   scripts/smoke-cross.sh armv7-unknown-linux-musleabihf
#
# Why this exists: `cargo test` runs the *host* build, so CI never exercises the
# aarch64 router binary at all, let alone a second target. A cross build can
# compile, link, pack and pass a size gate while being subtly broken — wrong
# ABI flags, a libatomic shim that was never actually resolved, an unusable
# musl syscall path. Those failures only show up when the ELF is executed.
#
# So this runs the artifact under qemu and, for the device targets, drives a
# real command against a scratch SQLite database — which is what exercises the
# bundled libsqlite3-sys, the single riskiest native dependency in the build.
#
# Nothing here touches a real database or a real router: the scratch dir is
# under the system temp dir and is removed on exit.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
OUT="$ROOT/out"
TARGET="${1:-}"
[ -n "$TARGET" ] || { echo "usage: $0 <target>" >&2; exit 2; }

BIN="$OUT/$TARGET"
[ "$TARGET" = "aarch64-unknown-linux-musl" ] && BIN="$OUT"

fails=0
pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fails=$((fails + 1)); }

[ -x "$BIN/rustical" ] || { echo "!! no rustical for $TARGET at $BIN — build it first" >&2; exit 1; }
[ -x "$BIN/dav-tls" ] || { echo "!! no dav-tls for $TARGET at $BIN" >&2; exit 1; }

# Which qemu user-mode binary runs this ISA?
case "$TARGET" in
aarch64-*) QEMU=qemu-aarch64; EXPECT_MACHINE="ARM aarch64"; EXPECT_BITS="64-bit" ;;
armv7-*) QEMU=qemu-arm; EXPECT_MACHINE="ARM"; EXPECT_BITS="32-bit" ;;
x86_64-*) QEMU=""; EXPECT_MACHINE="x86-64"; EXPECT_BITS="64-bit" ;;
*) echo "!! unknown target '$TARGET'" >&2; exit 2 ;;
esac

echo "==> $TARGET"

for bin in rustical dav-tls; do
	desc="$(file -b "$BIN/$bin")"
	case "$desc" in
	*"statically linked"*) pass "$bin is statically linked" ;;
	*) fail "$bin is not statically linked: $desc" ;;
	esac
	case "$desc" in
	*"$EXPECT_BITS"*"$EXPECT_MACHINE"*) pass "$bin is $EXPECT_BITS $EXPECT_MACHINE" ;;
	*) fail "$bin is not $EXPECT_BITS $EXPECT_MACHINE: $desc" ;;
	esac
done

# A dynamic or unsatisfied-symbol binary can pass `file` and still be broken.
if command -v upx >/dev/null 2>&1; then
	tmp="$(mktemp -d)"
	trap 'rm -rf "$tmp"' EXIT
	if upx -d -o "$tmp/r" "$BIN/rustical" >/dev/null 2>&1; then
		undef=$(nm -u "$tmp/r" 2>/dev/null | wc -l)
		if [ "$undef" -eq 0 ]; then
			pass "rustical has no undefined symbols"
		else
			fail "rustical has $undef undefined symbols (first: $(nm -u "$tmp/r" | head -1))"
		fi
	fi
else
	echo "  (upx absent — skipping undefined-symbol check)"
fi

if [ -n "$QEMU" ]; then
	command -v "$QEMU" >/dev/null || {
		echo "!! $QEMU not installed — cannot execute $TARGET" >&2
		exit 1
	}

	ver="$("$QEMU" "$BIN/rustical" --version 2>&1 | head -1)"
	case "$ver" in
	"rustical "*) pass "runs under $QEMU: $ver" ;;
	*) fail "$QEMU run failed: $ver" ;;
	esac

	# The part that matters: bundled SQLite, migrations, and the argument
	# parser, all on the target ISA. gen-config writes a config we point at a
	# throwaway database, so nothing here can touch a real one.
	scratch="$(mktemp -d)"
	trap 'rm -rf "$tmp" "$scratch"' EXIT
	"$QEMU" "$BIN/rustical" gen-config >"$scratch/config.toml" 2>/dev/null
	sed -i "s|^db_url = .*|db_url = \"$scratch/db.sqlite3\"|" "$scratch/config.toml"

	if "$QEMU" "$BIN/rustical" --config-file "$scratch/config.toml" \
		principals list >/dev/null 2>&1; then
		tables=$(sqlite3 "$scratch/db.sqlite3" \
			"select count(*) from sqlite_master where type='table';" 2>/dev/null || echo 0)
		if [ "${tables:-0}" -ge 20 ]; then
			pass "migrated the store under emulation ($tables tables)"
		else
			fail "store migration produced only $tables tables"
		fi
	else
		fail "principals list failed under $QEMU"
	fi
else
	"$BIN/rustical" --version >/dev/null 2>&1 &&
		pass "runs natively: $("$BIN/rustical" --version 2>&1 | head -1)" ||
		fail "native run failed"
fi

[ "$fails" -eq 0 ] || exit 1
echo "==> $TARGET OK"
