# Cross-compile plan: MIPS and ARMv7 targets

**Status: ARMv7 BUILT and VERIFIED. MIPS not attempted. Package builds; the
appliance *image* needs a full buildroot — see §2.**

`armv7-unknown-linux-musleabihf` now builds via
`scripts/build-rust.sh armv7-unknown-linux-musleabihf` and passes
`scripts/smoke-cross.sh`. §2 below records what it actually took, which was not
quite what the plan predicted. §3 (MIPS) is still unverified reasoning.

Companion to [`DEVICE_TARGETS.md`](DEVICE_TARGETS.md), which lists the devices
these targets would unlock.

---

## 0. A correction to the premise

**We have never built MIPS for the local router. The router is aarch64.**

Measured on the live unit just now:

```
DISTRIB_TARGET='rockchip/armv8'
DISTRIB_ARCH='aarch64_generic'
compatible: thinkpenguin,tpe-r1400 / rockchip,rk3328
Features    : fp asimd evtstrm aes pmull sha1 sha2 crc32 cpuid
```

`asimd` is the AArch64 crypto/FP extension set; a MIPS CPU has no such feature.
And the artifact this repo builds and deploys is unambiguously 64-bit:

```
out/rustical: ELF 64-bit LSB executable, ARM aarch64, ... statically linked
```

`scripts/build-rust.sh` has exactly two cases — `aarch64-unknown-linux-musl`
and `x86_64-unknown-linux-gnu` (the latter being a host build for smoke tests).
Anything else exits with `unsupported target`.

The confusion is understandable and worth naming, because it points at
something real: **libreCMC's supported-hardware list is predominantly 32-bit
MIPS** (ar71xx/ath79 — Archer C7, TL-WR1043ND, WNDR3800, Ben NanoNote), *and*
ThinkPenguin ships small MIPS routers (TPE-R1200, TPE-R1300 — QCA9531) that
ship with libreCMC preinstalled. So "libreCMC device" and "aarch64" are close to
opposite sets. That mismatch is the actual finding of this document, and it is
presumably why the TPE-R1400 was chosen for this appliance: within the libreCMC
lineup, it is the one device that is 64-bit.

**Consequence:** every device in the MIPS row of `DEVICE_TARGETS.md` §5 is
unreachable today, and the same is true of the ARMv7 row. This plan is what
closes that gap.

---

## 1. What was actually verified

I installed targets and ran a real probe build rather than reasoning from
target lists. Results:

| Check | Command | Result |
|---|---|---|
| armv7 std available? | `rustup target add armv7-unknown-linux-musleabihf` | **succeeds** — Tier 2, prebuilt `rust-std` |
| MIPS std available? | `rustup target add mipsel-unknown-linux-musl` | **fails** — `no prebuilt artifacts available` |
| MIPS std (BE)? | `rustup target add mips-unknown-linux-musl` | **fails** — same |
| `aws-lc-rs` on armv7 | probe crate, `--release` | **compiles** — produced `libaws_lc_0_45_0_crypto.a` |
| bundled SQLite on armv7 | probe crate | **compiles** — produced `libsqlite3.a` |
| final link on armv7 | probe crate | **fails** — see §2 |
| MIPS crypto support | read `aws-lc-sys-0.44.0/builder/main.rs:405` | `fn target_chokes_on_u1() -> bool { target_arch() == "mips" \|\| ... }` — upstream carries an explicit MIPS workaround |

Toolchain in use: `rustc 1.98.1 (48a229cea 2026-09-01)`, stable.

The two load-bearing results: **armv7 has a prebuilt std and our entire native
dependency stack compiles for it**, and **MIPS has no prebuilt std at all**.

### Since the armv7 build landed

| | Result |
|---|---|
| `aws-lc-sys` on armv7 | compiles (`libaws_lc_0_45_0_crypto.a`) |
| `openssl-sys` on armv7 | compiles |
| `ring` on armv7 | compiles |
| bundled SQLite on armv7 | compiles **and runs** — 21 tables migrated under `qemu-arm` |
| undefined symbols in the linked armv7 binary | **0** |
| rustical size | 16,182,268 → 4,642,880 packed (vs aarch64 17,716,384 → 5,467,184) |

---

## 2. ARMv7 (`armv7-unknown-linux-musleabihf`) — DONE

```
scripts/build-rust.sh armv7-unknown-linux-musleabihf   # builds
scripts/smoke-cross.sh armv7-unknown-linux-musleabihf   # verifies
```

### Why this was the low-risk one

- Tier 2 with prebuilt `rust-std`. No nightly, no `-Z build-std`, no
  rebuilding the standard library.
- **The crypto blocker, which is the thing that usually kills embedded Rust,
  did not apply.** `aws-lc-sys` built cleanly for armv7. This matters because
  `Cargo.toml:148` deliberately leaves the rustls provider at its `aws-lc-rs`
  default precisely so the crypto provider matches between the host build and
  the router — so the *default* configuration has to work, and it does.
- Bundled SQLite compiles, so the `-bundled` feature needs no per-target work.
- Targets in market: **Netgear R7800** (IPQ8065, dual Cortex-A15 @ 1.7 GHz,
  512 MB RAM, 128 MB NAND — comfortably above both floors), and the wider
  ipq40xx/ipq806x class.

### What actually blocked it — two things, neither predicted

**The link line was never the problem.** §2 below originally proposed four
recipes for the `duplicate symbol: _start_c` collision. All four were
unnecessary: `scripts/build-rust.sh` already documented the answer in its own
header, and the plan had not read it. The script's `clang` path uses clang with
zig's musl headers and links with `rust-lld` plus rustc's self-contained CRT,
with a comment saying `TOOLCHAIN=zig` is *"broken at link"* for exactly this
reason. My probe had used `zig cc` as the linker driver — the documented-broken
route. Mirroring the existing `clang` branch solved it on the first build
attempt.

The real obstacles were these two:

**1. A genuine 32-bit portability bug in the source.** `src/appliance.rs`
computed overlay free space as:

```rust
total: stats.f_blocks.saturating_mul(block),   // block came from f_frsize
```

The existing comment said `c_ulong` is *"unsigned on every platform this builds
for, so no cast is needed"* — true when aarch64 was the only cross target.
`f_frsize` is a `c_ulong`, so it is **32-bit on armv7** while `f_blocks` and
`f_bavail` are `fsblkcnt_t` and 64-bit on both. Two `E0308`s. Fixed with
`u64::from(...)`, which is the identity on a 64-bit source and a lossless
widening on a 32-bit one — and with a targeted
`#[allow(clippy::useless_conversion)]`, because clippy is right that the
conversion is a no-op *on the host* and wrong about armv7.

This is the kind of bug that only surfaces on a second architecture, which is
the argument for having built one.

**2. `-latomic`, which needed a different fix than planned.** rustc puts
`-latomic` on every armv7 link line — a 32-bit ARM core cannot do 64-bit atomics
natively — but its self-contained musl sysroot ships no `libatomic.a`, so the
link failed with *"unable to find library -latomic"* before symbol resolution.
No armv7 `libatomic.a` exists anywhere on this host, and neither clang nor zig
ships one.

The fix: **cortex-a15 has LPAE**, so LLVM lowers 8-byte atomics to
`ldrexd`/`strexd` inline instead of calling `__atomic_*`. Nothing is ever
pulled from the library, so an **empty** archive created with `llvm-ar crs`
satisfies the linker's search. This is verified, not assumed: the linked binary
has **zero undefined symbols** (`nm -u`), so the stub is provably unreferenced.
`-Ctarget-cpu=cortex-a15` is also the honest choice for the target class —
cortex-a15 *is* the R7800's core.

### What was done to the build script

- Factored the aarch64 toolchain configuration into `configure_musl_cross`,
  parameterised by `(rust target, zig C triple)`, so a third target is one
  `case` arm rather than a 20-line copy. The aarch64 `clang` recipe is
  unchanged — verified by rebuilding it after the refactor (`dav-tls` came out
  byte-identical at 620,884).
- Replaced the four hardcoded `[ "$TARGET" = "aarch64-..." ]` tests with a
  `ROUTER_TARGET` constant and a `bin_dest` helper.
- Extended the size gate to **every** device target, not just aarch64. A 32-bit
  build is the one that has to fit smaller devices' flash.
- Added a `rustup target list --installed` preflight that fails with the exact
  `rustup target add` command, rather than letting cargo die inside a C build
  script.

### Verification

`scripts/smoke-cross.sh` (new) runs the artifact and is reproducible rather than
ad-hoc. It checks the ELF is static and the right width/ISA, that there are zero
undefined symbols, then **executes** it under qemu and drives `principals list`
against a throwaway database — which is what actually exercises bundled SQLite
and the migration path on the target ISA. All seven checks pass for armv7, and
also for aarch64.

### The appliance image: what actually works, and what does not

This is where the plan was wrong, and the correction matters for anyone who
picks this up next. **The ImageBuilder cannot build an appliance image carrying
a custom package or profile, and the SDK cannot build an image at all.**

| | produces an image? | sees a custom package/profile? |
|---|---|---|
| **ImageBuilder** | yes (`make image PROFILE=`) | **no** |
| **SDK** | **no** (no `image` target exists) | yes |
| **full buildroot** | yes | yes |

The ImageBuilder ships `.packageinfo` and `.targetinfo` **generated at release
time**, and `make image` never re-scans `package/`. So:

- `PACKAGES="omnical"` → `omnical (no such package)`
- `PROFILE=omnical` → `Profile "omnical" does not exist!`

Both fail, and neither is fixable by hand-editing: nothing regenerates those
indexes. `.profiles.mk` is derived from `.targetinfo` via
`scripts/target-metadata.pl`, which parses `.targetinfo` rather than the `.mk`
files — so appending `include omnical.mk` to the subtarget is silently ignored.

The SDK is the mirror image. It *does* re-scan (`include/toplevel.mk`'s
`prepare-tmpinfo`) and finds both — `tmp/.targetinfo` gains the profile and
`tmp/.packageinfo` gains `Package: omnical` — but its only targets are
`world`/`prepare`/`prereq`. There is no `image` target in an SDK; its own help
says it is for building packages.

**So the appliance image needs a full buildroot** (`git clone` of openwrt at
the release tag, `./scripts/feeds update/install`, toolchain build). That is a
real piece of work and is not done.

One OpenWrt 25.12 gotcha on the way, worth recording: `feeds update -a &&
feeds install -a` installs *all* feeds, and that trips genuine kconfig cycles
in the release-pinned feeds — `LIBCURL_LDAP` ↔ `PACKAGE_libcurl`,
`PACKAGE_NETATALK_LDAP` ↔ `PACKAGE_netatalk-full`, and others. Feeds are pinned
to the correct 25.12.5 commits, so this is not a version drift. The official
buildroot only enables the feeds it needs, so the fix is the same:
`./scripts/feeds update -i packages`. With that, `make defconfig` succeeds.

### What the SDK *did* produce

A real package, which the ImageBuilder never could:

```
bin/packages/arm_cortex-a15_neon-vfpv4/base/omnical-0.16.1-r1.apk
```

`apk adbdump` confirms `usr/sbin/{rustical,dav-tls}`, `usr/bin/rustical-watchdog`,
`etc/init.d/{rustical,dav-tls}`, `etc/rustical/omnical-config.toml.sample`,
`lib/upgrade/keep.d/omnical`, generated conffiles, `arch: arm_cortex-a15_neon-vfpv4`,
`depends: libc sqlite3-cli`.

Building it found a real bug in the committed recipe that nothing had ever
exercised: `Package/omnical/install` installed `rustical-watchdog` into
`$(1)/usr/bin` without an `$(INSTALL_DIR) $(1)/usr/bin`, so the install step
died with `cannot create regular file ... No such file or directory`. Fixed in
`packaging/firmware/omnical/Makefile`. This would have failed on *any* OpenWrt
build; it had survived because the image phase had never run.

### Done

- The armv7 cross-build and its smoke test (§2 above).
- `scripts/smoke-cross.sh`, wired into CI for **both** cross targets. The aarch64
  job previously built the router's binary and never executed it.
- `scripts/firmware-gate.sh`: **all checks pass**, including the postinst
  byte-identical-config and idempotence gates.

### Still not done

- The sysupgrade image itself, which needs a full buildroot as above.
- `omnical.keep.d` and the postinst are exercised by `firmware-gate.sh` against
  a synthetic root, not against a real flash.

---

## 3. MIPS 32-bit — do not attempt yet

### Why this is categorically harder than armv7

**No prebuilt `rust-std` exists for `mips-unknown-linux-musl` or
`mipsel-unknown-linux-musl`.** Confirmed above. Consequences:

- Requires **nightly** (`-Z build-std=std,panic_unabort`), because the build
  needs to compile `std` from source for a target the stable toolchain does not
  ship.
- The workspace uses `#![warn(clippy::all, clippy::pedantic, clippy::nursery)]`
  in several crates — nightly brings lint churn that would have to be separated
  from real breakage.
- CI would need a nightly lane, or a pinned nightly with a documented reason it
  cannot be stable. That is a permanent tax on the build for one target.

**The crypto story is unproven.** `aws-lc-sys` does carry MIPS-specific
handling — `target_chokes_on_u1()` exists precisely because some MIPS
back-ends mis-handle the `\x01` symbol prefix — which shows upstream has
*thought* about MIPS. It does not show MIPS is *supported*. Our armv7 result
came from an actual successful build; the equivalent MIPS build has not been
attempted and may not link.

**And then the flash floor probably disqualifies the devices anyway.** The
overwhelming majority of 32-bit MIPS routers in libreCMC's list have 8–16 MB of
flash: Archer C7 (16 MB), TL-WR1043ND (16 MB), WNDR3800 (8 MB), Ben NanoNote
(8 MB). Our budget is 35 MiB. The ThinkPenguin TPE-R1200 is the sole MIPS
device with 128 MB of NAND — and its sibling TPE-R1300 has only 16 MB NOR.

So the honest position: **the highest-effort, highest-risk target in this
document would, on completion, most likely unlock exactly one device.**

### If it is attempted anyway

1. Install a nightly toolchain; add a `-Z build-std=std,panic_abort`
   path. Keep it out of the stable CI lane.
2. Probe `aws-lc-sys` for `mipsel-unknown-linux-musl` **first, in isolation**,
   before touching the workspace. This is the go/no-go gate; if the crypto
   library will not build, nothing else matters. The fallback is swapping the
   rustls provider to `ring`, which has its own MIPS story — but `ring` is not
   in the current dependency graph for the server, so that is a real change
   with FIPS-adjacent implications worth a deliberate decision, not a
   workaround.
3. Zig supports `mipsel-linux-musl`, so the C toolchain half is likely fine.
   UPX supports linux/mipsel. Verify both rather than assuming.
4. MIPS is big- and little-endian in the field (`mips` vs `mipsel`). The
   router SoCs are predominantly `mipsel`. Building only `mipsel` halves the
   eventual test matrix.

### Risk: high. Effort: high. Unlocks: probably one device.

---

## 4. Recommendation

**Do the ARMv7 target. Do not do MIPS.**

ARMv7 is a Tier-2 target with a prebuilt std whose entire native dependency
stack — `aws-lc-sys` and bundled SQLite, the two things that actually block
embedded Rust builds — compiles for it. Its only obstacle is a link-line recipe
problem, which is an afternoon of work against four known solutions. It unlocks
the ipq806x/ipq40xx class: mature OpenWrt support, 512 MB RAM, 128 MB NAND, and
plentiful second-hand availability.

MIPS is Tier 3 with no prebuilt std, an unproven crypto story, and a device
list whose flash sizes disqualify nearly every entry on the 35 MiB overlay
budget regardless of whether the build succeeds. The asymmetry is not close.

Two cheaper things to do first, if the goal is more device coverage:

1. **Revisit the overlay budget for the MT7981 class** (Cudy WR3000 and
   friends). They already have the right ISA and cost ~US$38. The blocker is
   16 MB of NOR against a 35 MiB budget — an architectural decision about
   extroot, not a compiler problem, and it would unlock a large cheap tier for
   zero build work.
2. **Finish the TWRP-style build** described in `DEVICE_TARGETS.md` §4. The
   packaging model and, critically, the `firmware-gate.sh` postinst-safety test
   already exist and transfer nearly unchanged. It targets aarch64 — the ISA we
   already build — and reaches every arm64 Android device in existence,
   including used phones at US$40.

In other words: the path to more targets runs through packaging and flash
budget, not through new ISAs. Only ARMv7 is worth opening as a new ISA, and only
because its barrier is genuinely low.
