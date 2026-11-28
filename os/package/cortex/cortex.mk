################################################################################
#
# cortex: llama.cpp's server, for Ivy Bridge
#
################################################################################

CORTEX_VERSION = v1.0.0
CORTEX_SOURCE = $(CORTEX_VERSION).tar.gz
CORTEX_SITE = https://github.com/ggml-org/llama.cpp/archive/refs/tags
CORTEX_LICENSE = MIT
CORTEX_LICENSE_FILES = LICENSE
CORTEX_SUPPORTS_IN_SOURCE_BUILD = NO

# Ivy Bridge: SSE4.2, AVX and F16C, and nothing newer. GGML_NATIVE must be off
# (the build machine is not the laptop), and with it off llama.cpp would
# otherwise assume AVX2, FMA and BMI2.
CORTEX_CONF_OPTS = \
	-DBUILD_SHARED_LIBS=OFF \
	-DLLAMA_BUILD_TESTS=OFF \
	-DLLAMA_BUILD_EXAMPLES=OFF \
	-DLLAMA_BUILD_TOOLS=ON \
	-DLLAMA_BUILD_SERVER=ON \
	-DLLAMA_CURL=OFF \
	-DLLAMA_OPENSSL=OFF \
	-DLLAMA_FATAL_WARNINGS=OFF \
	-DLLAMA_BUILD_UI=OFF \
	-DLLAMA_USE_PREBUILT_UI=OFF \
	-DGGML_NATIVE=OFF \
	-DGGML_SSE42=ON \
	-DGGML_AVX=ON \
	-DGGML_F16C=ON \
	-DGGML_AVX2=OFF \
	-DGGML_FMA=OFF \
	-DGGML_BMI2=OFF \
	-DGGML_AVX512=OFF \
	-DGGML_OPENMP=OFF \
	-DGGML_VULKAN=OFF \
	-DGGML_BLAS=OFF

# The two UI options: no web interface is built, and none is downloaded from
# llama.cpp's asset bucket during the build. The being has a face already.

# Shipping images build only the server. The isolated WP-C8 diagnostic image
# also carries the upstream benchmark executable; it is never in an update.
CORTEX_MAKE_OPTS = llama-server $(if $(BR2_PACKAGE_BEING_DIAGNOSTIC),llama-bench)

# The conversation model's chat template (config/chat-template.jinja): past
# replies render byte for byte as they were generated, so the cache holds.
CORTEX_CHAT_TEMPLATE = $(BR2_EXTERNAL_BEINGOS_PATH)/../config/chat-template.jinja

define CORTEX_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/buildroot-build/bin/llama-server $(TARGET_DIR)/usr/bin/llama-server
	$(if $(BR2_PACKAGE_BEING_DIAGNOSTIC),$(INSTALL) -D -m 0755 $(@D)/buildroot-build/bin/llama-bench $(TARGET_DIR)/usr/bin/llama-bench)
	$(INSTALL) -D -m 0644 $(CORTEX_CHAT_TEMPLATE) $(TARGET_DIR)/usr/share/being/chat-template.jinja
endef

# The dream model's server is started and stopped by the night shift only;
# 50-being.preset keeps it disabled at boot. being-model-check has no
# [Install] section: being-cortex.service pulls it in, on Try boots only.
define CORTEX_INSTALL_INIT_SYSTEMD
	$(INSTALL) -D -m 0644 $(CORTEX_PKGDIR)/being-cortex.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-cortex.service
	$(INSTALL) -D -m 0644 $(CORTEX_PKGDIR)/being-dream-cortex.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-dream-cortex.service
	$(INSTALL) -D -m 0644 $(CORTEX_PKGDIR)/being-model-check.service \
		$(TARGET_DIR)/usr/lib/systemd/system/being-model-check.service
endef

$(eval $(cmake-package))
