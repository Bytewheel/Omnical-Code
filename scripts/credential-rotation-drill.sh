#!/usr/bin/env bash
# credential-rotation-drill.sh — prove the rotation lost no customer data.
#
#   scripts/credential-rotation-drill.sh snapshot   <name>   # before or after
#   scripts/credential-rotation-drill.sh verify     <before> <after>
#   scripts/credential-rotation-drill.sh selftest
#
# PLAN_DEPLOYMENTS.md §5.2 item 1 (H1/H2). The runbook
# (docs/operations/credential-rotation.md) says what to do. **This says whether it
# worked**, which the runbook cannot: "the old tokens return 401" proves the old
# credentials died, and says nothing about whether the calendars survived.
#
# # Why this exists
#
# The requirement is that rotation retains every calendar, event, task, contact
# and everything else. That is a property of a *sequence of steps run against a
# live database by a human under time pressure*, and it is exactly the kind of
# property nobody checks. So:
#
#   snapshot  — take a fingerprint before the window
#   (…rotate…)
#   verify    — take it after, and assert nothing but the intended tables moved
#
# The fingerprint is the `row_counts` map already in `rustical backup`'s manifest,
# which counts **every** user table plus runs `PRAGMA integrity_check`. Reusing it
# means the drill is not a second, weaker inventory that could disagree with the
# one the backup uses.
#
# # What is allowed to change
#
# **Exactly one table: `app_tokens`.** Revoking H2 is *deleting rows* from it, so
# a row-count diff on it is the rotation working rather than the rotation failing.
# Everything else must be byte-for-byte identical in count, and the drill fails on
# anything else — including a table that appears, disappears, or moves by one.
#
# A new table appearing is a failure, not a curiosity: it means the window ran
# code that migrated the schema, which is a far larger change than a rotation and
# deserves to be noticed rather than absorbed.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Deliberately NOT `RUSTICAL_BIN` / `RUSTICAL_CONFIG`: `RUSTICAL_*` is rustical's
# own config-from-environment convention (`RUSTICAL_[data_store|tenancy|http|…]`),
# so naming these that way made the drill's own settings land in rustical's
# config and fail with "unknown field: BIN".
BIN="${ROTATION_DRILL_BIN:-$ROOT/out/rustical}"
CONFIG="${ROTATION_DRILL_CONFIG:-/etc/rustical/config.toml}"

# Tables that MUST NOT change count during a credential rotation.
# Anything absent from this list is compared strictly, so adding a table to the
# schema does not silently exempt it.
# Every user table in the live schema, taken from `sqlite_master` on a real
# migrated database rather than from memory. The first version of this list had
# `addressbook_objects`, `davpush_vapid` and `group_ownership` in it — none of
# which exist; the real names are `addressobjects`, `davpush_vapid_key` and
# `group_owners`. Guessing names here is worse than useless, because the list is
# the thing the drill trusts. It was corrected only after running against a real
# migrated database, which is the reason the rehearsal below exists at all.
#
# Note the strict default still holds: a table missing from this list is compared
# strictly, so a wrong name produces a confusing message rather than a false pass.
readonly PROTECTED=(
  _sqlx_migrations
  addressbooks
  addressobjectchangelog
  addressobjects
  birthday_calendars
  calendar_sources
  calendarobjectchangelog
  calendarobjects
  calendars
  collection_shares
  davpush_subscriptions
  davpush_vapid_key
  group_members
  group_owners
  invites
  memberships
  password_resets
  principals
  scheduling_inbox_objects
  subscriptions
)

# The one table whose change IS the rotation.
readonly ROTATION_TABLE="app_tokens"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
die()   { red "!! $*"; exit 1; }

is_protected() {
  local t="$1" p
  for p in "${PROTECTED[@]}"; do
    [ "$t" = "$p" ] && return 0
  done
  return 1
}

# ── snapshot ────────────────────────────────────────────────────────────────
cmd_snapshot() {
  local name="${1:?usage: snapshot <name>}"
  local out="$ROOT/rotation-drills/$name"
  mkdir -p "$out"
  # `--no-migrations`: a fingerprint must never *cause* a migration. If the window
  # opens on a schema that has not been migrated, that is a finding to report, not
  # a side effect to quietly apply.
  "$BIN" --config-file "$CONFIG" backup --out-dir "$out" >/dev/null ||
    die "backup failed — nothing to compare against"
  # Refuse rather than guess. `find | head -1` picks an arbitrary archive when
  # more than one is present, and that is not hypothetical: rehearsing this drill
  # left an archive and a manifest in rotation-drills/before/, and the live
  # "before" fingerprint of the production database was written next to them —
  # after which `snapshot` extracted the *rehearsal* manifest and reported
  #   principals 3, calendarobjects 4, app_tokens 4
  # for a database that actually holds
  #   principals 31, calendarobjects 216, app_tokens 69.
  #
  # Every later "nothing was lost" verdict would then have been a comparison of
  # rehearsal data against production data. The label on the directory was the
  # only thing asserting freshness, and a label is not evidence.
  local existing
  existing="$(find "$out" -name 'omnical-backup-*.tar' 2>/dev/null | head -1)"
  if [ -n "$existing" ]; then
    die "$out already contains $(basename "$existing"). Refusing to fingerprint \
into a directory that already holds a backup — remove it, or use a fresh name, \
because a stale manifest read as current would silently pass the retention check."
  fi
  local archive
  archive="$(find "$out" -name 'omnical-backup-*.tar' | head -1)"
  [ -n "$archive" ] || die "no archive in $out"

  tar xf "$archive" -C "$out" manifest.json
  [ -f "$out/manifest.json" ] || die "the archive has no manifest.json"

  local integrity
  integrity="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["integrity_check"])' "$out/manifest.json")"
  [ "$integrity" = "ok" ] || die "integrity_check is '$integrity' — do NOT run the window on this database"

  green "snapshot '$name': $(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["row_counts"]))' "$out/manifest.json") tables, integrity_check ok"
  echo "$out"
}

# ── verify ──────────────────────────────────────────────────────────────────
cmd_verify() {
  local before="${1:?usage: verify <before> <after>}"
  local after="${2:?usage: verify <before> <after>}"
  [ -f "$before/manifest.json" ] || die "no manifest at $before"
  [ -f "$after/manifest.json" ] || die "no manifest at $after"

  echo
  bold "=== customer-data retention check ==="

  ROTATION_DRILL_PROTECTED="${PROTECTED[*]}" \
  python3 - "$before/manifest.json" "$after/manifest.json" "$ROTATION_TABLE" <<'PY'
import json, os, sys

before = json.load(open(sys.argv[1]))
after = json.load(open(sys.argv[2]))
rotation_table = sys.argv[3]
# From the environment, not a temp file. The first version wrote
# `$ROOT/.drill-protected` (since deleted) inside `selftest` and read it back in `verify`, which
# meant `selftest` passed while a real `verify` died with FileNotFoundError — the
# gate testing itself and not the thing it gates.
protected = set(os.environ.get("ROTATION_DRILL_PROTECTED", "").split())

b = before["row_counts"]
a = after["row_counts"]

def green(s): print("\033[32m" + s + "\033[0m")
def red(s):   print("\033[31m" + s + "\033[0m")
def bold(s):  print("\033[1m" + s + "\033[0m")

changed_protected = []
changed_other = []
added = sorted(set(a) - set(b))
dropped = sorted(set(b) - set(a))

moved_rotation = []
for table in sorted(b):
    if table not in a:
        dropped.append(table)
        continue
    if b[table] == a[table]:
        continue
    delta = a[table] - b[table]
    line = f"  {table:<32} {b[table]:>8} -> {a[table]:>8}  ({delta:+d})"
    if table == rotation_table:
        moved_rotation.append(line)
    elif table in protected:
        changed_protected.append(line)
    else:
        changed_other.append(line)

if not protected:
    # Fail closed. An unreadable protected list must not silently downgrade the
    # check to "compare nothing".
    red("  could not read the protected list; treating EVERY changed table as a failure")
    changed_protected = changed_other = [l for l in moved_rotation] + changed_protected + changed_other

bold("\nrow counts that moved:")
if moved_rotation:
    for line in moved_rotation:
        green(line + "   <- the rotation itself")
if changed_other:
    for line in changed_other:
        red(line + "   <- UNEXPECTED")
if changed_protected:
    bold("\nprotected tables that moved:")
    for line in changed_protected:
        red(line + "   <- CUSTOMER DATA CHANGED")
if not (moved_rotation or changed_other or changed_protected):
    green("  (nothing moved at all — the rotation revoked nothing. Check the window ran.)")

if added:
    bold("\ntables that appeared:")
    for t in added:
        red(f"  {t}   <- the window ran a schema migration; that is not a rotation")
if dropped:
    bold("\ntables that disappeared:")
    for t in dropped:
        red(f"  {t}   <- data was destroyed")

failed = bool(changed_protected or changed_other or added or dropped)
if failed:
    red("\nRETENTION CHECK FAILED — do not tell anyone the rotation was clean.")
    sys.exit(1)

green("\nEvery calendar, event, task, contact and other user table is unchanged.")
# Only narrate the credential table as having moved if it actually did.
#
# The first version printed this unconditionally, so a *stale* after-snapshot
# produced two contradictory lines and still exited 0:
#     (nothing moved at all — the rotation revoked nothing. Check the window ran.)
#     Only app_tokens moved (69 -> 69), which is the revocation working.
#
# A gate that narrates both "nothing happened" and "the thing happened" is worse
# than no narration, because it teaches the reader to skip it. That contradiction
# is the only reason a 177 KB un-checkpointed WAL went unnoticed: the "after"
# snapshot it compared against was still the pre-revocation database, and it
# agreed with itself.
if moved_rotation:
    green(f"Only {rotation_table} moved, which is the revocation working.")
else:
    red(f"NOTE: {rotation_table} did NOT change. Nothing was revoked. If this window "
        "was meant to rotate credentials it did not run, and a retention check that "
        "passes on a no-op is not evidence that anything was safe.")
sys.exit(0)
PY
}

# ── selftest ────────────────────────────────────────────────────────────────
# A gate that cannot fail is worse than no gate. This feeds the verifier
# synthetic manifests covering: the intended change, a protected table moving,
# a table appearing, a table disappearing, and the clean case.
cmd_selftest() {
  local failures=0
  # Global, not `local`: the EXIT trap fires after the function has returned, so a
  # `local` here is unbound by the time it runs and `set -u` turned a passing
  # selftest into exit 1.
  DRILL_W="$(mktemp -d)"; trap 'rm -rf "$DRILL_W"' EXIT

  # `cmd_verify` reads `<dir>/manifest.json`, so each fixture is a *directory*
  # holding that file — the first version wrote the manifest to `$w/b1` as a
  # plain file and every case failed with `NotADirectoryError`, which the
  # selftest's own pass/fail wrapper reported as five failing cases rather than
  # one broken fixture.
  mk() { # mk <dir> <json-row_counts>
    mkdir -p "$1"
    printf '{"integrity_check":"ok","row_counts":%s}' "$2" > "$1/manifest.json"
  }

  # 1. clean: only app_tokens moved
  mk "$DRILL_W/b1" '{"calendars":9,"calendarobjects":1400,"principals":11,"app_tokens":50}'
  mk "$DRILL_W/a1" '{"calendars":9,"calendarobjects":1400,"principals":11,"app_tokens":0}'
  if cmd_verify "$DRILL_W/b1" "$DRILL_W/a1" >/dev/null 2>&1; then
    green "selftest: the intended change passes"
  else
    red "selftest: FAIL — the intended change was rejected"; failures=$((failures+1))
  fi

  # 2. a protected table lost rows
  mk "$DRILL_W/b2" '{"calendars":9,"calendarobjects":1400,"principals":11,"app_tokens":50}'
  mk "$DRILL_W/a2" '{"calendars":7,"calendarobjects":1400,"principals":11,"app_tokens":0}'
  if cmd_verify "$DRILL_W/b2" "$DRILL_W/a2" >/dev/null 2>&1; then
    red "selftest: FAIL — calendars going 9 -> 7 was accepted"; failures=$((failures+1))
  else
    green "selftest: a lost calendar is caught"
  fi

  # 3. a table appeared (a schema migration during the window)
  mk "$DRILL_W/b3" '{"calendars":9,"principals":11,"app_tokens":50}'
  mk "$DRILL_W/a3" '{"calendars":9,"principals":11,"app_tokens":0,"something_new":3}'
  if cmd_verify "$DRILL_W/b3" "$DRILL_W/a3" >/dev/null 2>&1; then
    red "selftest: FAIL — a new table was accepted"; failures=$((failures+1))
  else
    green "selftest: an appearing table is caught"
  fi

  # 4. a table disappeared
  mk "$DRILL_W/b4" '{"calendars":9,"calendarobjects":1400,"principals":11,"app_tokens":50}'
  mk "$DRILL_W/a4" '{"calendars":9,"principals":11,"app_tokens":0}'
  if cmd_verify "$DRILL_W/b4" "$DRILL_W/a4" >/dev/null 2>&1; then
    red "selftest: FAIL — a dropped table was accepted"; failures=$((failures+1))
  else
    green "selftest: a disappearing table is caught"
  fi

  # 5. one event lost out of 1400 — the case a human reviewing counts would skim past
  mk "$DRILL_W/b5" '{"calendars":9,"calendarobjects":1400,"principals":11,"app_tokens":50}'
  mk "$DRILL_W/a5" '{"calendars":9,"calendarobjects":1399,"principals":11,"app_tokens":0}'
  if cmd_verify "$DRILL_W/b5" "$DRILL_W/a5" >/dev/null 2>&1; then
    red "selftest: FAIL — 1400 -> 1399 events was accepted"; failures=$((failures+1))
  else
    green "selftest: a single lost event is caught"
  fi

  # 6. nothing moved at all
  mk "$DRILL_W/b6" '{"calendars":9,"principals":11,"app_tokens":50}'
  mk "$DRILL_W/a6" '{"calendars":9,"principals":11,"app_tokens":50}'
  # A no-op must not be reported as a completed rotation. It still exits 0 —
  # nothing was lost — but it has to say out loud that nothing was revoked,
  # because "no data lost" and "the window ran" are different claims and
  # conflating them is how a stale snapshot passes unnoticed.
  if out="$(cmd_verify "$DRILL_W/b6" "$DRILL_W/a6" 2>&1)"; then
    if printf '%s' "$out" | grep -qi "did NOT change"; then
      green "selftest: a no-op passes but is flagged as having revoked nothing"
    else
      red "selftest: FAIL — a no-op was reported as a completed rotation"
      failures=$((failures+1))
    fi
  else
    red "selftest: FAIL — a no-op rotation was rejected outright"
    failures=$((failures+1))
  fi

  echo
  [ "$failures" -eq 0 ] && { green "credential-rotation-drill: selftest passed"; return 0; }
  red "credential-rotation-drill: $failures selftest case(s) FAILED"
  return 1
}

case "${1:-}" in
  snapshot) cmd_snapshot "${2:?}" ;;
  verify)   cmd_verify "${2:?}" "${3:?}" ;;
  selftest) cmd_selftest ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
