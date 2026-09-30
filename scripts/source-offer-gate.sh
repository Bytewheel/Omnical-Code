#!/usr/bin/env bash
# source-offer-gate.sh — §10.3's half that a unit test cannot do.
#
#   scripts/source-offer-gate.sh <rustical-binary> <source-tarball>
#
# §10.3: *"on a running hosted instance, an anonymous client can reach
# `/frontend/source`, download a tarball whose `git rev-parse HEAD` matches the
# running binary's build, and the tarball builds. Test this in CI, not by eye."*
#
# `tests/source_offer.rs` covers the part that can be unit-tested: that the page
# refuses to publish a commit it does not have, and withholds an unverifiable
# tarball link. This covers the rest, and it needs two real inputs:
#
#   1. the **release binary**, which has a commit baked in by `build.rs`
#   2. the **published tarball**, which is the source that binary was built from
#
# The check is one comparison: the commit the binary reports must be the commit
# in the tarball. That single comparison is the whole of §13 compliance, and it
# is why this is a CI job rather than a paragraph — a page that renders
# correctly and points at the wrong tree is a compliance failure that looks like
# a working feature.
#
# The third clause is also checked: the tarball must **build**. An offer of
# source that does not compile is not an offer.

set -euo pipefail

BIN="${1:?usage: source-offer-gate.sh <rustical-binary> <source-tarball>}"
TARBALL="${2:?usage: source-offer-gate.sh <rustical-binary> <source-tarball>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

[ -x "$BIN" ] || { echo "::error::not executable: $BIN"; exit 1; }
[ -f "$TARBALL" ] || { echo "::error::no such tarball: $TARBALL"; exit 1; }

fails=0
pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fails=$((fails + 1)); }
section() { printf '\n\033[1m%s\033[0m\n' "$1"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── 1. the tarball is a tarball ──────────────────────────────────────────────
section "1. the published artefact is a real tarball"
# A digest fetched from the same host as the tarball is not a check of the
# tarball, and this gate is where that mistake would be made. `file` first, then
# actually list it.
if file "$TARBALL" | grep -qiE 'gzip compressed data'; then
	pass "gzip-compressed"
else
	fail "not gzip: $(file -b "$TARBALL")"
fi
if tar tzf "$TARBALL" >/dev/null 2>&1; then
	pass "lists ($(tar tzf "$TARBALL" | wc -l) entries)"
else
	fail "does not list — this is not a tarball, and CI would be checking nothing"
	exit 1
fi

# ── 2. the commit in the tarball ────────────────────────────────────────────
section "2. the tarball's own commit"
tar xzf "$TARBALL" -C "$WORK"
SRC="$(find "$WORK" -maxdepth 2 -name Cargo.toml -printf '%h\n' | head -1)"
[ -n "$SRC" ] || { fail "no Cargo.toml in the tarball"; exit 1; }
pass "unpacked to ${SRC#"$WORK"/}"

if [ -d "$SRC/.git" ]; then
	TARBALL_SHA="$(git -C "$SRC" rev-parse HEAD 2>/dev/null || echo "")"
	pass "tarball carries a .git, rev-parse HEAD = ${TARBALL_SHA:0:12}"
else
	# A *published* tarball normally has no .git — `git archive` strips it. Then
	# the only honest way to get the commit is from the archive's directory name,
	# which GitHub names after the ref, or from `cargo metadata` in a checkout.
	# A tarball with neither cannot be checked, and saying so is the point.
	TARBALL_SHA=""
	fail "no .git in the tarball and no way to name its commit — §10.3 cannot be verified"
fi

# ── 3. the binary's commit, and the comparison ───────────────────────────────
section "3. the running binary's commit (this is the whole of §13 compliance)"
# `rustical --version` does not print the commit, so the gate reads it the way a
# reader would: from the page. That is deliberate — if the page and the build
# constant could disagree, this gate would be checking the build constant and
# miss the disagreement, which is the bug the unit test is for.
# `awk '{print $2}'` rather than `cut -d' ' -f2`: the header line is
# `commit:     <sha>` with a *run* of spaces for alignment, and `cut` on a
# single-space delimiter returns an empty field for every space after the first.
# The first version of this gate read that empty string and reported "the binary
# does not report a commit" — on a binary that plainly reported one.
BIN_SHA="$("$BIN" support-bundle --stdout --no-config 2>/dev/null |
	grep -E '^commit:' | awk '{print $2}' | head -1 || true)"
if [ -n "$BIN_SHA" ]; then
	pass "the binary reports commit ${BIN_SHA:0:12}"
else
	fail "the binary does not report a commit — add it to the support bundle's header"
	exit 1
fi

if [ "$BIN_SHA" = "$TARBALL_SHA" ]; then
	pass "MATCH: the running binary was built from the source being offered"
else
	fail "MISMATCH: the binary is ${BIN_SHA:0:12} but the tarball is ${TARBALL_SHA:0:12}"
	echo
	echo "    This is an AGPL §13 compliance failure, not a packaging bug."
	echo "    §10.3: the tarball's 'git rev-parse HEAD' must match the running"
	echo "    binary's build, because the offer is of the source *of that version*."
	echo
	echo "    A page that renders correctly and points at the wrong tree is exactly"
	echo "    the failure this gate exists to catch, and it is invisible by eye."
fi

# ── 4. the offered source builds ────────────────────────────────────────────
section "4. the offered source builds"
# "Can be downloaded" is not "can be used". An offer of source that does not
# compile is not an offer, and this is cheap relative to the failure it prevents.
if cargo build --manifest-path "$SRC/Cargo.toml" --release -q >/dev/null 2>&1; then
	pass "cargo build --release succeeds on the offered tree"
else
	fail "the offered source does not build — §10.1 promises the complete corresponding source"
	cargo build --manifest-path "$SRC/Cargo.toml" --release 2>&1 | tail -20 | sed 's/^/      /'
fi

# ── 5. what this gate does not check ────────────────────────────────────────
section "5. what this gate does NOT check"
cat <<'NOTE'
  · that the page is reachable from the internet. It is unauthenticated and
    mounted on every host, which the unit tests assert; reaching it is a DNS
    and firewall question that row 38 already owns and this gate does not
    duplicate.

  · that no *customer-specific* fork exists. §10.2.1 makes that a process rule
    ("any fix made for a hosted customer is pushed to the public fork"), and no
    gate can check what was never pushed.
NOTE

echo
if [ "$fails" -eq 0 ]; then
	echo "source-offer-gate: all checks passed"
	exit 0
fi
echo "source-offer-gate: $fails FAILED"
exit 1
