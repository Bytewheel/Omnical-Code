# PLAN: Rustical Binary Size Optimization

## Baseline

| Metric | Value |
|--------|-------|
| Binary | `out/rustical` (aarch64-unknown-linux-musl, static) |
| Current size | 29.0 MiB (30,386,704 bytes) |
| Stripped? | Yes (`CARGO_PROFILE_RELEASE_STRIP=true`) |
| Debug sections? | No (`.comment` section only) |
| Overlay budget | 35 MiB (build-rust.sh `RUSTICAL_BUDGET`) |
| Runtime penalty budget | ~0 (CalDAV server, latency-sensitive) |

---

## Phase 1: Profile Optimizations (zero code changes)

**Expected reduction: ~30-40% (29 MiB → ~17-20 MiB)**

These are pure Cargo profile settings — no dependency or source changes, no risk of breakage.

### 1.1 Add `[profile.release]` to root `Cargo.toml`

```toml
[profile.release]
opt-level = "z"     # optimize aggressively for binary size
lto = true          # link-time optimization: dead code elimination + cross-crate inlining
panic = "abort"     # drop unwinding tables (_Unwind_* infrastructure)
codegen-units = 1   # single codegen unit = better optimization across crate boundaries
strip = true        # strip symbols (already enabled via env var; move here for clarity)
```

**Why each setting helps:**
- `opt-level = "z"`: LLVM's size-focused optimizer. Trades ~5-15% throughput for significantly smaller code. For a CalDAV server (I/O-bound, not CPU-bound), this is free.
- `lto = true`: Enables cross-crate dead code elimination. Without LTO, each crate is optimized independently — unreachable code in a dependency survives into the final binary. With LTO, the linker sees the whole program and strips everything unused.
- `panic = "abort"`: Removes the unwind tables and landing pad infrastructure. On musl targets this is especially effective because musl's unwind support is minimal anyway. Saves ~1-2 MiB.
- `codegen-units = 1`: Forces LLVM to optimize the entire crate as one unit instead of splitting into parallel codegen units. Better optimization passes, smaller output. Trades compile time (slower parallel builds).
- `strip = true`: Already active via env var. Moving to Cargo.toml makes it explicit and removes the env var from build-rust.sh.

### 1.2 Remove `.comment` section

After build, run `llvm-strip --remove-section=.comment` on the binary. This section contains compiler version strings (~4 KiB). `llvm-strip` is available at `/usr/lib/llvm/22/bin/llvm-strip`.

### 1.3 Update `scripts/build-rust.sh`

- Remove `export CARGO_PROFILE_RELEASE_STRIP=true` (now in Cargo.toml)
- Add post-build `llvm-strip --remove-section=.comment` step
- Update size gate budget if needed (will likely never hit 35 MiB after these changes)

### 1.4 Verification

```bash
scripts/build-rust.sh   # rebuild
ls -lh out/rustical      # should be ~17-20 MiB
file out/rustical        # confirm still aarch64 ELF
scripts/deploy.sh        # deploy and smoke test
```

---

## Phase 2: Dependency Trims (minor Cargo.toml changes)

**Expected additional reduction: ~1-3 MiB (cumulative with Phase 1)**

These are workspace dependency feature trims. Each has been audited against the full codebase — only confirmed-unused features are removed.

### 2.1 Tokio: drop `full`, use explicit features

**Current** (Cargo.toml line 72-78):
```toml
tokio = { version = "1.53", features = [
  "net", "tracing", "macros", "rt-multi-thread", "full",
] }
```

**Change to:**
```toml
tokio = { version = "1.53", features = [
  "rt-multi-thread", "macros", "net", "io-util", "sync",
  "time", "signal", "tracing",
] }
```

**Rationale:** The `full` meta-feature enables 12 sub-features. Codebase audit found usage of only 8. Dropping `fs`, `process`, `io-std` (unused) and the redundant explicit entries (`net`, `macros`, `rt-multi-thread` are already in `full`). The `tracing` feature is NOT in `full` and must be kept.

**Risk:** Low. These features are additive opt-ins; removing unused ones has no effect on code that doesn't call the corresponding APIs. All 14 workspace crates reference `tokio.workspace = true`.

### 2.2 Reqwest: trim default features

**Current** (Cargo.toml line 143):
```toml
reqwest = { version = "0.13" }
```

**Change to:**
```toml
reqwest = { version = "0.13", default-features = false, features = ["rustls-tls", "http2", "charset"] }
```

**Dev-dependencies** (line 166) re-add what tests need:
```toml
reqwest = { workspace = true, features = ["cookies", "form"] }
```

**Rationale:** Production code only uses `Client::builder()`, `Url`, `Request::new()`, and HTTPS connect. It does NOT use: cookies, gzip, brotli, deflate, socks, multipart, streaming, or blocking. The `cookies` feature alone pulls in ~4 extra crates (`cookie`, `cookie_store`, etc.). The `oidc` crate already declares its own trimmed reqwest 0.12 — this does not conflict.

**Risk:** Low. The dev-dependency override ensures tests still compile. Production code was audited for every reqwest API call.

### 2.3 Anyhow: remove backtrace

**Current** (Cargo.toml line 62):
```toml
anyhow = { version = "1.0", features = ["backtrace"] }
```

**Change to:**
```toml
anyhow = "1.0"
```

**Rationale:** Zero calls to `.backtrace()` or references to the `backtrace` crate anywhere in the codebase. The `backtrace` feature adds the `backtrace` crate as a dependency and enables the backtrace capture path in anyhow::Error — all dead code.

### 2.4 UUID: remove fast-rng

**Current** (Cargo.toml line 58):
```toml
uuid = { version = "1.19", features = ["v4", "fast-rng"] }
```

**Change to:**
```toml
uuid = { version = "1.19", features = ["v4"] }
```

**Rationale:** `fast-rng` switches the RNG backend for WASM performance. On Linux aarch64 it provides no benefit — the standard `v4` RNG already uses `getrandom` which is fast on Linux. Saves a minor amount of compile-time configuration.

### 2.5 Serde: remove rc and serde_derive

**Current** (Cargo.toml line 63):
```toml
serde = { version = "1.0", features = ["serde_derive", "derive", "rc"] }
```

**Change to:**
```toml
serde = { version = "1.0", features = ["derive"] }
```

**Rationale:**
- `rc`: Enables `Serialize`/`Deserialize` for `Arc<T>` and `Rc<T>`. Zero usage found — all `Arc` usage is for trait objects and config structs that don't derive serde traits.
- `serde_derive`: Redundant with `derive` (both enable the proc macro). The `derive` feature is the modern name.

### 2.6 Derive_more: remove try_into

**Current** (Cargo.toml line 90-97):
```toml
derive_more = { version = "2.1", features = [
  "from", "try_into", "into", "deref", "constructor", "display",
] }
```

**Change to:**
```toml
derive_more = { version = "2.1", features = [
  "from", "into", "deref", "constructor", "display",
] }
```

**Rationale:** Zero `#[derive(TryInto)]` or `use derive_more::TryInto` in the codebase. The 3 `try_into()` calls found use the standard library `TryFrom` trait, not derive_more.

### 2.7 Verification

After all Phase 2 changes:
```bash
cargo check --target aarch64-unknown-linux-musl   # ensure compilation
cargo test                                          # ensure tests pass
scripts/build-rust.sh                              # full cross-build
scripts/deploy.sh                                  # deploy and smoke test
```

---

## Phase 3: Aggressive Compression (post-build)

**Expected additional reduction: ~40-60% (on top of Phase 1+2)**

### 3.1 Install UPX

```bash
# On the build host
sudo apt install upx-ucl   # Debian/Ubuntu
# or
brew install upx            # macOS
```

### 3.2 Add UPX compression to build script

After the `llvm-strip` step in `build-rust.sh`:

```bash
upx --lzma "$OUT/rustical"
```

**Why `--lzma`:** UPX offers several compression methods. `--lzma` gives the best compression ratio with slightly slower decompression. For a server binary that starts once and runs for weeks, startup time impact (~50-200ms) is negligible.

### 3.3 Verification

```bash
ls -lh out/rustical          # expect ~8-12 MiB
file out/rustical            # still ELF aarch64
scripts/deploy.sh            # deploy, verify startup + health check
# Measure startup time:
time ssh router '/etc/init.d/rustical stop && /etc/init.d/rustical start && /usr/sbin/rustical --config-file /etc/rustical/config.toml health'
```

### 3.4 Risk assessment

- UPX decompression adds ~50-200ms to startup. Acceptable for a long-running service.
- UPX can occasionally cause issues with self-modifying code or JIT — not applicable here (pure Rust, no JIT).
- If UPX causes problems on the router, it's trivially reversible (just remove the `upx` line).

---

## Cumulative Size Projections

| Phase | Estimated Size | Reduction |
|-------|---------------|-----------|
| Baseline | 29.0 MiB | — |
| Phase 1 (profile) | ~17-20 MiB | ~30-40% |
| Phase 2 (deps) | ~15-18 MiB | ~5-10% additional |
| Phase 3 (upx) | ~8-12 MiB | ~40-60% additional |

Even Phase 1 alone brings the binary well under the 35 MiB budget with massive headroom. Phase 3 can bring it under 10 MiB.

---

## Rollback Plan

Each phase is independently revertible:
- **Phase 1:** Remove `[profile.release]` from Cargo.toml, restore env var in build-rust.sh
- **Phase 2:** Revert individual dependency lines in Cargo.toml (each is a one-line change)
- **Phase 3:** Remove `upx` line from build-rust.sh

All changes are in Cargo.toml and build scripts — no Rust source code is modified.

---

## File Changes Summary

| File | Change |
|------|--------|
| `rustical/Cargo.toml` | Add `[profile.release]`; trim 6 dependency feature lists |
| `scripts/build-rust.sh` | Remove env var; add `llvm-strip` + optional `upx` steps |
