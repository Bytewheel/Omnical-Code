//! dav-tls — a minimal TLS-terminating TCP tunnel for RustiCal on libreCMC.
//!
//! Listens for TLS connections (ALPN restricted to `http/1.1`), performs the
//! TLS handshake, then splices plaintext bytes bidirectionally to an upstream
//! plain-TCP socket.  No HTTP parsing of TLS traffic: PROPFIND/REPORT/PUT and
//! WebSocket upgrades (WebDAV Push) pass through untouched.  Certificates are
//! reloaded on service restart (renewal flow restarts this service via
//! procd).
//!
//! Plaintext HTTP on the TLS port (browsers default to http:// for bare
//! host:port entries, internet scanners probe with plain GET) is answered
//! with a 301 redirect to the https:// equivalent instead of a fatal TLS
//! alert + close.
//!
//! `--discover <interface>:<port>` binds to whatever IPv4 address an interface
//! holds, resolved at startup and re-checked on an interval, so a DHCP-assigned
//! WAN works without the address being pinned in a file. `--listen <ip:port>`
//! keeps working exactly as before for a static address. See `discover`.

mod discover;

use std::{
    ffi::CString,
    fs::File,
    io::BufReader,
    net::SocketAddr,
    process,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use rustls_pki_types::{CertificateDer, PrivateKeyDer};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt, copy},
    net::{TcpListener, TcpStream},
    sync::Notify,
    task,
};
use tokio_rustls::{TlsAcceptor, rustls::ServerConfig};

const MAX_CONNS: usize = 1024;
const DRAIN_TIMEOUT: Duration = Duration::from_secs(5);
const ALPN_HTTP1: &[u8] = b"http/1.1";
const MAX_HEAD: usize = 8 * 1024;
const REDIRECT_READ_TIMEOUT: Duration = Duration::from_secs(5);
const HTTP_METHODS: [&[u8]; 8] = [
    b"GET ",
    b"HEAD ",
    b"POST ",
    b"PUT ",
    b"DELETE ",
    b"OPTIONS ",
    b"PATCH ",
    b"TRACE ",
];

/// How often a `--discover` listener re-reads its interface.
///
/// A DHCP lease on a retail unit can change on a reboot, a WAN flap, or an
/// upstream lease expiry — minutes to hours, and on no schedule we control.
/// Thirty seconds is well inside the slowest plausible change, and costs one
/// `getifaddrs` per discovered listener per tick: a linked-list walk over a
/// handful of interfaces.
///
/// Re-resolving is the difference between a listener that follows its address
/// and one that silently stops receiving. A bound socket keeps the address it
/// was given for the life of the process, so without this a lease change turns
/// the public front end into a black hole while the process still looks healthy.
const REDISCOVER_INTERVAL: Duration = Duration::from_secs(30);

/// How long before a failed rebind is retried.
///
/// Retry rather than exit: the usual cause of a failed resolve is an interface
/// that is mid-lease, and that resolves itself. Exiting would hand a transient
/// DHCP state to procd's `respawn 3600 5 0`, which gives up after five tries in
/// an hour — turning a two-minute DHCP gap into an hour-long outage.
const REBIND_RETRY_INTERVAL: Duration = Duration::from_secs(5);

struct Args {
    listen: Vec<discover::Listen>,
    upstream: SocketAddr,
    cert: String,
    key: String,
    user: Option<String>,
}

const USAGE: &str = "\
dav-tls — minimal rustls TLS tunnel

USAGE:
    dav-tls (--listen <ADDR> | --discover <IFACE:PORT>) [--repeatable] \\
           --upstream <ADDR> --cert <PEM> --key <PEM> [--user <NAME>]

OPTIONS:
    --listen <ip:port>      Address to accept TLS on (repeatable)
    --discover <iface:port> Bind to the interface's current IPv4 address, and
                            re-resolve it while running. Use this for a
                            DHCP-assigned WAN: pinning an address DHCP will
                            move is what made this refuse to start.
    --upstream <ip:port>    Plain TCP address to forward to (e.g. 127.0.0.1:4000)
    --cert <path>           TLS certificate chain (PEM, fullchain)
    --key <path>            TLS private key (PEM)
    --user <name>           Drop privileges to this user after binding
    -h, --help              Show this help

EXAMPLES:
    # Static address — unchanged behaviour
    dav-tls --listen 192.168.1.21:8443 --upstream 127.0.0.1:4000 --cert c.pem --key k.pem

    # DHCP-assigned WAN
    dav-tls --discover eth0:8443 --upstream 127.0.0.1:4000 --cert c.pem --key k.pem
";

fn parse_args() -> Args {
    let mut listen: Vec<discover::Listen> = Vec::new();
    let mut upstream: Option<SocketAddr> = None;
    let mut cert: Option<String> = None;
    let mut key: Option<String> = None;
    let mut user: Option<String> = None;

    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        let mut value = |name: &str| -> String {
            match it.next() {
                Some(v) => v,
                None => {
                    eprintln!("dav-tls: missing value for {name}");
                    process::exit(2);
                }
            }
        };
        match arg.as_str() {
            "--listen" => match value("--listen").parse() {
                Ok(addr) => listen.push(discover::Listen::Fixed(addr)),
                Err(_) => {
                    eprintln!("dav-tls: invalid --listen address");
                    process::exit(2);
                }
            },
            "--discover" => {
                let spec = value("--discover");
                // A malformed spec is a usage error and exits 2. An interface
                // that does not *resolve* is a runtime condition, reported by
                // the listener with a retry, not here — conflating the two
                // would make a typo look like an outage.
                match discover::parse_spec(&spec) {
                    Ok((iface, port)) => listen.push(discover::Listen::Discovered { iface, port }),
                    Err(e) => {
                        eprintln!("dav-tls: {e}");
                        process::exit(2);
                    }
                }
            }
            "--upstream" => match value("--upstream").parse() {
                Ok(addr) => upstream = Some(addr),
                Err(_) => {
                    eprintln!("dav-tls: invalid --upstream address");
                    process::exit(2);
                }
            },
            "--cert" => cert = Some(value("--cert")),
            "--key" => key = Some(value("--key")),
            "--user" => user = Some(value("--user")),
            "-h" | "--help" => {
                print!("{USAGE}");
                process::exit(0);
            }
            other => {
                eprintln!("dav-tls: unknown argument '{other}'\n{USAGE}");
                process::exit(2);
            }
        }
    }

    if listen.is_empty() || upstream.is_none() || cert.is_none() || key.is_none() {
        eprint!("{USAGE}");
        process::exit(2);
    }
    Args {
        listen,
        upstream: upstream.unwrap(),
        cert: cert.unwrap(),
        key: key.unwrap(),
        user,
    }
}

/// Bind a listener with SO_REUSEADDR set before bind (std does not set it
/// on Unix for listeners, and procd restarts must not fail on TIME_WAIT).
fn bind_listener(addr: SocketAddr) -> std::io::Result<TcpListener> {
    let domain = if addr.is_ipv4() {
        socket2::Domain::IPV4
    } else {
        socket2::Domain::IPV6
    };
    let socket = socket2::Socket::new(domain, socket2::Type::STREAM, Some(socket2::Protocol::TCP))?;
    socket.set_reuse_address(true)?;
    socket.set_nonblocking(true)?;
    socket.bind(&addr.into())?;
    socket.listen(1024)?;
    Ok(TcpListener::from_std(std::net::TcpListener::from(socket))?)
}

fn load_server_config(cert_path: &str, key_path: &str) -> Result<ServerConfig, String> {
    let certs_file = File::open(cert_path).map_err(|e| format!("open {cert_path}: {e}"))?;
    let certs: Vec<CertificateDer> = rustls_pemfile::certs(&mut BufReader::new(certs_file))
        .collect::<Result<_, _>>()
        .map_err(|e| format!("parse certificates in {cert_path}: {e}"))?;
    if certs.is_empty() {
        return Err(format!("no certificates found in {cert_path}"));
    }
    let key_file = File::open(key_path).map_err(|e| format!("open {key_path}: {e}"))?;
    let key: PrivateKeyDer = rustls_pemfile::private_key(&mut BufReader::new(key_file))
        .map_err(|e| format!("parse key in {key_path}: {e}"))?
        .ok_or_else(|| format!("no private key found in {key_path}"))?;

    let mut config = ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(certs, key)
        .map_err(|e| format!("bad certificate/key pair: {e}"))?;
    // Offer only http/1.1: every CalDAV/CardDAV client falls back to
    // HTTP/1.1, and HTTP/2 would need h2c-aware handling upstream anyway.
    config.alpn_protocols = vec![ALPN_HTTP1.to_vec()];
    Ok(config)
}

/// Drop root privileges to `user` (looked up by name) after binding.
fn drop_privileges(user: &str) -> Result<(), String> {
    let name = CString::new(user).map_err(|_| "invalid user name".to_string())?;
    unsafe {
        let pw = libc::getpwnam(name.as_ptr());
        if pw.is_null() {
            return Err(format!("getpwnam: no such user '{user}'"));
        }
        let uid = (*pw).pw_uid;
        let gid = (*pw).pw_gid;
        if libc::setgroups(0, std::ptr::null()) != 0 {
            return Err("setgroups failed".to_string());
        }
        if libc::setgid(gid) != 0 {
            return Err("setgid failed".to_string());
        }
        if libc::setuid(uid) != 0 {
            return Err("setuid failed".to_string());
        }
        if libc::getuid() != uid {
            return Err("privilege drop failed sanity check".to_string());
        }
    }
    Ok(())
}

/// Answer a plaintext HTTP request arriving on the TLS port with a 301
/// redirect to its https:// equivalent (Host header + request target
/// preserved, port included in Host kept), then close the connection.
/// Browsers default to http:// for bare `host:port` address-bar entries
/// (https is only the default on 443, and there is no HSTS before a first
/// successful visit) — previously such requests hit the TLS acceptor and
/// died as "network connection lost".  Scanners get a definite answer too,
/// instead of provoking TLS handshake errors into syslog.
async fn serve_https_redirect(mut stream: TcpStream) {
    let mut head: Vec<u8> = Vec::with_capacity(1024);
    let mut chunk = [0u8; 1024];

    // Read the request head (bounded, with a deadline — this path can also
    // be reached by non-HTTP garbage that happens to start like a method).
    let complete = match tokio::time::timeout(REDIRECT_READ_TIMEOUT, async {
        loop {
            if head.windows(4).any(|w| w == b"\r\n\r\n") || head.len() >= MAX_HEAD {
                break true;
            }
            match stream.read(&mut chunk).await {
                Ok(0) => break false,
                Ok(n) => head.extend_from_slice(&chunk[..n]),
                Err(_) => break false,
            }
        }
    })
    .await
    {
        Ok(done) => done,
        Err(_) => false,
    };
    if !complete {
        return;
    }

    let head = String::from_utf8_lossy(&head);
    let mut lines = head.lines();
    let target = lines
        .next()
        .unwrap_or_default()
        .split(' ')
        .nth(1)
        .unwrap_or("/")
        .to_string();
    let host = lines.find_map(|l| {
        let (name, value) = l.split_once(':')?;
        name.trim()
            .eq_ignore_ascii_case("host")
            .then(|| value.trim())
    });
    // Host is required in HTTP/1.1 and always sent by browsers; fall back to
    // the connection's local address for ancient HTTP/1.0 probes.
    let host = match host {
        Some(h) if !h.is_empty() => h.to_string(),
        _ => match stream.local_addr() {
            Ok(a) => {
                let ip = a.ip().to_string();
                if ip.contains(':') {
                    format!("[{ip}]")
                } else {
                    ip
                }
            }
            Err(_) => return,
        },
    };

    let location = if let Some(rest) = target.strip_prefix("http://") {
        // absolute-form request target: swap the scheme, keep host+path
        format!("https://{rest}")
    } else if target.starts_with('/') {
        // origin-form: prepend scheme://host (Host carries the port if any)
        format!("https://{host}{target}")
    } else {
        // asterisk/authority/obsolete forms — send to the root
        format!("https://{host}/")
    };

    let response = format!(
        "HTTP/1.1 301 Moved Permanently\r\n\
         Location: {location}\r\n\
         Strict-Transport-Security: max-age=31536000\r\n\
         Content-Length: 0\r\n\
         Connection: close\r\n\
         \r\n"
    );
    let _ = stream.write_all(response.as_bytes()).await;
    let _ = stream.flush().await;
    let _ = stream.shutdown().await;
}

async fn handle_connection(
    stream: TcpStream,
    acceptor: Arc<TlsAcceptor>,
    upstream: SocketAddr,
    conns: Arc<AtomicUsize>,
) {
    let _ = stream.set_nodelay(true);

    // Plaintext-HTTP detection: peek (does not consume) the first bytes and
    // check for an HTTP request method.  A TLS record starts with a
    // content-type byte 0x14..=0x17, never an ASCII method, so legitimate
    // TLS handshakes always take the acceptor path unchanged.
    let mut sniff = [0u8; 8];
    let is_http = match stream.peek(&mut sniff).await {
        Ok(n) if n > 0 => HTTP_METHODS.iter().any(|m| sniff[..n].starts_with(m)),
        _ => {
            // 0 bytes = EOF before anything was sent, or a peek error
            conns.fetch_sub(1, Ordering::SeqCst);
            return;
        }
    };
    if is_http {
        serve_https_redirect(stream).await;
        conns.fetch_sub(1, Ordering::SeqCst);
        return;
    }

    let tls_stream = match acceptor.accept(stream).await {
        Ok(s) => s,
        Err(e) => {
            eprintln!("dav-tls: TLS handshake failed: {e}");
            conns.fetch_sub(1, Ordering::SeqCst);
            return;
        }
    };
    let (_io, conn) = tls_stream.get_ref();
    match conn.alpn_protocol() {
        None | Some(ALPN_HTTP1) => {}
        Some(other) => {
            eprintln!("dav-tls: note: negotiated ALPN {:?}", other);
        }
    }

    let upstream_stream = match TcpStream::connect(upstream).await {
        Ok(s) => s,
        Err(e) => {
            eprintln!("dav-tls: connect upstream {upstream} failed: {e}");
            conns.fetch_sub(1, Ordering::SeqCst);
            return;
        }
    };
    let _ = upstream_stream.set_nodelay(true);

    let (mut tls_r, mut tls_w) = tokio::io::split(tls_stream);
    let (mut up_r, mut up_w) = upstream_stream.into_split();

    // client -> server
    let c2s = task::spawn(async move {
        let _ = copy(&mut tls_r, &mut up_w).await;
        let _ = up_w.shutdown().await;
    });
    // server -> client (WriteHalf shutdown sends a TLS close_notify)
    let s2c = task::spawn(async move {
        let _ = copy(&mut up_r, &mut tls_w).await;
        let _ = tls_w.shutdown().await;
    });

    let _ = tokio::join!(c2s, s2c);
    conns.fetch_sub(1, Ordering::SeqCst);
}

/// Accept connections on one endpoint, re-resolving it if it was `--discover`.
///
/// The rebind is what makes a DHCP-assigned WAN viable. A bound socket keeps its
/// address for the life of the process, so a lease change would otherwise leave
/// this task happily accepting connections that no port-forward will ever reach.
/// On detecting a different address this drops the listener and re-binds;
/// in-flight connections are unaffected, because each one is a separate socket
/// that the listener never held.
///
/// `addr` is the address currently bound, tracked separately from `spec` so the
/// comparison is against what is actually open rather than a re-read that could
/// race the bind.
#[allow(clippy::too_many_arguments)]
async fn accept_loop(
    spec: discover::Listen,
    mut addr: SocketAddr,
    mut listener: TcpListener,
    acceptor: Arc<TlsAcceptor>,
    upstream: SocketAddr,
    shutdown: Arc<Notify>,
    conns: Arc<AtomicUsize>,
) {
    // A `--listen` address is fixed by definition; re-reading it every tick
    // would be pure overhead, and its purpose is to not move.
    let watched = matches!(spec, discover::Listen::Discovered { .. });

    let mut ticker = tokio::time::interval(REDISCOVER_INTERVAL);
    // The first tick fires immediately, and the address was just resolved by
    // main(). Skip it: it would be a redundant syscall comparing a value to
    // itself, and a spurious "unchanged" log line on every start.
    ticker.tick().await;

    loop {
        tokio::select! {
                    _ = shutdown.notified() => break,

                    // Watchdog branch — disabled entirely for a fixed address, so the
                    // fixed-address path costs nothing per tick.
                    _ = ticker.tick(), if watched => {
                        match spec.current_addr() {
                            Ok(next) if next != addr => {
                                eprintln!(
                                    "dav-tls: {} moved {addr} -> {next}, rebinding",
                                    spec.describe()
                                );
                                match bind_listener(next) {
                                    Ok(fresh) => {
                                        // Only swap after the new bind succeeded, so a
                                        // failed rebind leaves the old listener serving
                                        // rather than leaving nothing listening at all.
                                        listener = fresh;
                                        addr = next;
                                    }
                                    Err(e) => {
                                        // Keep the old listener and retry next tick.
                                        eprintln!(
                                            "dav-tls: rebind to {next} failed: {e}; still serving {addr}, retrying in {}s",
                                            REBIND_RETRY_INTERVAL.as_secs()
                                        );
        ticker.reset_after(REBIND_RETRY_INTERVAL);
                                    }
                                }
                            }
                            Ok(_) => {}
                            Err(e) => {
                                // Same reasoning as the bind failure: an unresolvable
                                // interface is usually mid-lease, so keep serving the
                                // old address and try again shortly. If the interface
                                // really is gone this logs every retry, which is the
                                // honest report — the endpoint is down and only the
                                // operator can fix it.
                                eprintln!(
                                    "dav-tls: could not re-resolve {}: {e}; still serving {addr}",
                                    spec.describe()
                                );
                                ticker.reset_after(REBIND_RETRY_INTERVAL);
                            }
                        }
                    }

                    accepted = listener.accept() => match accepted {
                        Ok((stream, _peer)) => {
                            if conns.load(Ordering::SeqCst) >= MAX_CONNS {
                                eprintln!("dav-tls: connection limit reached, dropping");
                                continue;
                            }
                            conns.fetch_add(1, Ordering::SeqCst);
                            let acceptor = Arc::clone(&acceptor);
                            let conns = Arc::clone(&conns);
                            task::spawn(handle_connection(stream, acceptor, upstream, conns));
                        }
                        Err(e) => {
                            eprintln!("dav-tls: accept failed: {e}");
                            tokio::time::sleep(Duration::from_millis(100)).await;
                        }
                    }
                }
    }
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = parse_args();

    let config = Arc::new(load_server_config(&args.cert, &args.key).map_err(|e| {
        eprintln!("dav-tls: {e}");
        e
    })?);

    let shutdown = Arc::new(Notify::new());
    let conns = Arc::new(AtomicUsize::new(0));
    let acceptor = Arc::new(TlsAcceptor::from(config));
    let upstream = args.upstream;

    // Must be done while still root (after binding the ports).
    //
    // Each endpoint binds before the privilege drop — including `--discover`
    // ones, which resolve first. That ordering is load-bearing on the appliance:
    // :8443 is above 1024 so a non-root bind would work, but :443 is not, and a
    // host that front-ends dav-tls on 443 must still be able to drop privileges.
    // Resolving here also means a bad interface name is a startup failure with a
    // precise message, rather than a silent listener that never receives.
    let mut bound = Vec::new();
    for spec in &args.listen {
        // Exit rather than propagate: propagating would print the Debug
        // representation as a second, worse error line — an operator on a
        // headless appliance would read `NoSuchInterface { iface: ... }` in
        // syslog after the message that actually explains the problem.
        let addr = match spec.current_addr() {
            Ok(addr) => addr,
            Err(e) => {
                eprintln!("dav-tls: {e}");
                process::exit(1);
            }
        };
        let listener = bind_listener(addr)?;
        eprintln!(
            "dav-tls: listening on TLS {} -> {upstream}",
            spec.describe()
        );
        bound.push((spec.clone(), addr, listener));
    }

    if let Some(user) = &args.user {
        drop_privileges(user).map_err(|e| {
            eprintln!("dav-tls: {e}");
            e
        })?;
        eprintln!("dav-tls: dropped privileges to user '{user}'");
    }

    for (spec, addr, listener) in bound {
        let acceptor = Arc::clone(&acceptor);
        let shutdown = Arc::clone(&shutdown);
        let conns = Arc::clone(&conns);
        task::spawn(accept_loop(
            spec, addr, listener, acceptor, upstream, shutdown, conns,
        ));
    }

    // Graceful SIGTERM/SIGINT: stop accepting, let in-flight connections
    // drain (bounded by DRAIN_TIMEOUT), then exit 0 for procd.
    let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let mut sigint = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
    tokio::select! {
        _ = sigterm.recv() => eprintln!("dav-tls: SIGTERM received, shutting down"),
        _ = sigint.recv() => eprintln!("dav-tls: SIGINT received, shutting down"),
    }
    shutdown.notify_waiters();

    let deadline = tokio::time::Instant::now() + DRAIN_TIMEOUT;
    while conns.load(Ordering::SeqCst) > 0 && tokio::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let remaining = conns.load(Ordering::SeqCst);
    if remaining > 0 {
        eprintln!("dav-tls: drain timeout with {remaining} connection(s) open, exiting");
    }
    process::exit(0);
}
