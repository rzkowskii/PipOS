# The PipOS build environment: Debian plus what Buildroot asks of its host.
# Buildroot builds its own cross-compiler and host tools; this is only the floor.
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash bc binutils bison build-essential bzip2 ca-certificates cmake cpio curl \
        diffutils e2fsprogs file findutils flex g++ gawk gcc git gzip libelf-dev libncurses-dev \
        libssl-dev locales make patch perl pkg-config python3 rsync sed tar unzip wget \
        xz-utils zstd \
    && rm -rf /var/lib/apt/lists/* \
    && sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen \
    && locale-gen

ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# Buildroot does not want to run as root. /work is the build volume.
RUN useradd --create-home --uid 1000 builder && mkdir /work && chown builder:builder /work
USER builder
WORKDIR /work
