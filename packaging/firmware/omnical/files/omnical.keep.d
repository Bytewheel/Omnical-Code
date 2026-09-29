# `sysupgrade -b` keep list for the Omnical payload.
#
# /etc/rustical and /usr/local/share/rustical are already in
# /etc/sysupgrade.conf (the postinst re-asserts them, idempotently). What is
# here is the part a file list CANNOT cover: the custom init scripts.
#
# A fresh flash restores them because this package owns them. An *in-place*
# sysupgrade is a different code path — it does not reinstall the package, it
# restores a tarball of the old /etc — and a custom /etc/init.d script is not a
# conffile, so it is not in that tarball. This file is what makes the two paths
# agree. Without it the box comes back from an in-place upgrade with a
# preserved database, a preserved config, and no way to start either.
/etc/init.d/rustical
/etc/init.d/dav-tls
