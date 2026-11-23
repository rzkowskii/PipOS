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

define BEING_BACKUP_AGE_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/age $(TARGET_DIR)/usr/bin/age
	$(INSTALL) -D -m 0755 $(@D)/age-keygen $(TARGET_DIR)/usr/bin/age-keygen
endef

$(eval $(generic-package))
