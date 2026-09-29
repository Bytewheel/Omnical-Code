#
# omnical — the Omnical appliance payload as an OpenWrt package.
#
# PLAN_DEPLOYMENTS.md §9.2. The deliverable is a *sysupgrade image*, not a
# deploy script, and the reason is in the README's sysupgrade runbook: a
# `sysupgrade` wipes `/usr/sbin/rustical`, `/usr/sbin/dav-tls`, both custom
# init scripts and the watchdog, so today every firmware update requires a
# laptop, an SSH session, the `pass` secret store, and a re-run of
# `~/router-dav/deploy.sh`. On a retail appliance there is no laptop. So the
# payload moves *into the image*, and a flashed unit re-provisions itself.
#
# The structural fix, not the scripted one. §9.2 says "the image must re-install
# them on every flash". Making `files/postinst` re-install them would still be a
# script that can be skipped. Making the init scripts **files this package
# owns** is what actually fixes it: the package manager restores a package's
# file list on install, so a flash puts them back whether or not anything runs
# afterwards. `files/omnical.keep.d` then makes `sysupgrade -b` preserve them
# across an in-place upgrade, which is a *different* code path from a fresh
# flash and is the one that bit us.
#
# What the postinst still has to do is the genuinely stateful part — the
# `/etc/sysupgrade.conf` entries, the watchdog cron line, and seeding a base
# config — none of which a package file list can express, because they are
# edits to files this package does not own.
#
# Staged inputs (populated by scripts/build-firmware.sh, not committed):
#   files/rustical             aarch64-musl binary        (out/rustical)
#   files/dav-tls              aarch64-musl binary        (out/dav-tls)
#   files/init.d/rustical      init script                (router/etc/init.d)
#   files/init.d/dav-tls       init script                (router/etc/init.d)
#   files/rustical-watchdog    watchdog                   (router/usr/bin)
#   files/config.toml          factory base config        (router/etc/rustical)
#   files/postinst             the hook below
#   files/omnical.keep.d       the sysupgrade -b keep list

include $(TOPDIR)/rules.mk

PKG_NAME:=omnical
PKG_VERSION:=$(call qstrip,$(OMNICAL_VERSION))
PKG_RELEASE:=1
PKG_LICENSE:=AGPL-3.0-only
PKG_MAINTAINER:=Omnical <support@omnical.invalid>

OMNICAL_PAYLOAD_BYTES?=$(shell wc -c < $(CURDIR)/files/rustical 2>/dev/null || echo 0)

include $(INCLUDE_DIR)/package.mk

define Package/omnical
  SECTION:=net
  CATEGORY:=Network
  TITLE:=Omnical CalDAV/CardDAV server
  DEPENDS:=+sqlite3-cli
endef

define Package/omnical/description
  The Omnical payload for a router-class appliance: the rustical CalDAV/CardDAV
  server, the dav-tls TLS front end, a 5-minute health watchdog, and a base
  config. Installed on top of an existing libreCMC/OpenWrt firmware, it turns a
  router into an Omnical node that re-provisions itself after every flash.
endef

# Nothing to compile: the two binaries are built outside the OpenWrt SDK by
# scripts/build-rust.sh (aarch64-unknown-linux-musl, UPX'd) because the SDK
# toolchain is a second, redundant way to produce the same artifact.
define Build/Prepare
endef

define Build/Configure
endef

define Build/Compile
endef

define Package/omnical/install
	# Binaries.  /usr/sbin is NOT preserved by sysupgrade; this is the file
	# list that puts them back on a flash.
	$(INSTALL_DIR) $(1)/usr/sbin
	$(INSTALL_BIN) $(CURDIR)/files/rustical          $(1)/usr/sbin/rustical
	$(INSTALL_BIN) $(CURDIR)/files/dav-tls           $(1)/usr/sbin/dav-tls
	$(INSTALL_BIN) $(CURDIR)/files/rustical-watchdog $(1)/usr/bin/rustical-watchdog

	# The two init scripts.  Custom /etc/init.d files are not conffiles, which
	# is the entire reason this package exists — see the header.
	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) $(CURDIR)/files/init.d/rustical $(1)/etc/init.d/rustical
	$(INSTALL_BIN) $(CURDIR)/files/init.d/dav-tls  $(1)/etc/init.d/dav-tls

	# Persistence list for `sysupgrade -b`.  A fresh flash and an in-place
	# upgrade are different code paths; this covers the second.
	$(INSTALL_DIR) $(1)/lib/upgrade/keep.d
	$(INSTALL_DATA) $(CURDIR)/files/omnical.keep.d $(1)/lib/upgrade/keep.d/omnical

	# The base config is installed as a *sample*, not as /etc/rustical/config.toml.
	# A preserved config is the user's — it holds SMTP credentials and the RSVP
	# secret, and it must survive every future flash untouched.  The postinst
	# copies the sample in only when there is nothing to preserve, and the gate
	# in scripts/firmware-gate.sh asserts exactly that.
	$(INSTALL_DIR) $(1)/etc/rustical
	$(INSTALL_DATA) $(CURDIR)/files/config.toml $(1)/etc/omnical-config.toml.sample

	# Where the database must live: /var is tmpfs and vanishes on every reboot.
	$(INSTALL_DIR) $(1)/usr/local/share/rustical
endef

define Package/omnical/postinst
#!/bin/sh
# Guard: on a build root or a cross-build there is no /etc to touch.
[ -n "$${IPKG_INSTROOT}" ] && exit 0
exec $(CURDIR)/files/postinst
endef

define Package/omnical/prerm
#!/bin/sh
[ -n "$${IPKG_INSTROOT}" ] && exit 0
[ "$${IPKG_INSTROOT}" = "0" ] || /etc/init.d/rustical stop 2>/dev/null
/etc/init.d/dav-tls stop 2>/dev/null
exit 0
endef

define Package/omnical/conffiles
/etc/rustical/config.toml
endef

$(eval $(call BuildPackage,omnical))
