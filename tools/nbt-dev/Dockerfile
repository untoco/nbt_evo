FROM ubuntu:22.04@sha256:74b12b11ffa550fba1929ad4d8d16533c34f6612728469671afba1b03b66881c

ENV DEBIAN_FRONTEND=noninteractive
RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates git build-essential cmake ninja-build \
        python3 python3-pip python3-venv \
        binutils file gdb-multiarch openssh-client rsync zip unzip \
        golang-go libc6:i386 libstdc++6:i386 zlib1g:i386 \
        libncurses5:i386 libtinfo5:i386 \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/nbt-python \
    && /opt/nbt-python/bin/pip install --no-cache-dir \
        Pillow==11.3.0 pyelftools==0.32 capstone==5.0.6

ENV PATH="/opt/nbt-python/bin:/opt/qnx650/host/linux/x86/usr/bin:${PATH}" \
    QNX_HOST=/opt/qnx650/host/linux/x86 \
    QNX_TARGET=/opt/qnx650/target/qnx6 \
    QNX_CONFIGURATION=/qnx-license \
    PYTHONDONTWRITEBYTECODE=1
COPY doctor.sh /usr/local/bin/nbt-doctor
WORKDIR /work
CMD ["bash"]
