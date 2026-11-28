################################################################################
#
# turbostat from the pinned target Linux tree -- diagnostic image only
#
################################################################################

BEING_TURBOSTAT_VERSION = $(LINUX_VERSION)
BEING_TURBOSTAT_SITE = $(BR2_EXTERNAL_BEINGOS_PATH)/package/being-turbostat
BEING_TURBOSTAT_SITE_METHOD = local
BEING_TURBOSTAT_DEPENDENCIES = linux libcap
BEING_TURBOSTAT_LICENSE = GPL-2.0-only

# Linux 6.18's own Makefile supplies its matching MSR and Intel-family
# headers and links libcap + librt. O= keeps the kernel source tree untouched.
define BEING_TURBOSTAT_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(MAKE) -C $(LINUX_DIR)/tools/power/x86/turbostat \
		O=$(@D) CC="$(TARGET_CC)" \
		CFLAGS="$(TARGET_CFLAGS) -I$(STAGING_DIR)/usr/include" \
		LDFLAGS="$(TARGET_LDFLAGS) -L$(STAGING_DIR)/usr/lib -Wl,-rpath-link,$(STAGING_DIR)/usr/lib" \
		turbostat
endef

define BEING_TURBOSTAT_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/turbostat $(TARGET_DIR)/usr/bin/turbostat
endef

$(eval $(generic-package))
