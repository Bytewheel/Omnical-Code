# Toaster targets

**Status: research and jokes, in roughly equal measure. Nothing has been built,
flashed, or plugged in.**

Commissioned as: "find toasters that could be capable of running it, with
creative flashing."

**The headline finding is negative and worth stating plainly: no toaster's own
SoC can run the Omnical firmware, and none ever will.** The gap is two to three
orders of magnitude on RAM and roughly an order of magnitude on flash. What
*is* real is a documented precedent trail for putting a general-purpose computer
inside a toaster, and — more interestingly — a genuinely feasible way to use
Omnical's own VTODO support to make a toaster a calendar citizen.

---

## 1. Why no toaster SoC qualifies

The requirements from [`DEVICE_TARGETS.md`](DEVICE_TARGETS.md) §1 against the
only SoCs that actually appear in toasters:

| | Omnical needs | ESP32 (classic) | ESP32-C3 | BF7515BM44 (common toaster MCU) |
|---|---|---|---|---|
| ISA | aarch64 | Xtensa LX6, dual @240 MHz | RISC-V single @160 MHz | 8051, 8/12 MHz |
| RAM | **128 MB** | 520 KB SRAM | 400 KB SRAM | 4 KB |
| Flash | **128 MB** | 4 MB | 4 MB | 63 KB code |
| Runs Linux? | yes | no | only via emulation | no |

The closest anyone has come: `uc-rv32ima` boots a real Linux kernel on an
ESP32-C3 using CNLohr's **mini-rv32ima software RISC-V emulator** plus an 8 MB
SPI PSRAM module soldered on. Boot-to-shell takes about 80 seconds. And
`jcmvbkbc/esp32-linux-build` produces genuine Linux + Buildroot rootfs images
for ESP32/ESP32-S3 (Xtensa, uClibc, `esp-hosted` for networking).

Both are real and both are magnificent. Neither is remotely relevant: an
emulated RISC-V core with 8 MB of external PSRAM is not going to run a server
with a 23 MB working set, and our flash budget alone is 35 MiB.

For completeness on the RISC-V angle: `riscv32imac-unknown-none-elf` *is* a
Tier 2 Rust target with a prebuilt std, but it is bare-metal — no hosted
`std`, no Linux. And Espressif's C6/H2 parts are RISC-V, so even a future
generation of toaster silicon would land in the same category as the MIPS row
of the cross-compile plan: Tier 2 std, but no operating system underneath.

**A toaster MCU will never be a server.** The interesting question is what
else it can be.

---

## 2. The precedent trail

I went looking for toasters that *are* computers, and found a genuine lineage.

### NetBSD Toaster, LinuxWorld 2005 — the original

A candy-apple-red toaster built by **Technologic Systems Designs** running
**NetBSD**, demonstrated by Jake Appelbaum and John McCay at the .org
Pavilion. Not a joke and not a PC-case mod — it actually made toast.

Hardware: a **TS-7200** CPU board (ARM, OMAP350) inside, with a 4-line LCD
showing a `NetBSD/toaster` login prompt, a USB keyboard port, **10/100
Ethernet**, and RS232 as the console. The toaster's original boards were
*bypassed* and routed through the CPU board, which took full control; the
burner element itself was switched by a relay on the TS-7200.

This is the load-bearing precedent: a toaster, an ARM board, a network stack,
and a login prompt. It is also worth noting what it was *not* — NetBSD on ARMv5,
not Linux, and the interesting part was the demonstration rather than the
appliance.

### DataToaster 3000, 2024 — the practical template

Alex Meub built a **NAS inside a cheap two-slice toaster**, after being
inspired by a computer he'd seen in a recycling shop. Design specifics:

- x86-64 **ZimaBoard** SBC
- two 3.5-inch hot-swap drive bays where the bread slots were
- the toaster's internals **gutted**; 3D-printed mounts hold the electronics
- the toaster's chrome **dial replaced with a potentiometer** driving a
  "MoodGlow" power LED ("patent-pending")
- cuts made in the steel chassis; a connector replaced

If anyone builds an Omnical toaster, this is the build log to copy. It also
confirms the practical sequence: gut, replace, mount, and treat the toaster as
a sheet-metal enclosure with a mains lead.

### Toaster PC, 2012

A forum build where **the toaster's lever is the power button** and the
temperature dial sets the brightness of internal red LEDs. Crude, and the
comments are worth reading for the exact mood this project provokes.

### Smart toasters, actual products

Multiple ESP32 toaster projects exist and are worth knowing about, mostly for
the hardware rather than the software: `WIFI_Toaster` (ESP32 driving servos
that press the lever and release a hopper trapdoor), `SmartTart` (ESP32,
relay, OLED, accelerometer), and a nicely documented **"Singing Toaster QSV"**
teardown that reverse-engineered an Instant toaster oven's serial protocol and
replaced the front panel with an ESP32-S3 running MicroPython. That last one
includes the actual MCU in a consumer toaster: a **BF7515BM44**, an 8051-core
part with 63 KB of code flash and 4 KB of SRAM.

None of these can host a server. All of them can host a *client*.

---

## 3. The build that would actually work

Drop the premise that the toaster's electronics do anything. There are two
distinct builds, and only the second is interesting.

### Build A — toaster as pure enclosure

Gut everything. Mount an aarch64 SBC where the crumb tray was. Use the toaster's
lever as a power switch. Run the stock appliance image.

**This works and is about thirty minutes of work, but it is just a Pi in a
metal box.** The toaster contributes sheet metal and nothing else, because its
transformer is sized for a 700–1500 W heating element and is useless for a
5 V rail. You will need external power, at which point the toaster has
contributed no electronics whatsoever. Worth doing once, for the joke.

### Build B — a toaster that is *also* a CalDAV server

Keep the heating elements and the mains path intact. Add the SBC in the
mechanical dead space, with its own low-voltage supply. This is the version
that earns its keep, because the toaster is still a working toaster.

Omnical fits this better than a generic Linux server does, for reasons that are
not accidental:

- **It is designed to be headless.** `procd` init scripts, a hardware watchdog
  (`router/usr/bin/rustical-watchdog`), a self-provisioning `sysupgrade` image,
  and no dependency on a local console. A toaster has no display and no
  keyboard. Neither does the appliance care.
- **TLS is already solved.** `dav-tls` terminates TLS and is a separate
  process, so there is no need for a local admin UI to get the server running.
- **The setup wizard already exists.** On a factory flash with no
  `config.toml`, the server serves a LAN setup wizard — reachable from a phone
  on the same network. A toaster does not need a screen to be configured.

Practical constraints, which are real engineering rather than scruples:

- **Thermal.** A toaster cavity is an oven when it is toasting, and the
  transformer is a heat source even when it is not. The appliance's 23 MB
  working set generates very little heat — far less than the two 3.5-inch
  HDDs in the DataToaster — but an SBC still needs airflow, and a sealed
  stainless cavity does not provide it. Expect to vent.
- **Isolation.** Mains wiring stays where it is and is not modified. The SBC
  runs off a low-voltage brick that is outside the shell. This is the correct
  boundary to draw and it is also what the DataToaster did.
- **Clearance.** Nichrome elements run at hundreds of degrees Celsius. The board
  wants to be in the base, below the element cage, not beside it.

---

## 4. The actually-good idea: the toaster as a CalDAV *client*

This is the one I would actually build, and it needs no enclosure surgery.

Omnical implements **CalDAV with VTODO support**. So an ESP32 in a toaster can
authenticate as a principal and *write tasks into the calendar*. The toaster
becomes a calendar citizen rather than a calendar server:

- The ESP32 subscribes to a VTODO collection on the Omnical appliance.
- It posts a task when the toaster is armed: **"Take the bread out — 3 min."**
- Or the reverse: the toaster polls for a task and toasts when you schedule
  breakfast in your calendar app. Schedule a 07:00 VTODO titled "toast", and
  the toaster toasts at 07:00.
- On completion it PUTs back a completion status, so the task in the client
  ticks itself off.

That is a real, standards-compliant, useful thing to build, it uses the toaster
hardware for its actual purpose, and it needs only what the ESP32 projects
above already demonstrate: relay control of the element and a serial or Wi-Fi
link. It also has a pleasant symmetry — the appliance serves the calendar, the
toaster *uses* it, and neither pretends to be the other.

If Omnical ever wants a demo that shows what a CalDAV server is *for*, this is
it.

---

## 5. The Turris 1.x, and a note on PowerPC

**Correction to an assumption I expected to confirm:** I went looking for a
shipping toaster-shaped network appliance and did not find one. The closest
real candidate is **Turris 1.0 / 1.1** (CZ.NIC) — OpenWrt-native, documented
under the CERN Open Hardware Licence, 5× gigabit LAN, and a well-documented
hardware design. But I could find no source describing its case as toaster-like;
it is a 212 × 175 × 50 mm box with three external antennas, and I am not going
to claim a form factor I could not verify.

It is worth a line for a different reason. The Turris 1.x runs a
**Freescale P2020, dual-core PowerPC @ 1.2 GHz**, with 2 GB DDR3 in a SO-DIMM
slot and 256 MB NAND + 16 MB NOR. That is enormously over-provisioned for this
workload, and it would run the appliance happily *if we had a PowerPC build* —
which we do not.

Worth noting for the cross-compile plan: **`powerpc64-unknown-linux-musl` is a
Tier 2 target with a prebuilt std**, the same tier as the armv7 target
recommended there. So PowerPC would be roughly the same amount of work as
ARMv7, not the Tier-3 nightmare MIPS is. It unlocks essentially nothing that
ARMv7 does not — the Turris is discontinued and the OpenWrt `powerpc` target is
narrow — so it is not worth doing. Recording it only so the option space is
complete.

---

## 6. Summary

| Toaster class | Could it run Omnical? | Notes |
|---|---|---|
| ESP32 / ESP32-C3 toasters | **No** | 400 KB–520 KB RAM vs 128 MB required; 4 MB flash vs 35 MiB budget. Linux only via software emulation. |
| BF7515BM44 and similar 8051 toaster MCUs | **No** | 4 KB SRAM. Not a Linux candidate at any point in the future. |
| Toaster-shaped router (Turris 1.x) | **Not without a new ISA** | PowerPC; would need a `powerpc64` build, same effort class as armv7, unlocks nothing extra. |
| An aarch64 SBC *inside* a toaster | **Yes** | Build A (enclosure) or Build B (working toaster + server). DataToaster 3000 is the template. |
| An ESP32 toaster as a CalDAV **client** | **Yes, and it is the good idea** | VTODO-based; uses the toaster as a toaster. |

Recommendation: build the VTODO toaster client before any toaster that hosts a
server. It is smaller, it does not involve mains-voltage surgery, it exercises a
part of the product (`VTODO`) that is otherwise hard to demonstrate, and it
makes the toaster do the one thing toasters are for.
