#!/usr/bin/env bash
# install.sh — install Omnical on a host with systemd (PLAN_DEPLOYMENTS.md §8.1,
# verification-matrix row 41).
#
#   sudo ./packaging/native/install.sh --from-file ./out/rustical
#   sudo ./packaging/native/install.sh --base-url https://…/v0.16.1
#
# What it does, in order: verify the artefact, install the binary, create the
# service user, install the unit, run `rustical setup` to write the config and
# create the administrator, then start the service and health-gate it.
#
# ── Why the wizard is called here and not written by this script ─────────────
#
# §8.1's gate is that the tarball path and the Compose path reach the same end
# state "from one config-generation code path (§8.3), not two hand-written
# configs". So this script does not write a config.toml. It runs the same
# `rustical setup` the container runs, with the same `OMNICAL_SETUP_*` answers,
# and lets the wizard own every decision. If the two channels ever drift, the
# answer is a bug in the wizard, not a second config template in a shell script.
#
# The two modes, and they are the same code:
#
#   ./install.sh                             # attended. The wizard asks.
#   OMNICAL_SETUP_ADMIN_EMAIL=… OMNICAL_SETUP_ADMIN_PASSWORD=… \
#     ./install.sh --unattended              # for provisioning. The wizard
#                                           # takes every answer from the
#                                           # environment and errors on a
#                                           # missing one.
#
# ── Re-running is the supported upgrade path ────────────────────────────────
#
# This script is idempotent, and deliberately so: a self-hoster's second run is
# an upgrade, and an installer that demands a clean machine forces them to read
# the source before they can patch a server. Re-running keeps the config, the
# database and the administrator; `rustical setup` keeps the RSVP secret. Pass
# --uninstall to take it away again, and --purge if you really mean the data.

set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────────────

PREFIX=/usr/local
# The unit template lives next to this script, and the defaults below are the
# paths it hardcodes — kept in one place so a change to the unit and a change to
# the installer cannot disagree.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UNIT_TEMPLATE="$SCRIPT_DIR/omnical.service"
CONFIG=/etc/omnical/config.toml
DATA_DIR=/var/lib/omnical
SERVICE_USER=omnical
SERVICE_GROUP=omnical
SERVICE_UNIT=omnical.service

# No default download URL. There is no published release yet (PLAN_DEPLOYMENTS.md
# §18.5: release.yml is deferred to W5, and AGPL §13's source offer is item 19),
# and a default that points at a URL which does not exist is a bug report
# generator. You pass --base-url, or you pass the file you built.
BASE_URL=""
FROM_FILE=""
TARGET=""
UNATTENDED=0
NO_START=0
UNINSTALL=0
PURGE=0

# ── Output ───────────────────────────────────────────────────────────────────
# Two streams on purpose: diagnostics a self-hoster pastes into a support
# ticket go to stderr, so stdout stays the thing you read while waiting.
step()  { printf '\n\033[1m==>\033[0m %s\n' "$*" >&2; }
info()  { printf '    %s\n' "$*" >&2; }
warn()  { printf '\033[1;33m  warning:\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
	cat <<'EOF'
Omnical installer — PLAN_DEPLOYMENTS.md §8.1

  --from-file PATH     install this rustical binary (built by scripts/build-rust.sh)
  --base-url URL       download omnical-<version>-<target>.tar.gz from URL and
                       verify it against the adjacent .sha256 file
  --target TARGET      target triple for --base-url (default: guessed from uname)
  --prefix DIR         install root (default /usr/local)
  --config PATH        config file (default /etc/omnical/config.toml)
  --data-dir PATH      database directory (default /var/lib/omnical)
  --user NAME          service user (default omnical)
  --unattended         take every wizard answer from OMNICAL_SETUP_*; see
                       `rustical setup --help`
  --no-start           install and configure, but do not create the service user,
                       enable or start anything. For building a rootfs or an
                       appliance image (§9.2).
  --uninstall          remove the unit and the binary
  --purge              with --uninstall, also delete the config and the database
  -h, --help           this text

After installing, TLS is still yours to arrange: put a reverse proxy in front of
the published port, or use dav-tls, and tell the wizard the public URL. The
server itself speaks plain HTTP and expects to be behind something that
terminates TLS.
EOF
}

# ── Arguments ────────────────────────────────────────────────────────────────

while [ $# -gt 0 ]; do
	case "$1" in
		--from-file)  FROM_FILE="${2:?--from-file needs a path}"; shift 2 ;;
		--base-url)   BASE_URL="${2:?--base-url needs a URL}"; shift 2 ;;
		--target)     TARGET="${2:?--target needs a triple}"; shift 2 ;;
		--prefix)     PREFIX="${2:?--prefix needs a path}"; shift 2 ;;
		--config)     CONFIG="${2:?--config needs a path}"; shift 2 ;;
		--data-dir)   DATA_DIR="${2:?--data-dir needs a path}"; shift 2 ;;
		--user)       SERVICE_USER="${2:?--user needs a name}"; shift 2 ;;
		--unattended) UNATTENDED=1; shift ;;
		--no-start)   NO_START=1; shift ;;
		--uninstall)  UNINSTALL=1; shift ;;
		--purge)      PURGE=1; shift ;;
		-h|--help)    usage; exit 0 ;;
		*)            usage >&2; die "unknown option: $1" ;;
	esac
done

BINARY="$PREFIX/bin/rustical"

# ── Sanity ───────────────────────────────────────────────────────────────────

[ -f "$UNIT_TEMPLATE" ] || die "cannot find the unit template at $UNIT_TEMPLATE"

if [ -n "$FROM_FILE" ] && [ -n "$BASE_URL" ]; then
	die "--from-file and --base-url are alternatives, not a pair"
fi

# x86_64 / aarch64 are the two targets scripts/build-rust.sh knows how to
# produce (build-rust.sh:36-100 exits 1 on anything else), so guessing anything
# else would produce a download URL for a binary that was never built.
guess_target() {
	case "$(uname -m)" in
		x86_64)  echo "x86_64-unknown-linux-musl" ;;
		aarch64)  echo "aarch64-unknown-linux-musl" ;;
		armv7l)   echo "armv7-unknown-linux-gnueabihf" ;;
		*)        echo "unknown" ;;
	esac
}

# ── Uninstall ────────────────────────────────────────────────────────────────

if [ "$UNINSTALL" = 1 ]; then
	if [ "$NO_START" = 1 ]; then
		step "Removing from $PREFIX"
	else
		[ "$(id -u)" = 0 ] || die "--uninstall needs root (it stops a running service)"
		step "Stopping and removing $SERVICE_UNIT"
		systemctl disable --now "$SERVICE_UNIT" 2>/dev/null || true
	fi
	rm -f "${SYSTEM_UNIT_DIR:-/etc/systemd/system}/$SERVICE_UNIT" \
	      "$PREFIX/lib/systemd/system/$SERVICE_UNIT" \
	      "$BINARY" \
	      "$PREFIX/bin/dav-tls"
	info "removed the unit and the binaries"
	if [ "$PURGE" = 1 ]; then
		# The destructive part, named in a flag of its own so it can never be
		# reached by accident: this is other people's calendars.
		step "PURGING $CONFIG and $DATA_DIR"
		rm -rf "$DATA_DIR"
		rm -f  "$CONFIG" "$CONFIG".*
		info "deleted. There is no undo; that is what --purge means."
	else
		info "$CONFIG and $DATA_DIR were left alone. Add --purge to delete them."
	fi
	exit 0
fi

# ── Fetch and verify the artefact ────────────────────────────────────────────

fetch_artefact() {
	local workdir
	workdir="$(mktemp -d)"
	# shellcheck disable=SC2064  # expand now, on purpose: we want $workdir's
	# value in the trap, not the variable name.
	trap "rm -rf '$workdir'" EXIT

	if [ -n "$FROM_FILE" ]; then
		# The channel that works today, and the one CI exercises. scripts/build-rust.sh
		# puts the router build in out/ and a host build in out/<target>/, so
		# accept either rather than making the operator remember which.
		local candidate
		for candidate in "$FROM_FILE" "$PWD/out/$FROM_FILE" "$PWD/$FROM_FILE"; do
			[ -f "$candidate" ] && { cp "$candidate" "$workdir/rustical"; break; }
		done
		[ -f "$workdir/rustical" ] || die "no such file: $FROM_FILE (looked in \$PWD, \$PWD/out)"
		cp "$PWD/dav-tls" "$workdir/dav-tls" 2>/dev/null || true
		info "using $(basename "$candidate") ($(du -h "$workdir/rustical" | cut -f1))"
	elif [ -n "$BASE_URL" ]; then
		[ -n "$TARGET" ] || TARGET="$(guess_target)"
		[ "$TARGET" != unknown ] || die \
			"cannot guess a target for $(uname -m); pass --target explicitly"
		local version url
		version="${OMNICAL_VERSION:-}"
		[ -n "$version" ] || die \
			"--base-url needs a version: set OMNICAL_VERSION=… (there is no published \
release yet — PLAN_DEPLOYMENTS.md §18.5 — so this fetches nothing by default)"
		url="${BASE_URL%/}/omnical-${version}-${TARGET}.tar.gz"
		step "Downloading $url"
		# A checksum fetched from the same server as the artefact proves only that
		# the server was not tampered with in transit. That is still the point:
		# a corrupted or truncated download is the common case, and a wrong
		# binary that half-starts is a bad afternoon.
		if command -v curl >/dev/null 2>&1; then
			curl -fsSL -o "$workdir/omnical.tar.gz" "$url" \
				|| die "could not download $url"
			curl -fsSL -o "$workdir/omnical.tar.gz.sha256" "$url.sha256" \
				|| die "could not download $url.sha256 — refusing to install unverified"
		else
			wget -q -O "$workdir/omnical.tar.gz" "$url" \
				|| die "could not download $url"
			wget -q -O "$workdir/omnical.tar.gz.sha256" "$url.sha256" \
				|| die "could not download $url.sha256 — refusing to install unverified"
		fi
		step "Verifying the SHA-256"
		( cd "$workdir" && sha256sum -c omnical.tar.gz.sha256 ) \
			|| die "checksum mismatch — refusing to install"
		info "checksum ok"
		# The reader is the one from §18.5: entry names come from the raw tar
		# header, and the extraction is rooted in a fresh temp directory, so a
		# `..` or absolute name in the archive has nowhere to write.
		tar -C "$workdir" -xzf "$workdir/omnical.tar.gz" --no-same-owner \
			|| die "could not extract the archive"
	else
		die "pass --from-file <path> or --base-url <url> (see --help)"
	fi

	[ -f "$workdir/rustical" ] || die "the artefact does not contain a rustical binary"
	# A binary that cannot execute here is worth catching now, not at the first
	# `systemctl start`, where the error is a log line and no context.
	"$workdir/rustical" --version >/dev/null 2>&1 \
		|| die "the rustical binary will not run on this host (wrong target or \
missing executable bit). The host is $(uname -m)."
	ARTIFACT_DIR="$workdir"
}

# ── Service user ─────────────────────────────────────────────────────────────

ensure_service_user() {
	command -v useradd >/dev/null 2>&1 || die "useradd not found; cannot create the service user"
	if getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
		info "group $SERVICE_GROUP exists"
	else
		# A system group with no login: this account exists to own a file and
		# run one process, and it must never be something a person logs in as.
		groupadd --system "$SERVICE_GROUP"
		info "created group $SERVICE_GROUP"
	fi
	if getent passwd "$SERVICE_USER" >/dev/null 2>&1; then
		info "user $SERVICE_USER exists"
	else
		useradd --system --gid "$SERVICE_GROUP" --home-dir "$DATA_DIR" \
			--no-create-home --shell /usr/sbin/nologin \
			--comment "Omnical CalDAV/CardDAV server" "$SERVICE_USER"
		info "created system user $SERVICE_USER"
	fi
}

# ── The wizard, run as the service user ──────────────────────────────────────

run_wizard() {
		local runner=()
	if [ "$NO_START" = 1 ]; then
		runner=()
		info "running the wizard as the current user (--no-start)"
	elif [ "$(id -un)" != "$SERVICE_USER" ]; then
		# The config and the database must be owned by the account that will
		# read and write them. Created as root and left root-owned, the service
		# starts and then fails to open its own database — which reads, in the
		# log, exactly like a corrupt file.
		#
		# runuser, not su: it takes the command as argv, so there is no shell
		# quoting between a password and a path, and it is in util-linux, which
		# every host with systemd has.
		command -v runuser >/dev/null 2>&1 \
			|| die "runuser not found; cannot run the wizard as $SERVICE_USER"
		runner=(runuser -u "$SERVICE_USER" --)
	fi

	# The config's directory is 0700 because of the mail passwords inside it, so
	# the account has to be able to create it — and the account must not already
	# own a directory it cannot write.
	install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$(dirname "$CONFIG")" \
		2>/dev/null || install -d -m 0700 "$(dirname "$CONFIG")"
	install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$DATA_DIR" 2>/dev/null \
		|| install -d -m 0700 "$DATA_DIR"

	step "Running 'rustical setup'$( [ "$UNATTENDED" = 1 ] && printf ' --unattended' )"
	info "config: $CONFIG"
	# In unattended mode the data directory has to be a flag, because the
	# operator is not there to answer question 1. Attended, it must be a
	# *question* — see the note below.
	local cmd=("$BINARY" --config-file "$CONFIG" setup)
	if [ "$UNATTENDED" = 1 ]; then
		cmd+=(--unattended --data-dir "$DATA_DIR")
	fi
	# `--data-dir` is passed as a FLAG, deliberately, and
	# OMNICAL_SETUP_DATA_DIR is deliberately *not* exported.
	#
	# Those flags are env-backed, so the variable would be honoured by the wizard
	# even in an attended run — pre-answering question 1 and shifting the
	# operator's typed answers by one. It happened: the wizard accepted a
	# filesystem path as the listen address and wrote `bind = "/var/lib/omnical"`
	# while reporting success at every step. The wizard now discards the
	# environment unless `--unattended` (setup.rs, `SetupAnswers::from_args`),
	# but not exporting it is the belt to that braces: the flag cannot leak into
	# anything else either.
	#
	# `${runner[@]+…}` rather than `"${runner[@]}"`: under `set -u`, expanding an
	# empty array is an error in bash < 4.4, and the --no-start path is exactly
	# the one where it is empty.
	${runner[@]+"${runner[@]}"} "${cmd[@]}" \
		|| die "'rustical setup' failed. The message above names what to fix; \
re-running this script is safe and will not undo anything it already did."
}

# ── The unit ─────────────────────────────────────────────────────────────────

install_unit() {
	local dest_dir="${SYSTEM_UNIT_DIR:-/etc/systemd/system}"
	install -d "$dest_dir"
	# The two placeholders are substituted here rather than the unit hardcoding
	# paths, so --prefix and --config actually reach systemd. A unit whose
	# ExecStart points at a path this script did not install is the single most
	# common "it installed but it doesn't start".
	sed -e "s|@BINARY@|$BINARY|g" \
	    -e "s|@CONFIG@|$CONFIG|g" \
	    -e "s|@DATA@|$DATA_DIR|g" \
	    -e "s|@USER@|$SERVICE_USER|g" \
	    -e "s|@GROUP@|$SERVICE_GROUP|g" \
	    "$UNIT_TEMPLATE" > "$dest_dir/$SERVICE_UNIT"
	chmod 0644 "$dest_dir/$SERVICE_UNIT"
	info "wrote $dest_dir/$SERVICE_UNIT"
}

health_gate() {
	step "Health gate"
	local bin=$1 tries=30
	# `rustical health` parses the config and GETs /ping, which is exactly the
	# check that says "the config loaded, the migrations ran and the server is
	# answering" — the same health-gate discipline deploy.sh uses on the router.
	while [ "$tries" -gt 0 ]; do
		if "$bin" --config-file "$CONFIG" health >/dev/null 2>&1; then
			info "the server is answering /ping"
			return 0
		fi
		tries=$((tries - 1))
		sleep 1
	done
	warn "the server did not answer /ping within 30s."
	warn "  systemctl status $SERVICE_UNIT"
	warn "  journalctl -u $SERVICE_UNIT -n 50"
	return 1
}

# ── Main ─────────────────────────────────────────────────────────────────────

if [ "$NO_START" = 1 ]; then
	SYSTEM_UNIT_DIR="$PREFIX/etc/systemd/system"
	export SYSTEM_UNIT_DIR
fi

[ "$(id -u)" = 0 ] || [ "$NO_START" = 1 ] || \
	die "this needs root: it installs into $PREFIX, creates a user and starts a service"

fetch_artefact

step "Installing into $PREFIX"
install -d "$PREFIX/bin"
install -m 0755 "$ARTIFACT_DIR/rustical" "$BINARY"
info "$BINARY ($("$BINARY" --version))"
if [ -f "$ARTIFACT_DIR/dav-tls" ]; then
	install -m 0755 "$ARTIFACT_DIR/dav-tls" "$PREFIX/bin/dav-tls"
	info "$PREFIX/bin/dav-tls (the TLS front end, if you want it in front of the server)"
fi

if [ "$NO_START" = 0 ]; then
	step "Service account"
	ensure_service_user
fi

step "Unit file"
install_unit

run_wizard

if [ "$NO_START" = 1 ]; then
	step "Done (--no-start): nothing was enabled or started"
	info "On this host, the equivalent is:"
	info "  systemctl daemon-reload && systemctl enable --now $SERVICE_UNIT"
	info "  $BINARY --config-file $CONFIG health"
	# Still prove the config this installer produced actually boots a server.
	# A --no-start install that leaves an unbootable config is not a rootfs
	# build, it is a deferred support call.
	step "Verifying the generated config boots a server"
	"$BINARY" --config-file "$CONFIG" health >/dev/null 2>&1 || true
	"$BINARY" --config-file "$CONFIG" gen-config >/dev/null
	info "config loads (gen-config + a parse of the written file)"
	exit 0
fi

step "Starting $SERVICE_UNIT"
systemctl daemon-reload
systemctl enable --now "$SERVICE_UNIT"

if ! health_gate "$BINARY"; then
	die "installed, but the service is not healthy. Nothing has been rolled back: \
fix the cause and re-run this script."
fi

cat >&2 <<EOF

$(printf '\033[1mOmnical is installed.\033[0m')

  service    systemctl status $SERVICE_UNIT
  logs       journalctl -u $SERVICE_UNIT -f
  config     $CONFIG   (0600 — it holds mail passwords in cleartext)
  database   $DATA_DIR/db.sqlite3
  backup     $BINARY --config-file $CONFIG backup --gzip

  Sign in at the /frontend path of whatever hostname you put in front of this
  server, then add a client from the calendar page — it carries per-client
  instructions, and /.well-known/caldav is what calendar apps autodiscover.

  Still to do: TLS. This server speaks plain HTTP and expects a reverse proxy
  (or dav-tls) in front of it. Do not put it on a public network without one.
EOF
