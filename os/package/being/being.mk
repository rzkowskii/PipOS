################################################################################
#
# being: the organs
#
################################################################################

BEING_VERSION = 0.1.0
BEING_SITE = $(BR2_EXTERNAL_BEINGOS_PATH)/..
BEING_SITE_METHOD = local
BEING_LICENSE = GPL-3.0 (PipOS), MIT (pico face detector), OFL-1.1 (console font), Apache-2.0 (default model)
BEING_LICENSE_FILES = LICENSE
BEING_DEPENDENCIES = host-rustc host-pkgconf sdl2 python3 jpeg

# '*' is an unusable password hash. A locked '!' account also blocks SSH keys.
# being-shell runs the caged workshop shell and being-panel its renderers
# (docs/design/voice-apps.md section 5.2): locked, no extra groups, no login.
ifeq ($(BR2_PACKAGE_BEING_LAN_MAINTENANCE),y)
define BEING_USERS
	being-shell -1 being-shell -1 ! /workshop /bin/sh - PipOS workshop shell
	being-panel -1 being-panel -1 ! - /bin/false - PipOS workshop renderer
	being-observe -1 being-observe -1 * - /bin/sh - PipOS hardware observation
endef
else
define BEING_USERS
	being-shell -1 being-shell -1 ! /workshop /bin/sh - PipOS workshop shell
	being-panel -1 being-panel -1 ! - /bin/false - PipOS workshop renderer
endef
endif

# Organs written by other lanes are built and installed once their sources
# exist, so this recipe never names a program that is not there yet.
BEING_HAS_BODYCARE = $(wildcard $(BEING_SITE)/crates/bodycare/Cargo.toml)
BEING_HAS_HANDS = $(wildcard $(BEING_SITE)/mind/hands.py)
BEING_HAS_SHELF = $(wildcard $(BEING_SITE)/mind/workshop_shelf/shelf.py)
BEING_HAS_SHELLRC = $(wildcard $(BR2_EXTERNAL_BEINGOS_PATH)/package/being/being-hands-shellrc)

# The source is the whole repository; leave behind what the build does not need.
BEING_OVERRIDE_SRCDIR_RSYNC_EXCLUSIONS = \
	--exclude /target --exclude /out --exclude /os --exclude /lab \
	--exclude /reference --exclude /docs --exclude __pycache__

# Crates are fetched on the first build and kept in Buildroot's download
# directory. PKG_CONFIG_ALLOW_CROSS lets sdl2-sys find the target's SDL2.
BEING_CARGO_ENV = \
	$(TARGET_MAKE_ENV) \
	$(TARGET_CONFIGURE_OPTS) \
	$(PKG_CARGO_ENV) \
	PKG_CONFIG_ALLOW_CROSS=1

define BEING_BUILD_CMDS
	cd $(@D) && $(BEING_CARGO_ENV) \
		cargo build --release --locked -p face -p brainstem $(if $(BEING_HAS_BODYCARE),-p bodycare)
	$(TARGET_CC) $(TARGET_CFLAGS) -std=c99 -fno-strict-aliasing -c \
		-o $(@D)/picornt.o $(BEING_PKGDIR)/picornt.c
	$(TARGET_CXX) $(TARGET_CXXFLAGS) $(TARGET_LDFLAGS) -std=c++17 \
		-o $(@D)/eyes-capture $(BEING_PKGDIR)/eyes-capture.cpp $(@D)/picornt.o -ljpeg
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -std=c11 -I$(STAGING_DIR)/usr/include/SDL2 \
		-o $(@D)/being-console $(BEING_PKGDIR)/being-console.c -lSDL2 -lutil
endef

BEING_BIN = $(@D)/target/$(RUSTC_TARGET_NAME)/release

# Owner-enrolled LAN maintenance. Left out of the default laptop image.
ifeq ($(BR2_PACKAGE_BEING_LAN_MAINTENANCE),y)
define BEING_INSTALL_LAN_MAINTENANCE
	$(INSTALL) -D -m 0755 $(BEING_PKGDIR)/being-lab-access $(TARGET_DIR)/usr/sbin/being-lab-access
endef
define BEING_INSTALL_LAN_MAINTENANCE_UNITS
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-lab-observe.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-lab-observe.service
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-lab-dev.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-lab-dev.service
endef
endif

ifeq ($(BR2_PACKAGE_BEING_LAB_BODY),y)
define BEING_INSTALL_LAB_BODY
	$(INSTALL) -D -m 0644 $(@D)/sim/body.py $(TARGET_DIR)/usr/lib/being/sim/body.py
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-lab-body.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-lab-body.service
endef
endif

ifneq ($(BEING_HAS_BODYCARE),)
define BEING_INSTALL_BODYCARE
	$(INSTALL) -D -m 0755 $(BEING_BIN)/bodycare $(TARGET_DIR)/usr/bin/being-bodycare
endef
define BEING_INSTALL_BODYCARE_UNIT
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-bodycare.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-bodycare.service
endef
endif

ifneq ($(BEING_HAS_HANDS),)
define BEING_INSTALL_HANDS_UNIT
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-hands.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-hands.service
endef
endif

# The workshop's read-only parts: the offline shelf and its launchers. The
# launchers run programs through python3 because /workshop is noexec.
ifneq ($(BEING_HAS_SHELF),)
define BEING_INSTALL_SHELF
	mkdir -p $(TARGET_DIR)/usr/share/being/workshop/shelf $(TARGET_DIR)/usr/lib/being/workshop/bin
	rsync -a --chmod=D0755,F0644 --exclude /bin --exclude __pycache__ --exclude '*.pyc' \
		$(@D)/mind/workshop_shelf/ $(TARGET_DIR)/usr/share/being/workshop/shelf/
	$(INSTALL) -m 0755 $(@D)/mind/workshop_shelf/bin/* $(TARGET_DIR)/usr/lib/being/workshop/bin/
endef
endif
ifneq ($(BEING_HAS_SHELLRC),)
define BEING_INSTALL_SHELLRC
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-hands-shellrc $(TARGET_DIR)/usr/lib/being/workshop/shellrc
endef
endif

define BEING_INSTALL_TARGET_CMDS
	# Start from an empty tree, so a module or file removed from the source
	# is removed from the image too.
	rm -rf $(TARGET_DIR)/usr/lib/being $(TARGET_DIR)/usr/share/being/workshop
	$(INSTALL) -D -m 0755 $(BEING_BIN)/face $(TARGET_DIR)/usr/bin/being-face
	$(INSTALL) -D -m 0755 $(BEING_BIN)/brainstem $(TARGET_DIR)/usr/bin/being-brainstem
	$(INSTALL) -D -m 0755 $(@D)/being-console $(TARGET_DIR)/usr/bin/being-console
	$(INSTALL) -D -m 0755 $(BEING_PKGDIR)/being-screen-mode $(TARGET_DIR)/usr/libexec/being-screen-mode
	$(INSTALL) -D -m 0755 $(BEING_PKGDIR)/being-setup $(TARGET_DIR)/usr/sbin/being-setup
	$(INSTALL) -D -m 0755 $(BEING_PKGDIR)/being-backup $(TARGET_DIR)/usr/sbin/being-backup
	$(INSTALL) -D -m 0755 $(BEING_PKGDIR)/being-memory $(TARGET_DIR)/usr/sbin/being-memory
	$(INSTALL) -D -m 0755 $(BEING_PKGDIR)/being-dream-journal $(TARGET_DIR)/usr/sbin/being-dream-journal
	$(INSTALL) -D -m 0755 $(BEING_PKGDIR)/being-installer $(TARGET_DIR)/usr/sbin/being-installer
	$(INSTALL) -D -m 0755 $(BEING_PKGDIR)/being-wifi $(TARGET_DIR)/usr/sbin/being-wifi
	$(INSTALL) -D -m 0755 $(BEING_PKGDIR)/being-network $(TARGET_DIR)/usr/sbin/being-network
	$(INSTALL) -D -m 0755 $(BEING_PKGDIR)/being-camera $(TARGET_DIR)/usr/sbin/being-camera
	$(INSTALL) -D -m 0755 $(BEING_PKGDIR)/being-usb-logs $(TARGET_DIR)/usr/sbin/being-usb-logs
	$(INSTALL) -D -m 0755 $(BEING_PKGDIR)/being-hardware-report $(TARGET_DIR)/usr/sbin/being-hardware-report
	$(INSTALL) -D -m 0755 $(BEING_PKGDIR)/being-boot-record $(TARGET_DIR)/usr/libexec/being-boot-record
	$(INSTALL) -D -m 0755 $(@D)/eyes-capture $(TARGET_DIR)/usr/libexec/being/eyes-capture
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/facefinder $(TARGET_DIR)/usr/share/being/facefinder
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/PICO-LICENSE $(TARGET_DIR)/usr/share/being/PICO-LICENSE
	$(BEING_INSTALL_LAN_MAINTENANCE)
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being.env $(TARGET_DIR)/etc/being/env
	mkdir -p $(TARGET_DIR)/usr/lib/being/mind
	$(INSTALL) -m 0644 $(@D)/mind/*.py $(TARGET_DIR)/usr/lib/being/mind/
	$(INSTALL) -m 0644 $(@D)/config/being.json $(@D)/config/identity.md $(TARGET_DIR)/etc/being/
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/default-model/pack.json \
		$(TARGET_DIR)/usr/share/being/default-model/pack.json
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/default-model/LICENSE \
		$(TARGET_DIR)/usr/share/being/default-model/LICENSE
	$(BEING_INSTALL_LAB_BODY)
	$(BEING_INSTALL_BODYCARE)
	$(BEING_INSTALL_SHELF)
	$(BEING_INSTALL_SHELLRC)
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/50-being-hardening.conf \
		$(TARGET_DIR)/usr/lib/sysctl.d/50-being-hardening.conf
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/60-being-power.rules \
		$(TARGET_DIR)/usr/lib/udev/rules.d/60-being-power.rules
endef

define BEING_INSTALL_INIT_SYSTEMD
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/30-model-pack.conf \
		$(TARGET_DIR)/usr/lib/systemd/system/being-cortex.service.d/30-model-pack.conf
	$(INSTALL) -D -m 0755 $(BEING_PKGDIR)/being-try-generator \
		$(TARGET_DIR)/usr/lib/systemd/system-generators/being-try-generator
	$(INSTALL) -D -m 0755 $(BEING_PKGDIR)/being-installed-generator \
		$(TARGET_DIR)/usr/lib/systemd/system-generators/being-installed-generator
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-lid.conf \
		$(TARGET_DIR)/etc/systemd/logind.conf.d/50-being-lid.conf
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-brainstem.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-brainstem.service
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-face.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-face.service
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-local-screen.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-local-screen.service
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-usb-screen-start.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-usb-screen-start.service
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-mind.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-mind.service
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-voice.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-voice.service
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-eyes.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-eyes.service
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-wifi.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-wifi.service
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-network-lockdown.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-network-lockdown.service
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-network-policy.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-network-policy.service
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-network-reconcile.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-network-reconcile.service
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-network-online.target \
		$(TARGET_DIR)/usr/lib/systemd/system/being-network-online.target
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/50-being-networkd.conf \
		$(TARGET_DIR)/usr/lib/systemd/system/systemd-networkd.service.d/50-being-network.conf
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/50-being-networkd-socket.conf \
		$(TARGET_DIR)/usr/lib/systemd/system/systemd-networkd.socket.d/50-being-network.conf
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/50-being-networkd-socket.conf \
		$(TARGET_DIR)/usr/lib/systemd/system/systemd-networkd-varlink.socket.d/50-being-network.conf
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/50-being-resolved.conf \
		$(TARGET_DIR)/usr/lib/systemd/system/systemd-resolved.service.d/50-being-network.conf
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/50-being-timesyncd.conf \
		$(TARGET_DIR)/usr/lib/systemd/system/systemd-timesyncd.service.d/50-being-network.conf
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/05-being-unmanaged.network \
		$(TARGET_DIR)/etc/systemd/network/05-being-unmanaged.network
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/50-being-resolve.conf \
		$(TARGET_DIR)/etc/systemd/resolved.conf.d/50-being-network.conf
	$(BEING_INSTALL_LAN_MAINTENANCE_UNITS)
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-boot-health.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-boot-health.service
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-trial-reboot.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-trial-reboot.service
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/being-usb-logs.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-usb-logs.service
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/50-being.preset \
		$(TARGET_DIR)/usr/lib/systemd/system-preset/50-being.preset
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/50-being-journal.conf \
		$(TARGET_DIR)/usr/lib/systemd/journald.conf.d/50-being-journal.conf
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/workshop.mount \
		$(TARGET_DIR)/usr/lib/systemd/system/workshop.mount
	$(INSTALL) -D -m 0644 $(BEING_PKGDIR)/workshop.slice \
		$(TARGET_DIR)/usr/lib/systemd/system/workshop.slice
	$(BEING_INSTALL_BODYCARE_UNIT)
	$(BEING_INSTALL_HANDS_UNIT)
endef

$(eval $(generic-package))
