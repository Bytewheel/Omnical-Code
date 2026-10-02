//! Runtime interface address discovery.
//!
//! The original deployment hard-coded the WAN address — `WAN_IF=eth0`,
//! `WAN_IP=192.168.1.21` — in `router/etc/init.d/dav-tls`, and **refused to
//! start** when the live address differed:
//!
//! ```sh
//! cur_ip="$(ip -4 -o addr show dev "$WAN_IF" | awk '{split($4,a,"/"); print a[1]; exit}')"
//! [ "$cur_ip" = "$WAN_IP" ] || return 1
//! ```
//!
//! That check is correct on a router whose WAN address is static, and wrong on
//! any unit whose WAN address is assigned. A retail box behind DHCP gets a
//! different lease after a reboot, a WAN flap, or a power cycle upstream, and
//! the service then refuses to start until somebody edits the file. The plans
//! name this as the one place the appliance needs real new work
//! (`PLAN_DEPLOYMENTS.md §9.5`), and it is what this module replaces.
//!
//! Two properties matter more than the mechanism:
//!
//! * **A wrong interface must fail loudly.** Binding to some *other* adapter
//!   that happens to carry a routable address is worse than not starting: it
//!   looks healthy and is unreachable. So discovery never falls back to "any
//!   address" — it resolves the named interface or it errors, and the error
//!   names the interfaces it did see.
//! * **A link-local address is not a WAN address.** `169.254.0.0/16` means DHCP
//!   has not completed. Binding there would reproduce the original bug in a
//!   new form: a listening socket that never receives a forwarded connection.

use std::{
    ffi::CStr,
    fmt,
    net::{Ipv4Addr, SocketAddr},
    os::raw::c_char,
};

/// One IPv4 address observed on one interface.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Candidate {
    pub iface: String,
    pub ip: Ipv4Addr,
}

/// Why a `--discover` spec could not be parsed, or an interface not resolved.
///
/// Carries the operator-facing text in the variant rather than formatting it at
/// the raise site, so that every caller produces the same message and the tests
/// can assert on the message a field failure will actually print.
// No `Eq`, and deliberately no `Clone`: `EnumerationFailed` carries an
// `io::Error`, which is neither. The variants a caller matches on in tests all
// compare by their own fields, so the tests pattern-match rather than assert
// whole-enum equality — which also means a new variant cannot silently break an
// exhaustive match in a test that was only ever asserting on one case.
#[derive(Debug)]
pub enum DiscoverError {
    /// `--discover` was not `interface:port`.
    BadSpec { spec: String, reason: &'static str },
    /// `getifaddrs` itself failed. Essentially never happens; the libc wrapper
    /// allocates, so this is OOM in practice.
    EnumerationFailed { source: std::io::Error },
    /// The named interface does not exist, or exists with no IPv4 address.
    NoSuchInterface {
        iface: String,
        available: Vec<String>,
    },
    /// The interface exists but has no address usable as a WAN address.
    NoUsableAddress { iface: String, found: Vec<Ipv4Addr> },
}

impl fmt::Display for DiscoverError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BadSpec { spec, reason } => {
                write!(
                    f,
                    "invalid --discover '{spec}': {reason} (want <interface>:<port>, e.g. eth0:8443)"
                )
            }
            Self::EnumerationFailed { source } => {
                write!(f, "could not enumerate interfaces: {source}")
            }
            Self::NoSuchInterface { iface, available } if available.is_empty() => {
                write!(
                    f,
                    "interface '{iface}' not found, and no interface has an IPv4 address"
                )
            }
            Self::NoSuchInterface { iface, available } => {
                write!(
                    f,
                    "interface '{iface}' not found or has no IPv4 address; interfaces with one: {}",
                    available.join(", ")
                )
            }
            Self::NoUsableAddress { iface, found } if found.iter().all(Ipv4Addr::is_link_local) => {
                write!(
                    f,
                    "interface '{iface}' has only link-local addresses ({}); no DHCP lease yet — waiting for the lease is correct, binding here is not",
                    join_ips(found)
                )
            }
            Self::NoUsableAddress { iface, found } => {
                write!(
                    f,
                    "interface '{iface}' has no usable IPv4 address (found {})",
                    join_ips(found)
                )
            }
        }
    }
}

impl std::error::Error for DiscoverError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::EnumerationFailed { source } => Some(source),
            _ => None,
        }
    }
}

fn join_ips(ips: &[Ipv4Addr]) -> String {
    ips.iter()
        .map(Ipv4Addr::to_string)
        .collect::<Vec<_>>()
        .join(", ")
}

/// Parse a `--discover` value into `(interface, port)`.
///
/// Split on the **last** colon: a Linux interface name is capped at 15
/// characters and cannot contain a colon, so the trailing component is
/// unambiguously the port even for a name a human mistypes as `eth:0`.
pub fn parse_spec(spec: &str) -> Result<(String, u16), DiscoverError> {
    let bad = |reason| DiscoverError::BadSpec {
        spec: spec.to_string(),
        reason,
    };

    let (iface, port) = spec.rsplit_once(':').ok_or_else(|| bad("no port given"))?;
    if iface.is_empty() {
        return Err(bad("empty interface name"));
    }
    let port: u32 = port.parse().map_err(|_| bad("port is not a number"))?;
    let port = u16::try_from(port).map_err(|_| bad("port is out of range (0-65535)"))?;
    if port == 0 {
        return Err(bad("port 0 is not a listenable port"));
    }
    Ok((iface.to_string(), port))
}

/// Whether an address can serve as a public listener address.
///
/// Loopback is excluded because `--discover` names a *WAN* interface; a caller
/// that wants loopback already has `--listen 127.0.0.1:…` and this path would
/// only ever get in its way. Link-local is excluded because it is the shape a
/// not-yet-leased DHCP interface wears, and silently binding it converts a
/// visible "cannot start" into an invisible "nothing arrives".
fn is_usable(ip: Ipv4Addr) -> bool {
    !ip.is_loopback() && !ip.is_link_local()
}

/// Choose the address to bind to for `iface`.
///
/// Split out from [`observe`] so the selection rules — the part with judgement
/// in it — are testable without needing a network namespace or a DHCP server.
///
/// The first usable address wins. `getifaddrs` returns a stable order for a
/// given kernel state, and when an interface carries several global addresses
/// they are all equally correct listeners; picking the first avoids inventing a
/// selection policy with no requirement behind it.
pub fn select(candidates: &[Candidate], iface: &str) -> Result<Ipv4Addr, DiscoverError> {
    let on_iface: Vec<&Candidate> = candidates.iter().filter(|c| c.iface == iface).collect();

    if on_iface.is_empty() {
        let mut available: Vec<String> = candidates.iter().map(|c| c.iface.clone()).collect();
        available.sort();
        available.dedup();
        return Err(DiscoverError::NoSuchInterface {
            iface: iface.to_string(),
            available,
        });
    }

    if let Some(found) = on_iface.iter().find(|c| is_usable(c.ip)) {
        return Ok(found.ip);
    }

    let mut found: Vec<Ipv4Addr> = on_iface.iter().map(|c| c.ip).collect();
    found.sort();
    Err(DiscoverError::NoUsableAddress {
        iface: iface.to_string(),
        found,
    })
}

/// RAII wrapper so an early return cannot leak the `getifaddrs` list.
struct IfAddrs(*mut libc::ifaddrs);

impl IfAddrs {
    fn head(&self) -> *mut libc::ifaddrs {
        self.0
    }
}

impl Drop for IfAddrs {
    fn drop(&mut self) {
        unsafe { libc::freeifaddrs(self.0) };
    }
}

/// Observe every IPv4 address currently configured on the system.
///
/// `getifaddrs` rather than parsing `/proc/net/*` or shelling out to `ip`: it is
/// in libc, which this crate already links, so it adds no dependency and no
/// process spawn — and this runs once per listener at startup, plus once per
/// rebind check thereafter.
pub fn observe() -> Result<Vec<Candidate>, DiscoverError> {
    let mut head: *mut libc::ifaddrs = std::ptr::null_mut();
    // SAFETY: `head` is a valid out-pointer; getifaddrs writes a list head or
    // returns non-zero and leaves it untouched. On success the list is owned by
    // the kernel until `freeifaddrs`, which `IfAddrs` guarantees on drop.
    let rc = unsafe { libc::getifaddrs(&mut head) };
    if rc != 0 {
        return Err(DiscoverError::EnumerationFailed {
            source: std::io::Error::last_os_error(),
        });
    }
    let list = IfAddrs(head);

    let mut candidates = Vec::new();
    let mut cursor = list.head();
    while !cursor.is_null() {
        // SAFETY: `cursor` is non-null and is a node of the list `list` owns,
        // which stays alive and unmodified until `list` is dropped at the end of
        // this function. Reading the three fields we need is within the node's
        // contract.
        let (name_ptr, addr_ptr, next) = unsafe {
            let node = &*cursor;
            (node.ifa_name, node.ifa_addr, node.ifa_next)
        };

        if !name_ptr.is_null() && !addr_ptr.is_null() {
            // SAFETY: getifaddrs sets ifa_name to a NUL-terminated C string and
            // ifa_addr to a sockaddr of the family sa_family names.
            let (family, ipv4) = unsafe { ((*addr_ptr).sa_family, sockaddr_ipv4(addr_ptr)) };
            if family == libc::AF_INET as libc::sa_family_t
                && let Some(ip) = ipv4
            {
                // SAFETY: as above, ifa_name is NUL-terminated.
                let iface = unsafe { CStr::from_ptr(name_ptr as *const c_char) }
                    .to_string_lossy()
                    .into_owned();
                candidates.push(Candidate { iface, ip });
            }
        }
        cursor = next;
    }
    Ok(candidates)
}

/// Read the IPv4 address out of a `sockaddr` known to be `AF_INET`.
///
/// # Safety
///
/// `addr` must point to a `sockaddr` whose `sa_family` is `AF_INET`.
unsafe fn sockaddr_ipv4(addr: *mut libc::sockaddr) -> Option<Ipv4Addr> {
    // SAFETY: the caller guarantees an AF_INET sockaddr, whose concrete type is
    // sockaddr_in on every target this builds for (linux-gnu, linux-musl).
    let sin = unsafe { &*(addr as *const libc::sockaddr_in) };
    // sin_addr.s_addr is network byte order; from_be puts it back into host
    // order, which is what Ipv4Addr::from expects.
    Some(Ipv4Addr::from(u32::from_be(sin.sin_addr.s_addr)))
}

/// Resolve `iface` to the address to listen on, right now.
pub fn resolve(iface: &str) -> Result<Ipv4Addr, DiscoverError> {
    select(&observe()?, iface)
}

/// A listen endpoint: either a fixed address, or an interface to resolve.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Listen {
    /// `--listen <ip:port>` — used exactly as written, never re-resolved.
    Fixed(SocketAddr),
    /// `--discover <interface:port>` — resolved at startup and re-checked while
    /// running, so a lease change rebinds instead of going silently deaf.
    Discovered { iface: String, port: u16 },
}

impl Listen {
    /// The address this endpoint currently resolves to.
    ///
    /// `Fixed` is always `Ok`. `Discovered` re-reads the interface, which is the
    /// point: this is called on the rebind tick, not just at startup.
    pub fn current_addr(&self) -> Result<SocketAddr, DiscoverError> {
        match self {
            Self::Fixed(addr) => Ok(*addr),
            Self::Discovered { iface, port } => {
                resolve(iface).map(|ip| SocketAddr::new(ip.into(), *port))
            }
        }
    }

    /// One-line description for the startup log.
    pub fn describe(&self) -> String {
        match self {
            Self::Fixed(addr) => format!("{addr}"),
            Self::Discovered { iface, port } => format!("{iface}:{port} (discovered)"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn c(iface: &str, ip: &str) -> Candidate {
        Candidate {
            iface: iface.to_string(),
            ip: ip.parse().unwrap(),
        }
    }

    #[test]
    fn parses_interface_and_port() {
        assert_eq!(parse_spec("eth0:8443").unwrap(), ("eth0".to_string(), 8443));
    }

    #[test]
    fn splits_on_the_last_colon_so_a_mistyped_name_is_not_silently_accepted() {
        // `eth:0` is a typo for `eth0`. Splitting on the *first* colon would
        // yield interface `eth`, which does not exist; splitting on the last
        // yields `eth:0`, which also does not exist. Either way it must not be
        // mistaken for a working address, and the parse itself still succeeds —
        // the failure has to surface at resolve time.
        assert_eq!(
            parse_spec("eth:0:8443").unwrap(),
            ("eth:0".to_string(), 8443)
        );
    }

    #[test]
    fn rejects_specs_that_cannot_be_a_listener() {
        for (spec, needle) in [
            ("eth0", "no port given"),
            (":8443", "empty interface name"),
            ("eth0:", "port is not a number"),
            ("eth0:abc", "port is not a number"),
            ("eth0:0", "port 0"),
            ("eth0:99999", "out of range"),
        ] {
            let err = parse_spec(spec).unwrap_err();
            assert!(
                err.to_string().contains(needle),
                "spec {spec:?}: expected {needle:?}, got {err}"
            );
        }
    }

    #[test]
    fn selects_the_address_of_the_named_interface() {
        let found = select(&[c("lo", "127.0.0.1"), c("eth0", "203.0.113.7")], "eth0");
        assert_eq!(found.unwrap(), "203.0.113.7".parse::<Ipv4Addr>().unwrap());
    }

    #[test]
    fn a_global_address_elsewhere_never_satisfies_the_named_interface() {
        // The failure this whole module exists to prevent: eth0 has no lease,
        // wlan0 does, and binding wlan0 would look healthy and be unreachable.
        let err = select(&[c("wlan0", "203.0.113.9")], "eth0").unwrap_err();
        assert!(
            matches!(err, DiscoverError::NoSuchInterface { .. }),
            "got {err:?}"
        );
        assert!(
            err.to_string().contains("wlan0"),
            "error must name what it saw: {err}"
        );
    }

    #[test]
    fn link_local_is_refused_and_says_so_in_dhcp_terms() {
        let err = select(&[c("eth0", "169.254.10.1")], "eth0").unwrap_err();
        assert!(
            matches!(err, DiscoverError::NoUsableAddress { .. }),
            "got {err:?}"
        );
        let text = err.to_string();
        assert!(text.contains("link-local"), "{text}");
        assert!(
            text.contains("DHCP"),
            "the operator needs to know it is a lease, not a bug: {text}"
        );
    }

    #[test]
    fn loopback_is_refused_on_a_wan_interface() {
        let err = select(&[c("eth0", "127.0.0.1")], "eth0").unwrap_err();
        assert!(
            matches!(err, DiscoverError::NoUsableAddress { .. }),
            "got {err:?}"
        );
    }

    #[test]
    fn a_usable_address_beats_a_link_local_one_on_the_same_interface() {
        // An interface mid-lease can hold both. The global one is the answer.
        let found = select(
            &[c("eth0", "169.254.10.1"), c("eth0", "198.51.100.4")],
            "eth0",
        );
        assert_eq!(found.unwrap(), "198.51.100.4".parse::<Ipv4Addr>().unwrap());
    }

    #[test]
    fn a_missing_interface_with_no_addresses_anywhere_says_so_plainly() {
        let err = select(&[], "eth0").unwrap_err();
        assert_eq!(
            err.to_string(),
            "interface 'eth0' not found, and no interface has an IPv4 address"
        );
    }

    #[test]
    fn observe_finds_this_machine_loopback() {
        // The one assertion that can hold on any Linux CI box: enumeration
        // works and loopback is present. It does not check a WAN interface,
        // because a test host has no guaranteed one.
        let candidates = observe().unwrap();
        assert!(
            candidates.iter().any(|x| x.ip.is_loopback()),
            "no loopback address observed: {candidates:?}"
        );
    }

    #[test]
    fn current_addr_rejects_a_resolve_that_cannot_work() {
        let listen = Listen::Discovered {
            iface: "definitely-not-an-interface".into(),
            port: 8443,
        };
        assert!(matches!(
            listen.current_addr(),
            Err(DiscoverError::NoSuchInterface { .. })
        ));
    }

    #[test]
    fn a_fixed_listen_is_never_re_resolved() {
        let listen = Listen::Fixed("192.0.2.1:8443".parse().unwrap());
        assert_eq!(listen.current_addr().unwrap().to_string(), "192.0.2.1:8443");
        assert_eq!(listen.describe(), "192.0.2.1:8443");
    }
}
