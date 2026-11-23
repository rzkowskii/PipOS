################################################################################
#
# sherpa-onnx: the upstream prebuilt C API and ONNX Runtime, for the voice
#
################################################################################

ifeq ($(BR2_PACKAGE_SHERPA_ONNX_V2_0_2),y)
SHERPA_ONNX_VERSION = 2.0.2
else
SHERPA_ONNX_VERSION = 2.0.3
endif
SHERPA_ONNX_SOURCE = sherpa-onnx-v$(SHERPA_ONNX_VERSION)-linux-x64-shared.tar.bz2
SHERPA_ONNX_SITE = https://github.com/k2-fsa/sherpa-onnx/releases/download/v$(SHERPA_ONNX_VERSION)
# The release archive has no licence files: the texts ship in this package
# folder and are copied next to the extracted libraries, where legal-info and
# the hash check find them.
SHERPA_ONNX_LICENSE = Apache-2.0 (sherpa-onnx), MIT (ONNX Runtime 1.28.2), GPL-3.0+ (espeak-ng, linked statically)
SHERPA_ONNX_LICENSE_FILES = \
	LICENSE-sherpa-onnx-Apache-2.0 \
	LICENSE-onnxruntime-MIT \
	COPYING-espeak-ng-GPL-3.0 \
	LICENSES.md
SHERPA_ONNX_INSTALL_STAGING = NO

SHERPA_ONNX_LICENSE_TEXTS = $(SHERPA_ONNX_LICENSE_FILES)

define SHERPA_ONNX_COPY_LICENSE_TEXTS
	$(foreach text,$(SHERPA_ONNX_LICENSE_TEXTS),\
		$(INSTALL) -m 0644 $(SHERPA_ONNX_PKGDIR)/$(text) $(@D)/$(text)$(sep))
endef
SHERPA_ONNX_POST_EXTRACT_HOOKS += SHERPA_ONNX_COPY_LICENSE_TEXTS

# Only the C API and its runtime: no programs, no C++ API, no headers.
define SHERPA_ONNX_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/lib/libsherpa-onnx-c-api.so $(TARGET_DIR)/usr/lib/libsherpa-onnx-c-api.so
	$(INSTALL) -D -m 0755 $(@D)/lib/libonnxruntime.so $(TARGET_DIR)/usr/lib/libonnxruntime.so
	$(foreach text,$(SHERPA_ONNX_LICENSE_TEXTS),\
		$(INSTALL) -D -m 0644 $(@D)/$(text) $(TARGET_DIR)/usr/share/licenses/sherpa-onnx/$(text)$(sep))
endef

$(eval $(generic-package))
