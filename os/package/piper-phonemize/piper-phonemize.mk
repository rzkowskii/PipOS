################################################################################
#
# piper-phonemize: Piper's espeak-ng phonemizer, for the natural voice
#
################################################################################

PIPER_PHONEMIZE_VERSION = 1.5.9
PIPER_PHONEMIZE_SOURCE = piper-phonemize-v$(PIPER_PHONEMIZE_VERSION)-linux-x64-shared-lib.tar.bz2
PIPER_PHONEMIZE_SITE = https://github.com/csukuangfj/piper-phonemize/releases/download/v$(PIPER_PHONEMIZE_VERSION)
# The release archive holds lib/ and include/ at its top level.
PIPER_PHONEMIZE_STRIP_COMPONENTS = 0
# The archive has no licence files: the texts ship in this package folder and
# are copied next to the extracted library, where legal-info and the hash
# check find them. LICENSES.md names the corresponding source.
PIPER_PHONEMIZE_LICENSE = GPL-3.0+ (piper-phonemize, espeak-ng linked statically), Unlicense or MIT (uni-algo), Unicode-DFS-2016 (espeak-ng character data)
PIPER_PHONEMIZE_LICENSE_FILES = \
	COPYING-GPL-3.0 \
	LICENSE-uni-algo \
	COPYING-espeak-ng-UCD \
	LICENSES.md
PIPER_PHONEMIZE_INSTALL_STAGING = NO

PIPER_PHONEMIZE_LICENSE_TEXTS = $(PIPER_PHONEMIZE_LICENSE_FILES)

define PIPER_PHONEMIZE_COPY_LICENSE_TEXTS
	$(foreach text,$(PIPER_PHONEMIZE_LICENSE_TEXTS),\
		$(INSTALL) -m 0644 $(PIPER_PHONEMIZE_PKGDIR)/$(text) $(@D)/$(text)$(sep))
endef
PIPER_PHONEMIZE_POST_EXTRACT_HOOKS += PIPER_PHONEMIZE_COPY_LICENSE_TEXTS

# Only the C library: no C++ API, no headers, no data (the voice pack has it).
define PIPER_PHONEMIZE_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/lib/libpiper_phonemize_core.so $(TARGET_DIR)/usr/lib/libpiper_phonemize_core.so
	$(foreach text,$(PIPER_PHONEMIZE_LICENSE_TEXTS),\
		$(INSTALL) -D -m 0644 $(@D)/$(text) $(TARGET_DIR)/usr/share/licenses/piper-phonemize/$(text)$(sep))
endef

$(eval $(generic-package))
