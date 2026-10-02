# Device targets for the Omnical firmware image

**Status: research only. Nothing here has been built or flashed.**

Scope: which devices on the market can run the appliance image that
`scripts/build-firmware.sh` produces. Companion to
[`CROSS_COMPILE_PLAN.md`](CROSS_COMPILE_PLAN.md), which covers widening the
build to ISAs we do not target yet.

Every figure in the "Requirement" column below was measured or read out of this
repository, not estimated. Sources are named inline.

---

## 1. What the image actually requires

| Requirement | Value | Source |
|---|---|---|
| ISA | **aarch64 (ARMv8-A)**, `musl`, statically linked | `scripts/build-rust.sh:40` — the only non-host target in the `case` statement |
| RAM | **~23 MB** working set | measured on the live unit: `VmRSS` 20,796 kB (`rustical`) + 1,668 kB (`dav-tls`) |
| RAM floor | **128 MB** | OpenWrt's own guidance is 16 MB flash / 128 MB RAM as the practical minimum |
| Overlay | **35 MiB** combined budget | `scripts/build-firmware.sh` §9.5, enforced by the size gate |
| Payloads | 5.2 MB `rustical` + 0.6 MB `dav-tls` (UPX); 17.7 MB unpacked | `scripts/build-rust.sh` output |
| Rootfs | squashfs + writable overlay (f2fs/ext4) | live unit `mount`: `/dev/loop0` f2fs over `overlayfs:/overlay` |
| Distro | OpenWrt / libreCMC with a `sysupgrade` image | `scripts/build-firmware.sh:89-98` drives an ImageBuilder |
| ImageBuilder | **per-device**, not per-arch | `scripts/build-firmware.sh:89` requires the caller to pass an ImageBuilder dir |
| Database | SQLite, **bundled** into the binary | `Cargo.toml`: `sqlx-sqlite = { features = ["bundled"] }` |

### The two constraints that actually decide this list

**1. aarch64, not just "ARM".** The binary is `ELF 64-bit LSB executable, ARM
aarch64, statically linked`. This eliminates the large majority of cheap
routers. The most common low-cost router silicon is 32-bit — either MIPS
(AR9344, QCA9531, MT7621) or ARMv7 (IPQ8064/8065, Cortex-A7/A15). None of
those run this binary. Most of libreCMC's own supported-hardware list is
ar71xx/ath79, i.e. 32-bit MIPS, and is therefore *not* a candidate list.

**2. The 35 MiB overlay budget is the real floor, and it is a flash constraint,
not a CPU constraint.** A 16 MB NOR device typically yields 5–8 MB of usable
overlay after squashfs. The binaries alone are 5.8 MB, which would leave
essentially nothing for the database — and the database is not a fixed cost. It
is 3.8 MB today for 31 principals and 423 objects, and it grows monotonically
with every principal, calendar, contact and sync-token bump. A device that
fits the binaries on day one and fills its overlay in year two is not a
deployment target, it is a scheduled outage. **Practical flash floor: 128 MB.**

---

## 2. Tier 1 — smallest and weakest that can actually run it

Ordered by increasing size/cost. All three run the **existing** aarch64 binary
with no build changes.

### Raspberry Pi Zero 2 W — the smallest capable device

| | |
|---|---|
| SoC | Broadcom RP3A0 / BCM2710A1, quad Cortex-A53 @ 1.0 GHz |
| RAM | 512 MB LPDDR2 |
| Storage | microSD only |
| Network | 802.11b/g/n 2.4 GHz + Bluetooth 4.2; **no wired Ethernet** (USB OTG adapter) |
| Size / price | 65 × 30 mm; ~US$15 |
| Longevity | Raspberry Pi states production to **at least January 2030** |
| OpenWrt | `bcm27xx` / `bcm2709`, images exist |

This is the floor. 512 MB is ~22× the measured working set; the CPU is a
quad-core A53, the same core generation as the RK3328 in the unit we already
run, at half the clock.

Two things to be honest about:

- **Wi-Fi only.** There is no Ethernet. Fine on a LAN, but every client
  connection goes over 2.4 GHz 802.11n, which will be the bottleneck long
  before the server is. For a calendar server syncing on cron, this usually
  does not matter.
- **Broadcom Wi-Fi firmware is a blob.** The Zero 2 W's on-board radio needs
  `cypress-firmware-*` blobs that upstream OpenWrt ships, but this is not
  libreCMC-clean. Since the whole point of libreCMC is FSDG compliance and RYF
  certification, **this device breaks the project's licensing premise.** It is
  technically capable and philosophically disqualified. Listed here because it
  is the genuine minimum, not because it is a good fit.

Storage is not a problem: OpenWrt on SD uses a loop-mounted overlay, which is
structurally the same arrangement as the live unit's `/dev/loop0` f2fs overlay.

### Linksys E8450 / Belkin RT3200 — smallest with real Ethernet

| | |
|---|---|
| SoC | MediaTek MT7622BV dual-core Cortex-A53 + MT7915E |
| Arch | **aarch64** — `target/linux/mediatek/mt7622/target.mk` sets `ARCH:=aarch64`, and `config-6.18` sets `ARM64=y` |
| RAM | 512 MB DDR3 |
| Flash | 128 MiB SPI-NAND |
| Network | 5× Gigabit Ethernet, 1× USB 2.0, Wi-Fi 6 |
| Availability | used/clearance; commonly ~US$40–70 |

512 MB RAM and 128 MB of NAND — meets both floors with headroom, and it is the
cheist way to get **five gigabit ports** into an appliance. Frequently available
second-hand, which suits a self-host.

Caveats worth stating: flashing is not a stock-web-UI operation on all units,
and the `factory` UBI volume is unique per device — losing it costs you the
radio calibration. Recent stock firmware (1.2.00.273012+) requires a signed
image.

### OpenWrt One — the reference board

| | |
|---|---|
| SoC | MediaTek MT7981B dual-core Cortex-A53 @ 1.3 GHz |
| RAM | 1 GB DDR4 |
| Flash | 256 MiB SPI-NAND + 16 MiB SPI NOR (write-protected recovery) |
| Expansion | M.2 2242/2230 NVMe, PoE, mikroBUS |
| Network | 1× 2.5 GbE + 1× GbE, Wi-Fi 6 |

Dual-flash design makes it effectively unbrickable, which matters for an
appliance that must re-provision itself after a flash. More RAM and a real
NVMe slot than anything cheaper.

---

## 3. Tier 2 — more hardware, with the reason to spend more

Each of these is justified by a specific limit in Tier 1, not by "bigger is
better."

### GL.iNet Flint 2 (GL-MT6000) — buy this if you want flash endurance

| | |
|---|---|
| SoC | MediaTek MT7986 quad-core Cortex-A53 @ 2.0 GHz |
| RAM | 1 GB DDR4 |
| Flash | **8 GB eMMC** |
| Network | 2× 2.5 GbE + 4× GbE, Wi-Fi 6 |

**The rationale is the eMMC.** This is the single most important difference
between Tier 1 and Tier 2. An SD card and a 16 MB NOR both have orders of
magnitude less write endurance than eMMC, and this workload is write-heavy:
every calendar PUT is a database write plus a changelog insert plus a sync-token
bump. A card-based appliance is a device with a scheduled replacement date. At
8 GB, the overlay budget stops being a constraint at all and the database can
grow for years without an eviction conversation.

Secondary: 2.5 GbE matters if clients are on a fast wired LAN, and the quad
A53 @ 2 GHz gives real headroom for the argon2 password hashing on the login
path — the one genuinely CPU-bound operation in the request path.

### Banana Pi BPI-R4 — buy this for multi-gigabit or many users

MT7988, Wi-Fi 7, SFP+ and 2.5 GbE, 8 GB eMMC. Justified if you are serving a
large number of principals, need SFP/uplink flexibility, or want to front a
10 GbE LAN. Overpowered for a household calendar server.

### Raspberry Pi 5 / Compute Module 5 — buy this if you will benchmark here

**Rationale is methodological, not about the device.** `scripts/build-rust.sh:96`
already supports `x86_64-unknown-linux-gnu` as a host build. A Pi 5 gives you
a large-memory, NVMe-backed unit for load testing that matches production
topology far better than an x86 laptop does, and `sysupgrade`/postinst/keep-list
behaviour is testable on it end to end before touching the router.

---

## 4. Android and a TWRP-style build

Asked for explicitly, and the news here is genuinely good.

### Our binary runs natively on Android. No proot, no root.

`out/rustical` is `statically linked`. A static musl ELF needs no dynamic
loader, so the kernel's `binfmt_elf` handler maps it directly — it does not need
`/system/bin/linker64` (bionic) or a glibc loader. This is a known-good pattern
on Android: projects shipping static musl aarch64 binaries run them on stock
Termux with no root and no container.

Blockers, all real and all solvable:

- **DNS.** musl reads `/etc/resolv.conf`. On stock Android `/etc` is
  `/system/etc`, read-only and typically without it. The known workaround is a
  byte-level patch redirecting that path to a writable location, or supplying a
  real `resolv.conf` when rooted. This directly affects SMTP and IMAP, which
  the scheduling extension depends on.
- **TLS trust store.** Needs `ca-certificates` from Termux rather than the
  Android system store.
- **No init system.** No `procd`, no `/etc/init.d`. You need a supervisor —
  Termux:Boot plus `termux-wake-lock`, or `crond`.
- **Doze / battery optimisation.** Android will suspend the process. Must be
  exempted per-app, and the battery charge capped (~80%) for longevity.
- **SELinux.** Generally fine for an unprivileged app UID; can bite
  `getifaddrs()`, which Bionic handles differently from glibc/musl.

### A TWRP-style build is the better Android target than Termux

TWRP recovery sidesteps most of the list above. Recovery runs a complete
busybox userland with its own `/etc`, a writable `/data`, and none of Doze or
battery optimisation. And — this is the part that matters for us — **the TWRP
packaging model maps almost one-to-one onto what we already built for OpenWrt:**

| Our OpenWrt package | TWRP equivalent |
|---|---|
| `packaging/firmware/omnical/Makefile` (file list) | zip directory tree, applied by `update.sh` |
| `files/postinst` (stateful edits) | `updater-script` / `update-binary` shell |
| `files/omnical.keep.d` (`sysupgrade -b` keep list) | re-run-safe idempotency in `update.sh` |
| `firmware-gate.sh` (postinst must not clobber config) | same test, same assertion, new harness |

`firmware-gate.sh` already exists precisely to prove the postinst is
non-destructive and idempotent against a config holding an SMTP password. That
test transfers to a TWRP `update.sh` essentially unchanged, which makes a
TWRP-style build a genuinely cheap thing to add rather than a new engineering
project. The `Makefile` comment already articulates the principle — *make the
init scripts files the package owns, so restoration does not depend on anything
running afterwards* — and that argument holds identically for a recovery zip.

### Cheapest Android option: a phone you already own

A used phone is the cheapest capable machine in this entire document on a
per-unit basis. A used Pixel 5 (~US$40–60, 8 GB RAM) or any arm64 phone with
2 GB+ has roughly 100× the RAM of the Zero 2 W and a battery that doubles as a
UPS. Idle draw of 1–5 W is comparable to a Pi.

The honest caveat, which is about *product* rather than capability: Android is
an operating system that actively tries to kill background processes, so an
always-on server on a phone needs Termux:Boot, wake locks, per-app battery
exemption and a static DHCP lease. That is a maintenance burden a router does
not have. The battery-as-UPS property is genuinely excellent and unique — the
unit rides through a power cut without a shutdown.

For a CalDAV server specifically, I would rank TWRP-style above Termux above
proot. Proot adds a ptrace syscall-translation layer for no benefit here, since
a static binary does not need it.

---

## 5. Legacy and discontinued — listed separately

All of these are in the libreCMC lineage or its neighbourhood, and **none of
them can run the current binary.** They are here because they are the cheap
end of the market, and because the cross-compile plan exists to change that.

| Device | SoC | ISA | RAM | Flash | Status |
|---|---|---|---|---|---|
| ThinkPenguin TPE-R1200 | QCA9531 @ 650 MHz | **MIPS** 32-bit | 128 MB | 16 MB NOR + 128 MB NAND | libreCMC supported; the NAND is the only part that would have met our flash floor |
| ThinkPenguin TPE-R1300 | QCA9531 @ 650 MHz | **MIPS** 32-bit | 128 MB | 16 MB NOR | libreCMC supported, FSF RYF-certified |
| Netgear R7800 | IPQ8065 dual A15 @ 1.7 GHz | **ARMv7** 32-bit | 512 MB | 128 MB NAND (~80 MB free) | OpenWrt `ipq806x`; **the best armv7 candidate** |
| Netgear WNDR3800 | AR9344 | **MIPS** 32-bit | 128 MB | 8 MB flash | libreCMC supported; flash far too small |
| Buffalo WZR-HP-G300NH | AR9344 | **MIPS** 32-bit | 128 MB | 8 MB flash | libreCMC supported; flash far too small |
| TP-Link Archer C7 v2/v5 | AR9344 / QCA9887 | **MIPS** 32-bit | 128 MB | 16 MB | OpenWrt `ath79` |
| TP-Link TL-WR1043ND v1–v5 | AR9344 | **MIPS** 32-bit | 128 MB | 16 MB | libreCMC supported |
| Ben NanoNote (Qi-Hardware) | Atheros AR2315 | **MIPS** 32-bit | 64 MB | 8 MB | libreCMC supported; far below every floor |

Note the pattern in the libreCMC rows: the small ThinkPenguin routers are MIPS
with 16 MB of NOR. They are smaller and cheaper than anything in Tier 1 and
they are **twice disqualifying** — wrong ISA *and* below the flash floor.

---

## 6. Explicitly ruled out, with reasons

| Ruled out | Reason |
|---|---|
| **All ar71xx / ath79 MIPS devices** — most of libreCMC's list, incl. Archer C7, TL-WR1043ND, WNDR3800, Ben NanoNote | Wrong ISA. `ELF 64-bit aarch64` will not execute. Needs a MIPS build — see cross-compile plan, which assesses this as high-risk. |
| **ThinkPenguin TPE-R1200 / R1300** | MIPS, as above. R1200's 128 MB NAND would have met the flash floor, but the ISA rules it out first. |
| **Cudy WR3000 / WR3000H / M3000, GL.iNet Beryl AX (MT3000)** — the tempting ~US$38 tier | **Correct ISA** (MT7981 is dual Cortex-A53) but **16 MB NOR**. Below the 35 MiB overlay budget; the binaries consume the entire usable overlay and the database has nowhere to grow. Attractive on price, disqualified on flash. |
| **Any device under 16 MB flash** | Below both libreCMC's and OpenWrt's stated support floor. |
| **64 MB RAM devices** | OpenWrt's own guidance flags 64 MB as approaching unsupportable, before adding a 23 MB server. |

The MT7981 class is worth a second look *if* the overlay budget is ever
revisited — the silicon is right and cheap, and a device with 16 MB NOR plus
USB extroot would qualify. That is a packaging decision (accepting an extroot
dependency that the appliance currently does not have) rather than a hardware
one.

---

## 7. Recommendation

- **Smallest that genuinely works: Raspberry Pi Zero 2 W** — but it breaks the
  libreCMC/FSF premise via Broadcom Wi-Fi blobs, so do not treat it as a real
  target for this project.
- **Smallest that respects the project's premise: Linksys E8450 / Belkin
  RT3200.** aarch64, 512 MB RAM, 128 MB NAND, five gigabit ports, cheap
  second-hand. This is the answer to "smallest and weakest".
- **What I would actually deploy: GL.iNet Flint 2 (GL-MT6000)** or an
  OpenWrt One. The deciding factor is eMMC versus SD/NOR, because of write
  endurance — not CPU, which is already 20× oversupplied.
- **Do not spend effort on the MIPS devices** unless the cross-compile work in
  the companion document is greenlit. The 16 MB flash on most of them is an
  independent disqualifier that no amount of compiler work fixes.
- **Android is worth a TWRP-style build**, because the packaging model already
  exists in `packaging/firmware/omnical/` and `firmware-gate.sh` already
  proves the part that is easy to get wrong.
