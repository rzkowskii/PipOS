################################################################################
#
# being-backup-age
#
################################################################################

BEING_BACKUP_AGE_VERSION = 1.3.2
BEING_BACKUP_AGE_SOURCE = age-v$(BEING_BACKUP_AGE_VERSION)-linux-amd64.tar.gz
BEING_BACKUP_AGE_SITE = https://github.com/FiloSottile/age/releases/download/v$(BEING_BACKUP_AGE_VERSION)
BEING_BACKUP_AGE_LICENSE = BSD-3-Clause
BEING_BACKUP_AGE_LICENSE_FILES = LICENSE

# The official release binary, pinned by being-backup-age.hash. It is built
# with Go's default GOAMD64=v1 (the x86-64 baseline), so it needs no AVX2;
# Go's runtime uses wider instructions only behind CPU feature checks. This
# is the one prebuilt binary in the image: building age from source would
# add a Go toolchain to the build for no change in what runs.

define BEING_BACKUP_AGE_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/age $(TARGET_DIR)/usr/bin/age
	$(INSTALL) -D -m 0755 $(@D)/age-keygen $(TARGET_DIR)/usr/bin/age-keygen
endef

$(eval $(generic-package))
