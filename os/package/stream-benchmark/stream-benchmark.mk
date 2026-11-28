################################################################################
#
# STREAM 5.10 -- diagnostic image only
#
################################################################################

STREAM_BENCHMARK_VERSION = 5.10
STREAM_BENCHMARK_SOURCE = stream.c
STREAM_BENCHMARK_SITE = https://www.cs.virginia.edu/stream/FTP/Code
STREAM_BENCHMARK_LICENSE = STREAM benchmark license
STREAM_BENCHMARK_LICENSE_FILES = stream.c

# The primary distribution is one C source file rather than an archive.
define STREAM_BENCHMARK_EXTRACT_CMDS
	cp $(STREAM_BENCHMARK_DL_DIR)/$(STREAM_BENCHMARK_SOURCE) $(@D)/stream.c
endef

# Keep the author's default array size and iteration count. The maintenance
# sweep requests 1, 2 and 4 OpenMP workers with the same executable.
# Compiler flags alone do not make a run publishable as a conforming result;
# the run rules embedded in stream.c remain authoritative.
define STREAM_BENCHMARK_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) -O3 -fopenmp -o $(@D)/stream $(@D)/stream.c $(TARGET_LDFLAGS) -lm
endef

define STREAM_BENCHMARK_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/stream $(TARGET_DIR)/usr/bin/stream
	$(INSTALL) -D -m 0644 $(@D)/stream.c $(TARGET_DIR)/usr/share/being/stream-5.10.c
endef

$(eval $(generic-package))
