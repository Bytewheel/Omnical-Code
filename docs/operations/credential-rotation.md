# Credential rotation runbook (DEFERRED — not yet executed)

**Status: NOT DONE. Deliberately deferred by the user on 2026-09-28.**

Wave 0 of `PLAN_DEPLOYMENTS.md` removed both credential sets from the
publishable git history. That is the *containment*. These credentials are still
**live on the router** and are the reason this file exists. Nothing here has
been run.

## Why this is not optional

| Credential | Status | Blast radius if leaked |
|---|---|---|
| Let's Encrypt **private key**, `CN=0115d8cf.duckdns.org`, serial `06FD83DB0F39B1AF4EC8D781C13A7477ECCF`, valid **2026-09-04 → 2026-12-03** | live, served by `dav-tls` on `192.168.1.21:8443` | anyone can impersonate `https://0115d8cf.duckdns.org:8443` — a full MITM on a CalDAV server carrying other people's calendars. Rotating is also how you *revoke*, since the cert is only invalid once revoked or expired. |
| **50 app tokens** across 11 non-guest principals (15 of them were in the committed `.mobileconfig`) | live | direct read/write DAV access to every one of those accounts |

The repository is now safe to publish (`PLAN_DEPLOYMENTS.md` §10 needs it to be
public for AGPL-3.0 §13). **Publishing does not fix the live credentials** — it
only stops adding to the problem. Both must be rotated before any public push
is the *only* copy, and before the hosted deployment exists.

## Constraint: rotation logs real people out

Revoking the app tokens breaks every CalDAV/CardDAV client until the user
re-provisions. This is not theoretical — the affected principals include real
family and work accounts:

```
lynscarlton@gmail.com    6 tokens
nicholas@carltonaudio.com 5
live-test-20260914@example.com 5
lyrest@gmail.com         5
nfcarlton@gmail.com      5
nfcalaway@gmail.com      4
nfcalaway@novo-ordo.com  4
zero@novo-ordo.com       4
chris@carltonaudio.com   4
burningserenity@gmail.com 4
nicholas@hawksnestsoftware.com 4
```

**Sequence the work so people are told before they are cut off**, and prefer a
window where the affected people are reachable. Password login and the web
portal keep working throughout — only DAV app tokens break.

## Part 0 — fingerprint the customer data (before you touch anything)

**Do this first. It is the only step that makes the retention requirement
checkable instead of assumed.**

Parts 1 and 2 below can report complete success while having lost somebody's
calendar: Part 1's gate is "the new cert is valid", Part 2's is "the old token
401s". Neither looks at a single row. The requirement is that **every calendar,
event, task, contact and everything else survives**, and this fingerprints the
whole database so that claim can be falsified afterwards.

```sh
CFG=/etc/rustical/config.toml
R="ssh router"

# On the router: write a backup, keep the manifest.
$R "mkdir -p /var/lib/omnical/rotation-drills"
$R "/usr/sbin/rustical --config-file $CFG backup \
      --out-dir /var/lib/omnical/rotation-drills"
$R "cp /var/lib/omnical/rotation-drills/omnical-backup-*.tar\manifest.json \
     /var/lib/omnical/rotation-drills/before/manifest.json"   # see drill below
```

Or, from this repo, which does the whole thing including the comparison:

```sh
export ROTATION_DRILL_BIN=/usr/sbin/rustical
export ROTATION_DRILL_CONFIG=$CFG
scripts/credential-rotation-drill.sh snapshot before   # -> rotation-drills/before/
#   … Parts 1 and 2 …
scripts/credential-rotation-drill.sh snapshot after
scripts/credential-rotation-drill.sh verify rotation-drills/before rotation-drills/after
```

`verify` exits non-zero unless **every user table but `app_tokens` is unchanged.**
`app_tokens` is the sole exception, because deleting those rows *is* the
revocation; everything else moving — a calendar, an event, a contact, a
principal, a table appearing or vanishing — is a failure, and it says so in
those words. The fingerprint is the `row_counts` map already in the backup
manifest, so the drill and the backup cannot disagree about what is in the
database, and `snapshot` refuses to run against a database failing
`PRAGMA integrity_check`.

Rehearsed end to end against a real migrated database: a clean revocation
passes, and deleting one event out of four fails with
`calendarobjects 4 -> 3 <- CUSTOMER DATA CHANGED`. Its own gate
(`scripts/credential-rotation-drill.sh selftest`, wired into CI) covers the
same cases plus a table appearing and one disappearing, because a check that
cannot fail is worse than no check.

**If `verify` fails, do not report the rotation clean.** Restore the `before`
archive (`rustical restore`), find what touched the wrong rows, and re-run.

### The database is in WAL mode — copy the `-wal` or checkpoint first

The live database is `db.sqlite3` in **WAL mode**. A `DELETE` committed through
the CLI is written to `db.sqlite3-wal` and may not reach the main file for a long
time. On 2026-09-30 the guest-token revocation left **177,192 bytes** of WAL
un-checkpointed.

If you copy `db.sqlite3` to another machine to fingerprint it, **you will copy
the pre-revocation database**, and nothing in the manifest will reveal it: the
counts are internally consistent, just old. Run the drill *on the router* (as
above), where the WAL lives, or checkpoint first:

```sh
ssh router 'sqlite3 /usr/local/share/rustical/db.sqlite3 "PRAGMA wal_checkpoint(TRUNCATE);"'
# or, to copy:
scp router:/usr/local/share/rustical/db.sqlite3{,-wal,-shm} /tmp/
```

This is exactly how a **green retention check came to mean nothing** on the first
attempt at this window: it reported `app_tokens 69 -> 69` while the live database
had already gone to 50. The `verify` output has since been changed to say so
loudly instead of narrating "the rotation worked" over an unchanged table.

**If `verify` reports that the credential table did not change, treat that as a
failed window, not a clean one.** Nothing was revoked.

## Part 1 — rotate the TLS certificate

**Impact: a few seconds of TLS interruption on `:8443` if done carelessly.
Nothing else breaks. This one is cheap; do it first.**

```sh
# 0. Pre-flight: confirm what is currently live.
ssh router 'openssl x509 -in /etc/rustical/tls/fullchain.pem -noout -subject -dates -serial'
# expect: CN=0115d8cf.duckdns.org, notAfter=Dec  3 22:51:35 2026 GMT,
#         serial=06FD83DB0F39B1AF4EC8D781C13A7477ECCF
```

The certificate is issued via **DNS-01 through the DuckDNS API** (PLAN.md
Phase 3). Re-issue it on the dev machine with whatever ACME client was used —
find it in PLAN.md §Phase 3 / `PLAN.md:718-794`, do not assume certbot.

```sh
# 1. Re-issue to a staging dir first. Never overwrite the live pair in place.
mkdir -p ~/omnical-certs-new
# <your acme client> ... --dns duckdns --domain 0115d8cf.duckdns.org \
#     --cert-out ~/omnical-certs-new/fullchain.pem \
#     --key-out  ~/omnical-certs-new/key.pem

# 2. Verify BEFORE installing.
openssl x509 -in ~/omnical-certs-new/fullchain.pem -noout -subject -dates
openssl x509 -in ~/omnical-certs-new/fullchain.pem -noout -text \
  | grep -A2 "X509v3 Subject Alternative Name"     # must list the domain
openssl pkey -in ~/omnical-certs-new/key.pem -noout && echo "key parses"
# confirm the key matches the cert's public key
diff <(openssl x509 -in ~/omnical-certs-new/fullchain.pem -noout -pubkey) \
     <(openssl pkey -in ~/omnical-certs-new/key.pem -pubout) \
  && echo "key/cert MATCH"

# 3. Swap atomically. dav-tls reads the cert at start, so a plain overwrite is
#    safe — but stage to a temp name and mv, so a partial write cannot happen.
ssh router 'cp /etc/rustical/tls/fullchain.pem /etc/rustical/tls/fullchain.pem.bak'
cat ~/omnical-certs-new/fullchain.pem | ssh router 'cat > /etc/rustical/tls/.new.pem'
cat ~/omnical-certs-new/key.pem        | ssh router 'cat > /etc/rustical/tls/.new.key'
ssh router 'chmod 600 /etc/rustical/tls/.new.pem /etc/rustical/tls/.new.key
            mv /etc/rustical/tls/.new.pem /etc/rustical/tls/fullchain.pem
            mv /etc/rustical/tls/.new.key /etc/rustical/tls/key.pem
            /etc/init.d/dav-tls restart'

# 4. Verify from OUTSIDE the router, with no -k. A self-signed or
#    wrong-SAN cert must fail here.
curl -sS --resolve 0115d8cf.duckdns.org:8443:192.168.1.21 \
     https://0115d8cf.duckdns.org:8443/ping
echo
ssh router 'openssl x509 -in /etc/rustical/tls/fullchain.pem -noout -serial -dates'
```

**Gate:** external `curl` with no `-k` returns `Pong!`, and the serial is no
longer `06FD83DB…`. Then `openssl x509 -in ~/omnical-certs-new/fullchain.pem
-noout -text | grep -A1 "Authority Key Identifier"` should show a different
CA than the retired one.

**Also:** delete the local `out/tls/` pair. It is no longer used, it is
gitignored, and leaving a retired key lying around is how this happens twice.
Then confirm `out/tls/` is absent from the working tree.

**Rollback if the new cert is bad:** `mv
/etc/rustical/tls/fullchain.pem.bak` back and `/etc/init.d/dav-tls restart`.
The old cert is still valid until **2026-12-03**, so there is real time to fix
a mistake. That expiry is your rollback window — do not let it lapse.

## Part 2 — rotate the app tokens

**Impact: every CalDAV/CardDAV client for the 11 principals above stops
syncing until re-provisioned. Tell people first.**

```sh
CFG=/etc/rustical/config.toml
R="ssh router"
# NB: the --config-file option is TOP-LEVEL and must precede the subcommand.
#     This is a documented footgun (rustical/src/lib.rs:52-57).

# 1. Inventory before you touch anything, and SAVE it — this is your
#    re-provisioning checklist.
$R "/usr/sbin/rustical --config-file $CFG principals list" \
  | awk '{print $1}' | grep -vE '^(guest-|family$)' | while read -r p; do
      echo "== $p"
      $R "/usr/sbin/rustical --config-file $CFG principals app-token list $p"
    done | tee ~/omnical-app-tokens-BEFORE-rotation.txt
```

Then, **per principal, one at a time** — do not loop blindly, because each
`app-token remove` is irreversible and a typo removes the wrong credential:

```sh
p=nicholas@carltonaudio.com
$R "/usr/sbin/rustical --config-file $CFG principals app-token list $p"
# note the token IDs
$R "/usr/sbin/rustical --config-file $CFG principals app-token remove $p <TOKEN_ID>"
# re-issue a fresh token for the same client
$R "/usr/sbin/rustical --config-file $CFG principals app-token add $p <NAME> <TOKEN>"
```

Useful cleanups while in there (all of these are **test** residue, safe to
remove regardless of the rotation decision):

- `live-test-20260914@example.com` — 5 tokens from a 2026-09-14 test
- the `guest-*` principals (`guest-0011862e…`, `guest-1a9c6413…`, …) — orphaned
  test guests, several from the §17.13 banner-nesting incident
- `guest-30343a84-…` referenced in
  `PLAN_NEXT_AGENT_DEPLOY_SHARING.md` §3 as the orphaned share whose credential
  was never shown and is unrecoverable

```sh
# Bulk-delete the throwaway guests (they have no owner to re-provision).
$R "/usr/sbin/rustical --config-file $CFG principals list" \
  | awk '{print $1}' | grep '^guest-' | while read -r g; do
      echo "removing $g"
      $R "/usr/sbin/rustical --config-file $CFG principals remove $g" || \
        echo "  (failed: $g)"
    done
```

**Re-provisioning** (this is the user-facing half):

```sh
# Regenerate the Apple profile from FRESH tokens. It is deterministic (uuid5)
# and reads from `pass`, so nothing is hand-edited.
python3 scripts/make-apple-profile.py     # writes out/omnical-iphone.mobileconfig, mode 0600

# Serve it on the LAN (the existing helper).
python3 scripts/serve-apple-profile.py   # 0.0.0.0:8917
```

Send each person: the `.mobileconfig` link (Apple), or the per-client
instructions the portal now renders on each calendar tile (PLAN.md §17.15/§17.16
— Apple, DAVx5, Thunderbird, plus the "not possible on Google/Outlook" line).

**Gate:** every old token returns `401` on loopback DAV, and every affected
person confirms their calendar syncs again.

```sh
# Old token must now be rejected:
curl -s -o /dev/null -w '%{http_code}\n' -u "<principal>:<OLD_TOKEN>" \
     http://127.0.0.1:4000/caldav/  # expect 401
# New token must work:
curl -s -o /dev/null -w '%{http_code}\n' -u "<principal>:<NEW_TOKEN>" \
     http://127.0.0.1:4000/caldav/  # expect 207 or 200
```

## Part 3 — confirm the container is still clean

Rotation is only half the job; the new history must not re-collect anything.

```sh
cd ~/router-dav
git status --porcelain                      # out/ must not appear
git log --all --oneline -- out/ | head       # must be empty
ls out/tls 2>&1 | head -1                    # must not exist after Part 1
```

`.github/workflows/hygiene.yml` now enforces all of the above on every push
(gitleaks over full history, the tracked-file policy, the `.gitmodules`
assertion, and a 500 MiB `.git` size guard). It runs on GitHub, so it only
protects you **after** a remote exists — until then, re-run the checks by hand.

## Sequencing

1. Part 1 (TLS) — cheap, ~1 min of impact, huge blast radius if leaked. Do it
   first regardless of the token decision.
2. Part 3 (verify clean).
3. Part 2 (tokens) — **only** after telling the affected people, and only in a
   window where they can re-provision. Consider doing one principal as a pilot
   (`nicholas@carltonaudio.com` is the one the live tests were run against) to
   validate the runbook before touching 10 real accounts.

## See also

- `PLAN_DEPLOYMENTS.md` §5.1 (the H1/H2 findings), §5.2 (the tasks), §5.4 (CI),
  §10 (why the repo must be publishable), §18.2 (implementation split)
- `PLAN_NEXT_AGENT_DEPLOY_SHARING.md` §3 (the orphaned CAS share)
- `router-dav/README.md` §Sysupgrade runbook (where TLS files live and what
  survives a firmware flash)
