ARG BASE=eclipse-temurin:25-jdk-noble@sha256:80f67fb3d6f76e2d817d98a4cc2a9cf05f054d4e6adc5e2144bde671d722e63f
FROM ${BASE} AS dependencies
ENV DEBIAN_FRONTEND=noninteractive
COPY --from=recipes apt-packages.txt /tmp/apt-packages.txt
RUN --mount=type=bind,from=re-apt,target=/downloaded,ro \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && cp -a /downloaded/lists/. /var/lib/apt/lists/ \
    && cp /downloaded/archives/*.deb /var/cache/apt/archives/ \
    && xargs -r apt-get install -y --no-download --no-install-recommends < /tmp/apt-packages.txt \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb
COPY node-v24.22.3-linux-arm64.tar.xz /tmp/node.tar.xz
RUN tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 && rm /tmp/node.tar.xz
COPY rea-agents-6.9.0.tgz /tmp/rea.tgz
RUN --mount=type=bind,from=re-npm,target=/downloaded-npm,ro \
    cp -a /downloaded-npm /tmp/npm-cache \
    && mkdir /opt/rea && cd /opt/rea && npm install --offline --cache /tmp/npm-cache \
    --omit=dev --ignore-scripts --no-audit --no-fund /tmp/rea.tgz \
    && rm -rf /tmp/rea.tgz /tmp/npm-cache
FROM dependencies
COPY ghidra_12.2_PUBLIC_20261016.zip /tmp/ghidra.zip
RUN unzip -q /tmp/ghidra.zip -d /opt && mv /opt/ghidra_12.2_PUBLIC /opt/ghidra \
    && rm /tmp/ghidra.zip
# The release may omit the Linux ARM64 decompiler; compile it from bundled sources.
RUN if [ ! -x /opt/ghidra/Ghidra/Features/Decompiler/os/linux_arm_64/decompile ]; then \
    cd /opt/ghidra/Ghidra/Features/Decompiler/src/decompile/cpp \
    && make -j2 ARCH_TYPE= ghidra_opt \
    && mkdir -p /opt/ghidra/Ghidra/Features/Decompiler/os/linux_arm_64 \
    && cp ghidra_opt /opt/ghidra/Ghidra/Features/Decompiler/os/linux_arm_64/decompile \
    && make clean; fi
ENV GHIDRA_INSTALL_DIR=/opt/ghidra \
    PATH=/opt/rea/node_modules/.bin:/usr/local/bin:${PATH} \
    HOME=/state/home \
    GHIDRA_HEADLESS_MAXMEM=1536M \
    JAVA_TOOL_OPTIONS=-XX:ActiveProcessorCount=2 \
    REA_GHIDRA_STARTUP_TIMEOUT_MS=900000 \
    REA_GHIDRA_DECOMPILE_TIMEOUT_SECONDS=120
WORKDIR /state
CMD ["rea", "--help"]
