# omnical — appliance image profile for the Netgear R7800 (ipq806x).
#
# Installed into the ImageBuilder by scripts/build-firmware.sh as
# target/linux/<target>/image/omnical.mk and included from the subtarget, so
# `make image PROFILE=omnical` resolves.  The ImageBuilder itself is a
# disposable download; this file is the committed part.
#
# Why a device-specific profile rather than a generic image: a sysupgrade image
# has to match the device's partition layout, bootloader and board id, or the
# box does not come back.  So this inherits Netgear's DniImage machinery and
# every geometry value from `Device/netgear_r7800`, and changes only two things:
# the supported-device name, and the package list.
#
# SUPPORTED_DEVICES is `omnical_r7800`, not `r7800`.  That is deliberate: after a
# flash, /etc/board.json advertises omnical_r7800, so a later appliance image
# is accepted and a stock OpenWrt image is not.  The unit becomes an appliance
# rather than a router that happens to run something.
#
# PACKAGES is deliberately short.  This is not a router — no dnsmasq, no
# firewall4, no LuCI, no wireless config.  The two extras are there for
# operability, not for features:
#   - sqlite3-cli: lets an operator inspect a store on a device whose only
#     interface is a serial console.
#   - kmod-ramoops: writes the last kernel oops to persistent storage.  A
#     headless appliance with no console has no other way to learn it crashed.
define Device/omnical
	$(call Device/DniImage)
	$(Device/dsa-migration)
	DEVICE_VENDOR := NETGEAR
	DEVICE_MODEL := Omnical Appliance (R7800)
	# Inherited verbatim from Device/netgear_r7800 — these are what make the
	# image flashable on this board, and changing any of them bricks it.
	# (The bare names are the convention in these .mk files; the DEVICE_
	# prefix belongs only on VENDOR/MODEL/PACKAGES.)
	SOC := qcom-ipq8065
	KERNEL_SIZE := 4096k
	NETGEAR_BOARD_ID := R7800
	NETGEAR_HW_ID := 29764958+0+128+512+4x4+4x4+cascade
	BLOCKSIZE := 128k
	PAGESIZE := 2048
	# BOARD_NAME is what the DSA/NAND layout keys off, so it stays r7800.
	BOARD_NAME := r7800
	SUPPORTED_DEVICES += omnical_r7800
	DEVICE_PACKAGES := omnical sqlite3-cli kmod-ramoops ath10k-firmware-qca9984-ct
endef
TARGET_DEVICES += omnical
