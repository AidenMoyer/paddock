# Paddock server image (also used for Paddock Node with PADDOCK_ROLE=node).
#
# Build from the repository root:
#   docker build -f deploy/Dockerfile -t paddock:latest .
#
# Stage 1 is Paddock's own FFmpeg (ffmpeg/build.sh, built from official sources). If CI already built it,
# the tarball in ffmpeg/out/ is used; otherwise it's compiled here (~15 minutes).
# Stage 2 is the app on Debian 13 with the GPU video drivers: Intel Quick Sync/VAAPI and AMD VAAPI
# out of the box; NVIDIA NVENC/NVDEC when TrueNAS gives the app a GPU (NVIDIA container runtime).

FROM debian:trixie-slim AS ffmpeg
COPY ffmpeg/ /build/
RUN set -eux; \
    tarball="$(ls /build/out/paddock-ffmpeg-*-"$(dpkg --print-architecture)".tar.zst 2>/dev/null | sort -V | tail -n1 || true)"; \
    if [ -z "$tarball" ]; then \
      OUT=/build/out bash /build/build.sh; \
      tarball="$(ls /build/out/paddock-ffmpeg-*.tar.zst | sort -V | tail -n1)"; \
    else \
      apt-get update; apt-get install -y --no-install-recommends zstd; \
    fi; \
    mkdir -p /ffroot; tar -C /ffroot -I zstd -xf "$tarball"

FROM python:3.13-slim-trixie

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PADDOCK_DATA_DIR=/data \
    PADDOCK_MEDIA_ROOTS=/media \
    PADDOCK_FFMPEG=/opt/paddock-ffmpeg/bin/ffmpeg \
    PADDOCK_FFPROBE=/opt/paddock-ffmpeg/bin/ffprobe \
    PADDOCK_PORT=8443

RUN set -eux; \
    sed -i 's/^Components: main$/Components: main non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates \
      libx264-164 libzimg2 libdav1d7 libva2 libva-drm2 libdrm2 mesa-va-drivers; \
    if [ "$(dpkg --print-architecture)" = amd64 ]; then \
      apt-get install -y --no-install-recommends libvpl2 libmfx-gen1.2 intel-media-va-driver-non-free; \
    fi; \
    rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg /ffroot/paddock-ffmpeg /opt/paddock-ffmpeg
RUN /opt/paddock-ffmpeg/bin/ffmpeg -hide_banner -version | head -n1

WORKDIR /app
COPY server/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY server/paddock /app/paddock
COPY server/scripts/paddock_mcp_stdio.py /app/scripts/paddock_mcp_stdio.py

# TrueNAS runs apps as the "apps" user (568) by default. Give it the GPU groups at runtime
# with `group_add` in compose (video/render GIDs of the host).
RUN useradd --uid 568 --user-group --no-create-home --shell /usr/sbin/nologin paddock \
    && mkdir -p /data /media && chown 568:568 /data
USER 568:568

LABEL org.opencontainers.image.title="Paddock" \
      org.opencontainers.image.description="Self-hosted media, cinema-footage and engineering vault" \
      org.opencontainers.image.source="https://github.com/AidenMoyer/paddock" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

EXPOSE 8443
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import ssl,urllib.request,os;c=ssl.create_default_context();c.check_hostname=False;c.verify_mode=ssl.CERT_NONE;p=os.environ.get('PADDOCK_PORT','8443');urllib.request.urlopen(f'https://127.0.0.1:{p}/api/health',context=c,timeout=4)" || exit 1

CMD ["python", "-m", "paddock"]
