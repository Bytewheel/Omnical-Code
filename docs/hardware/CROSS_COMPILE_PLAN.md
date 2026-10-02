# Cross-compile plan: MIPS and ARMv7 targets

**Status: plan only. Nothing has been built for either ISA.** One probe build
was run and its results are recorded below; the rest is unverified reasoning.

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

---

## 2. ARMv7 (`armv7-unknown-linux-musleabihf`) — proceed

### Why this is the low-risk one

- Tier 2 with prebuilt `rust-std`. No nightly, no `-Z build-std`, no
  rebuilding the standard library.
- **The crypto blocker, which is the thing that usually kills embedded Rust,
  does not apply.** `aws-lc-sys` built cleanly for armv7. This matters because
  `Cargo.toml:148` deliberately leaves the rustls provider at its `aws-lc-rs`
  default precisely so the crypto provider matches between the host build and
  the router — so the *default* configuration has to work, and it does.
- Bundled SQLite compiles, so the `-bundled` feature needs no per-target work.
- Targets in market: **Netgear R7800** (IPQ8065, dual Cortex-A15 @ 1.7 GHz,
  512 MB RAM, 128 MB NAND — comfortably above both floors), and the wider
  ipq40xx/ipq806x class.

### The one real obstacle: the link step

The probe compiled every crate and failed only at the final link:

```
ld.lld: error: duplicate symbol: _start_c
  defined at crt1.c   (zig's musl crt1.o)
  defined at crt1.c   (rustc's self-contained crt1.o)
```

Cause: rustc's musl targets link **self-contained CRT objects**, and pointing
the linker at `zig cc` makes zig *also* supply musl's CRT. Both define
`_start_c`.

This is a known recipe problem with a known fix, not an architectural one. Work
it in this order, stopping when one links cleanly:

1. **Let zig provide the CRT entirely.** Suppress rustc's self-contained
   objects:
   ```
   RUSTFLAGS="-C link-self-contained=no"
   ```
   Tried this; it moves the failure to an unresolved symbol referenced from
   `libaws_lc_sys` — closer, and it confirms the CRT collision is the real
   issue rather than a missing toolchain.
2. **Use `rust-lld` directly.** rustc bundles an LLD that already understands
   the self-contained objects, so no second CRT is ever introduced:
   ```
   CARGO_TARGET_ARMV7_UNKNOWN_LINUX_MUSLEABIHF_LINKER=rust-lld
   CARGO_TARGET_ARMV7_UNKNOWN_LINUX_MUSLEABIHF_RUSTFLAGS="-C linker-flavor=gnu-lld"
   ```
3. **Keep zig but strip its CRT** via a wrapper that adds `-nostartfiles` and
   points `LD_LIBRARY_PATH`/sysroot at the Rust-provided objects.
4. **Fall back to a real musl cross-toolchain** (`musl-cross-make`, or Debian's
   `gcc-arm-linux-gnueabihf` + musl) if all of the above fight. Most robust,
   slowest to set up.

Note that `zig cc` cannot be handed to cargo as a multi-word `LINKER` value —
cargo does not shell-split it. It has to be a wrapper script:

```sh
#!/bin/sh
exec zig cc -target arm-linux-musleabihf "$@"
```

`scripts/build-rust.sh` will need the same treatment as its existing
`build/bin/zig-musl-cc` wrapper.

### Steps

1. Add `armv7-unknown-linux-musleabihf)` to the `case` statement at
   `scripts/build-rust.sh:40`, mirroring the `aarch64` arm: `zig-musl-cc`,
   `zig-musl-ar`, `zig-musl-nm`, `zig-musl-ranlib` symlinks, plus the
   `ZIG_MUSL_CFLAGS` and `-nostartfiles` handling.
2. Resolve the link per §2 options above. Gate: a trivial binary that links and
   runs under `qemu-arm`.
3. Extend the `dest="$OUT/$TARGET/$bin"` logic — note `build-rust.sh:113`,
   `:134` and `:144` already special-case `aarch64` for exactly this, so the
   per-target output directory scheme is in place.
4. Verify the size gate. A 32-bit build should land *under* the aarch64 figure
   (17.7 MB unpacked), which gives more overlay headroom per device, partially
   compensating for the smaller devices.
5. **Add the qemu-arm smoke test.** There is currently no emulator-based test —
   CI runs `cargo test` on the host (`.github/workflows/cicd.yml:32`), which
   exercises the x86_64 build only. `qemu-arm-static` running `rustical
   --version` and a `principals list` against a scratch SQLite file would catch
   ISA-specific breakage in CI. This is the step most likely to be skipped and
   most worth not skipping.
6. Cross-build an R7800-class ImageBuilder and run `firmware-gate.sh` against
   it.

### Risk: low. Effort: modest. Unlocks: a large, cheap, well-supported device class.

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
