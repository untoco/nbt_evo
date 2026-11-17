FROM ubuntu:22.04@sha256:74b12b11ffa550fba1929ad4d8d16533c34f6612728469671afba1b03b66881c

ENV DEBIAN_FRONTEND=noninteractive
COPY apt-packages.txt python-requirements.txt /tmp/
RUN --mount=type=bind,from=dev-apt,target=/downloaded,ro \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && dpkg --add-architecture i386 \
    && cp -a /downloaded/lists/. /var/lib/apt/lists/ \
    && cp /downloaded/archives/*.deb /var/cache/apt/archives/ \
    && xargs -r apt-get install -y --no-download --no-install-recommends < /tmp/apt-packages.txt \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb
RUN --mount=type=bind,from=dev-wheels,target=/wheels,ro \
    python3 -m venv /opt/nbt-python \
    && /opt/nbt-python/bin/pip install --require-hashes --only-binary=:all: --no-index --find-links=/wheels --no-cache-dir \
        -r /tmp/python-requirements.txt

ENV PATH="/opt/nbt-python/bin:/opt/qnx650/host/linux/x86/usr/bin:${PATH}" \
    QNX_HOST=/opt/qnx650/host/linux/x86 \
    QNX_TARGET=/opt/qnx650/target/qnx6 \
    QNX_CONFIGURATION=/qnx-license \
    PYTHONDONTWRITEBYTECODE=1
COPY doctor.sh /usr/local/bin/nbt-doctor
WORKDIR /work
CMD ["bash"]
