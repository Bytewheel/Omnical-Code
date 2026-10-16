#!/bin/sh
# clean-build-artifacts.sh — 02:45 cron on the dev machine. Build-cache GC.
#
# ## Why this exists
#
# The working tree grows build output that nothing ever removes. Measured
# 2026-10-16: rustical/target was 94 GB (93 GB of that target/debug/deps),
# build/cargo-target 1.5 GB, dav-tls/target a few hundred MB — on a root
# filesystem that was 87 % full. This exact problem has been solved by hand
# before: NEXT_AGENT_DEPLOY_PROFILE.md records a manual `cargo clean` plus a
# build/cargo-target wipe that reclaimed ~101 GB. A manual wipe is a
# decision somebody has to remember to make; this script makes it nightly
# instead, and only for directories that have gone quiet.
#
# ## What counts as "no longer needed"
#
# A build-cache directory is deleted when nothing inside it — file or
# directory — has been modified in the last DAYS days (default 14). Every
# cargo invocation rewrites fingerprints and output files, so an in-use
# target dir is continuously fresh; two weeks of silence means nothing has
# been built there and the whole directory can go.
#
# Deletion is whole-directory (`cargo clean` semantics). Partial mtime
# surgery inside target/ is what cargo-sweep exists for, and doing it by
# hand risks deleting half of a fingerprint pair; a wiped dir costs exactly
# one cold rebuild (~3.5–6 min, PLAN.md §1.3) and never any data — target/
# holds no sources, no credentials, and nothing git tracks.
#
# ## What this script must never touch
#
#   out/             live deploy inputs + LIVE credentials (out/tls/,
#                   *.mobileconfig) — NEXT_AGENT_PLAN.md "Do not".
#   rotation-drills/ copies of the production database.
#   build/bin, build/lib   the zig toolchain wrappers and libatomic.a —
#                   inputs to a build, not products of one.
#   anything tracked in git, any .git — sources and history.
#
# The candidate list is not hand-maintained guesswork: the caches that
# exist today (rustical/target, dav-tls/target, build/cargo-target) and
# the OpenWrt SDK/ImageBuilder trees (documented in .gitignore as the
# multi-GB shape that lands in build/ for firmware work) are named, and a
# find pass adds any other */target or zig-cache dir so a new crate cannot
# grow an unwatched 90 GB. Discovery prunes build/ outright: the OpenWrt
# SDK has a `target/` directory of its own that is not cargo output, so
# build/openwrt is managed only through its named entry, as one tree, by
# the same staleness rule.
#
# ## Guards
#
# Skips the run entirely if cargo or rustc is running (deleting a target
# dir mid-build fails the build out from under it; cron retries
# tomorrow). Single instance via flock. `--dry-run` (or -n) prints what
# would go and deletes nothing. DAYS=30 in the environment overrides the
# threshold.
#
# The log (build/artifact-clean.log) carries one short line per run plus
# one per deletion — paths and sizes only, nothing that could be a secret.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$ROOT/build"
LOG="$LOG_DIR/artifact-clean.log"
DAYS="${DAYS:-14}"
DRY_RUN=0

case "${1:-}" in
	--dry-run|-n) DRY_RUN=1 ;;
	"") ;;
	*) echo "usage: $0 [--dry-run]   (env override: DAYS=N)" >&2; exit 1 ;;
esac

# cron runs with a minimal PATH; pin the lookup order so the same script
# behaves the same from a shell and from crontab.
PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

# This script deletes directories derived from ROOT. Refuse to run against
# a tree that is not router-dav, so a copy of the script run from
# somewhere else cannot start deleting some other project's caches.
if [ ! -d "$ROOT/rustical" ] || [ ! -x "$ROOT/scripts/build-rust.sh" ]; then
	echo "!! $ROOT does not look like router-dav (rustical/ or scripts/build-rust.sh missing)" >&2
	exit 1
fi

mkdir -p "$LOG_DIR"

# In dry-run nothing is deleted, so skip both the lock and the log file —
# a dry run must never be blocked by (or pollute the log of) a real one.
if [ "$DRY_RUN" != 1 ]; then
	log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }
	exec 9>>"$LOG_DIR/.artifact-clean.lock"
	flock -n 9 || exit 0
else
	log() { :; }
fi

say() {
	if [ "$DRY_RUN" = 1 ]; then
		printf '%s\n' "$*"
	else
		log "$*"
	fi
}

# Never race a build: deleting a target dir under a live cargo/rustc
# fails that build. rust-analyzer spawns cargo, so editor check-on-save
# counts too. Skip — cron comes back tomorrow.
if pgrep -x cargo >/dev/null 2>&1 || pgrep -x rustc >/dev/null 2>&1; then
	say "SKIP: cargo/rustc running — build in progress"
	exit 0
fi

# A build rewrites files *inside* the cache dir, so the dir's own mtime can
# only ever prove "recently used" (cheap stat-only test). Proving staleness
# needs the full walk; `-quit` stops it at the first fresh entry, which a
# genuinely in-use dir serves up almost immediately.
CUTOFF="$(date -d "-$DAYS days" '+%F %T')"

stale() {
	if [ -n "$(find "$1" -maxdepth 0 -newermt "$CUTOFF" 2>/dev/null)" ]; then
		return 1
	fi
	if [ -n "$(find "$1" -newermt "$CUTOFF" -print -quit 2>/dev/null)" ]; then
		return 1
	fi
	return 0
}

handled='|'
checked=0
freed_kb=0

# already_handled DIR — true when an earlier gc() this run judged DIR.
# The discovery pass re-finds the named caches (they are target dirs too)
# and must not rescan a 93 GB tree it just decided to keep.
already() {
	case "$handled" in *"|$1|"*) return 0 ;; esac
	return 1
}

gc() {
	dir="$1"
	if already "$dir" || [ ! -d "$dir" ]; then
		return 0
	fi
	handled="$handled$dir|"
	checked=$((checked + 1))
	if ! stale "$dir"; then
		return 0
	fi
	kb="$(du -sk "$dir" 2>/dev/null | cut -f1)"
	kb="${kb:-0}"
	h="$(du -sh "$dir" 2>/dev/null | cut -f1)"
	h="${h:-?}"
	if [ "$DRY_RUN" = 1 ]; then
		printf 'would delete %s (%s) — idle since before %s\n' "$dir" "$h" "$CUTOFF"
	else
		rm -rf -- "$dir"
		log "deleted $dir ($h freed)"
	fi
	freed_kb=$((freed_kb + kb))
}

# 1. The named caches — the ones that exist today and the ones .gitignore
#    documents — plus the OpenWrt builder trees.
for dir in \
	"$ROOT/rustical/target" \
	"$ROOT/dav-tls/target" \
	"$ROOT/build/cargo-target" \
	"$ROOT/build/openwrt" \
; do
	gc "$dir"
done

# 2. Discovery: any other target/ or zig cache in the tree. Subtrees with
#    a different owner are pruned before the walk — .git (never enter any
#    history), out/ (live credentials), rotation-drills/ (production DB
#    copies), build/ (named entries above already own everything in it —
#    including the OpenWrt SDK's non-cargo target/ dir). Matching dirs are
#    printed and pruned, so the walk never descends into a 93 GB cache
#    while looking for more of them.
LIST="$LOG_DIR/.gc-scan.$$"
find "$ROOT" \
	\( -name .git -o -name out -o -name rotation-drills -o -name build \) -prune -o \
	\( -name target -o -name zig-cache -o -name .zig-cache \) -type d -prune -print \
	>"$LIST" 2>/dev/null || true
while IFS= read -r dir; do
	[ -n "$dir" ] && gc "$dir"
done <"$LIST"
rm -f "$LIST"

if [ "$DRY_RUN" = 1 ]; then
	printf 'dry-run done: %d dir(s) checked, %d KiB (%s GiB) would be freed\n' \
		"$checked" "$freed_kb" \
		"$(awk -v kb="$freed_kb" 'BEGIN{ printf "%.1f", kb/1048576 }')"
	exit 0
fi

if [ "$freed_kb" -gt 0 ]; then
	log "done: $checked dir(s) checked, $(awk -v kb="$freed_kb" 'BEGIN{ printf "%.1f", kb/1048576 }') GiB freed"
else
	log "done: $checked dir(s) checked, nothing stale"
fi
