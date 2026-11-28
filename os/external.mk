include $(sort $(wildcard $(BR2_EXTERNAL_BEINGOS_PATH)/package/*/*.mk))

# The memory's search uses SQLite's FTS5, which Buildroot leaves off.
SQLITE_CONF_OPTS += --fts5

# SQLite probes for libz during configure. Give it the selected library in
# its per-package staging tree as well as in the shared lab staging tree.
# Buildroot read these packages before external.mk, so also extend their
# existing make targets. The dependency variables populate the staging trees.
ifeq ($(BR2_PACKAGE_ZLIB),y)
SQLITE_DEPENDENCIES += zlib
$(SQLITE_TARGET_CONFIGURE) sqlite-depends: | zlib
endif

# Give Python's configure checks the same libuuid headers and pkg-config
# data in both trees. Buildroot still leaves its optional _uuid module off.
# Full util-linux itself needs Python for its libmount bindings.
ifeq ($(BR2_PACKAGE_UTIL_LINUX_LIBUUID),y)
PYTHON3_DEPENDENCIES += util-linux-libs
$(PYTHON3_TARGET_CONFIGURE) python3-depends: | util-linux-libs
endif
