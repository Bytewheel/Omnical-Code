# router-dav / Omnical

CalDAV (calendar + VTODO tasks) / CardDAV (contacts) server for the libreCMC
router (`192.168.10.1`), public at `https://0115d8cf.duckdns.org:8443`.
Plan + progress: `~/Documents/ByteWheel/omnical/PLAN.md`.

## Running your own copy

This repository builds and deploys Omnical three ways. Two of them are
self-hosting channels you can use right now; the third (a hosted SaaS) is not
built yet. `PLAN_DEPLOYMENTS.md` §7-§9 is the design, §18.7 is what shipped.

**Docker Compose (primary):**

```sh
printf 'OMNICAL_SETUP_ADMIN_EMAIL=you@example.com\nOMNICAL_SETUP_ADMIN_PASSWORD=…\n' > .env
chmod 600 .env
docker compose -f compose.omnical.yml up -d
docker compose -f compose.omnical.yml logs omnical-setup   # the wizard's output
```

That builds the image from `rustical/`, runs `rustical setup`, creates the
administrator and serves on `127.0.0.1:4000`. `compose.omnical.yml`'s header
documents every variable; `rustical setup --help` is the reference for what
they mean. You still have to put a TLS-terminating proxy in front of it — the
server speaks plain HTTP and says so.

**Native tarball + systemd:**

```sh
./scripts/build-rust.sh x86_64-unknown-linux-gnu
sudo ./packaging/native/install.sh --from-file out/x86_64-unknown-linux-gnu/rustical
```

`install.sh` verifies the artefact, installs the binary and the unit, runs the
wizard as the service user, starts the service and health-gates it. Re-running
it is the supported upgrade path; it will not clobber the config, the database
or the administrator. `install.sh --help` is the reference; `--uninstall` takes
it away again and `--purge` deletes the data.

Both channels generate their configuration with **the same command**
(`rustical setup`), from the same `OMNICAL_SETUP_*` answers. Neither contains a
hand-written `config.toml` — that is deliberate, and
`scripts/selfhost-gate.sh` fails if the two ever disagree.

```sh
./scripts/selfhost-gate.sh out/x86_64-unknown-linux-gnu/rustical
```

installs, boots, round-trips a real CalDAV write/read, registers a user through
the invite flow, syncs as that user and re-runs the installer over the result.
It is the `selfhost` job in CI.

Neither channel depends on the other: the native path reaches the identical end
state with no container anywhere, and the compose file is a *packaging* of the
same install. Actually running the Compose channel under Docker or Podman is a
**stretch goal** (`PLAN_DEPLOYMENTS.md` §18.7), not a gate.

**The hosted, multi-tenant SaaS is not built.** No image is published and
`rustical/Dockerfile` is still upstream's, used only as the self-host build.

## Layout

- `rustical/` — upstream [RustiCal](https://github.com/lennart-k/rustical) pinned at `v0.16.1`, fork branch `omnical-scheduling` (adds RFC 6638-style scheduling, token-URL public export feeds, invite-gated registration, a first-run wizard, and in-binary backup/restore)
- `dav-tls/` — the one custom component: minimal rustls TLS tunnel (`192.168.1.21:8443` → `127.0.0.1:4000`, ALPN `http/1.1` only, byte-splice after handshake)
- `router/` — overlay tree deployed onto the router (`etc/rustical`, `etc/init.d/*`, `usr/bin/rustical-watchdog`)
- `packaging/native/` — the tarball channel: `install.sh` + the `omnical.service` template (§8.1)
- `out/` — build artifacts (`rustical`, `dav-tls` = static aarch64-musl; `out/<target>/` for host builds)
- `build/bin/` — zig-musl-cc wrappers (copied from router-nym) used as the cross C toolchain
- `scripts/` — `build-rust.sh` (cross/host builds), `render-router-config.sh` (injects SMTP secrets from `pass`; rendered output never touches dev disk), `nightly-backup.sh` (02:30 cron on the dev machine), `selfhost-gate.sh` (the rows 40-41 gate)

## Commands

```sh
scripts/build-rust.sh                            # aarch64-unknown-linux-musl (router target)
scripts/build-rust.sh x86_64-unknown-linux-gnu   # host build for smoke tests
./deploy.sh                                      # push + enable on the router (idempotent; also THE post-sysupgrade restore)
./packaging/native/install.sh --help             # the tarball channel
./scripts/selfhost-gate.sh out/x86_64-unknown-linux-gnu/rustical
```

## Sysupgrade runbook (Phase 8.3)

Verified 2026-09-06 against the router's real preservation list (ground truth
from a `sysupgrade -b` tarball listing — not assumptions).

**Survives a sysupgrade (no action needed):**

- `/usr/local/share/rustical/db.sqlite3` (+ `-wal`/`-shm`) — the DB, via `/etc/sysupgrade.conf`
- `/etc/rustical/` — `config.toml` (incl. `[scheduling]` SMTP creds + `[subscriptions]`) + `tls/` LE certs, via `/etc/sysupgrade.conf`
- `/etc/crontabs/root` — **all five cron lines, including the DuckDNS updater with its token and `rustical-watchdog`** — via `/lib/upgrade/keep.d/busybox` (not a conffile, but preserved)
- all `/etc/config/*` uci — including the firewall `Allow-Dav-TLS` 8443 rule and uhttpd (LuCI stays on LAN :443)
- dropbear/SSH host keys + `authorized_keys`, network config, `passwd`/`shadow`

**Wiped (must be re-deployed):**

- `/usr/sbin/rustical`, `/usr/sbin/dav-tls` — the binaries
- `/etc/init.d/rustical`, `/etc/init.d/dav-tls` — **custom init.d scripts are NOT conffiles** (only package-provided ones are restored by their packages)
- `/usr/bin/rustical-watchdog`
- opkg-installed packages (`sqlite3-cli`) — reinstalled from feeds by deploy.sh

**Procedure:**

1. **Pre-flash:** confirm the nightly backup is current
   (`ls -l ~/backups/omnical/$(date +%F).tar.gz` — 02:30 cron; `backup.log` also
   records the router disk line). Optionally `sysupgrade -k` to snapshot the
   installed-package list.
2. **Flash normally** (keep-settings is the default). Do NOT pass `-n` — that
   skips the preservation list and loses the DB/config/certs.
3. **First boot after flash:** rustical + dav-tls do NOT run (init scripts are
   wiped), so public 8443 is down until step 4; LuCI is up normally on
   `https://192.168.10.1` (LAN :443). The DB, certs, config, firewall rule and
   crontab are all already in place.
4. **Re-run `~/router-dav/deploy.sh` from the dev machine.** Requires
   `out/rustical` + `out/dav-tls` (aarch64 builds) and the `pass` SMTP entries
   — the config render is fail-fast BEFORE any service stop, so a broken
   `pass` aborts harmlessly. It: stages binaries via `/tmp` (tmpfs), does the
   brief rustical stop→swap→start, re-pushes both init scripts + the watchdog,
   re-renders the config (deterministic — byte-identical to the preserved one),
   re-asserts the sysupgrade.conf entries + the watchdog cron line
   (belt-and-suspenders; touches `/etc/crontabs` if it appends), reinstalls
   `sqlite3-cli` if missing, re-enables and starts both services, health-checks.
   On a wiped router dav-tls's old sha is absent → its (re)start fires; expect
   one benign `ubus … Not found` log line on the first start (Phase 3 finding).
5. **Verify:**

```sh
ssh router '/usr/sbin/rustical --config-file /etc/rustical/config.toml health'  # NB: --config-file PRECEDES the subcommand
ssh router 'netstat -tlnp | grep -E ":4000|:8443"'                               # 127.0.0.1:4000 + 192.168.1.21:8443
ssh router 'logread -e rustical | grep -iE "scheduling|subscriptions" | tail'  # both extension lines
curl -sI https://0115d8cf.duckdns.org:8443/.well-known/caldav                    # 308 (hairpin NAT works from inside too)
vdirsyncer sync                                                                 # hub clean
ssh router 'df -k / | tail -1'                                                  # overlay ≥ 5 MB free
```

**Recovery:** everything irreplaceable (DB/config/certs) survives the flash, so
the only reconstruction is what `deploy.sh` does. If `deploy.sh` cannot run
(e.g. `pass` unavailable), the sysupgrade backup tarball (`sysupgrade -b`) or
the latest `~/backups/omnical/*.tar.gz` restores config/certs/DB — but the
binaries must come from `out/` either way. The idempotent re-run was verified
live on 2026-09-06: ~10 s rustical stop→swap→start, dav-tls untouched when its
sha256 is unchanged (zero TLS blip), and it tolerates the wiped-router state
(missing init script, absent old binary sha).
