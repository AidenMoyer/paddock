#!/usr/bin/env bash
# Paddock's own FFmpeg: built from the official FFmpeg source with only what Paddock uses.
#
#   GPU:     Intel Quick Sync (oneVPL) and VAAPI (Intel/AMD), NVIDIA NVENC/NVDEC + CUDA scaling
#   CPU:     x264 for H.264, dav1d for AV1, zimg for HDR → SDR tone-mapping
#   Speech:  the whisper filter (whisper.cpp, CPU; picks the best instruction set at run time)
#   Off:     network protocols, capture devices (only lavfi test sources stay), ffplay, docs
#
# Runs on Debian 13 "trixie" (a container or chroot) and produces
#   out/paddock-ffmpeg-<version>-<rev>-<arch>.tar.zst   → unpacks to /opt/paddock-ffmpeg
# used by both the container image and Paddock OS.
set -Eeuo pipefail
trap 'echo "ffmpeg/build.sh: failed at line $LINENO: $BASH_COMMAND (exit $?)" >&2' ERR

FFMPEG_VERSION=8.1.3
FFMPEG_SHA256=7138d28c96d9d3e3af4ee3d8cad72741f8ffb40da90c1112235dea3ecd3178a3
WHISPER_VERSION=1.9.5
WHISPER_SHA256=ff1a9053feb509ff9d7729703355541ae9690073a6b1c40eb692c962e0dc1720
# NVIDIA codec headers 12.1: NVENC/NVDEC with driver 530 or newer (works on GTX 900/10-series too)
NVHEADERS_VERSION=12.1.14.1
NVHEADERS_SHA256=f40c8a27417eb2688412da32985e382e811196311c4d103d1fcaafad2876cfc1
REV=1

PREFIX=/opt/paddock-ffmpeg
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${OUT:-$HERE/out}"
WORK="${WORK:-/var/tmp/paddock-ffmpeg}"
ARCH="$(dpkg --print-architecture)"
JOBS="$(nproc)"
NAME="paddock-ffmpeg-$FFMPEG_VERSION-$REV-$ARCH"

log() { echo "==> $*"; }

log "build dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  build-essential pkg-config nasm cmake clang curl ca-certificates xz-utils zstd patchelf \
  libx264-dev libzimg-dev libdav1d-dev libva-dev libdrm-dev zlib1g-dev >/dev/null
if [ "$ARCH" = amd64 ]; then apt-get install -y -qq --no-install-recommends libvpl-dev >/dev/null; fi

rm -rf "$WORK" && mkdir -p "$WORK/src" "$WORK/deps" "$OUT"
rm -rf "$PREFIX" && mkdir -p "$PREFIX"
cd "$WORK/src"

fetch() {  # url file sha256
  curl -fsSL --retry 3 -o "$2" "$1"
  echo "$3  $2" | sha256sum -c --quiet - || { echo "checksum mismatch for $2" >&2; exit 1; }
  mkdir -p "${2%%.tar*}" && tar -xf "$2" -C "${2%%.tar*}" --strip-components=1
}
fetch "https://ffmpeg.org/releases/ffmpeg-$FFMPEG_VERSION.tar.xz" ffmpeg.tar.xz "$FFMPEG_SHA256"
fetch "https://github.com/ggml-org/whisper.cpp/archive/refs/tags/v$WHISPER_VERSION.tar.gz" whisper.tar.gz "$WHISPER_SHA256"
fetch "https://github.com/FFmpeg/nv-codec-headers/archive/refs/tags/n$NVHEADERS_VERSION.tar.gz" nvheaders.tar.gz "$NVHEADERS_SHA256"

log "NVIDIA codec headers $NVHEADERS_VERSION"
make -C nvheaders PREFIX="$WORK/deps" install >/dev/null

log "whisper.cpp $WHISPER_VERSION"
# Shared libs with every CPU variant (SSE4.2 … AVX-512) as loadable backends: one build runs fast on
# new CPUs and still works on old NAS chips. Backends are found next to the ffmpeg binary.
cmake -S whisper -B whisper/build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \
  -DGGML_NATIVE=OFF -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS="$([ "$ARCH" = amd64 ] && echo ON || echo OFF)" \
  -DWHISPER_BUILD_EXAMPLES=OFF -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_INSTALL_RPATH='$ORIGIN' >/dev/null
cmake --build whisper/build -j "$JOBS" >/dev/null
cmake --install whisper/build >/dev/null

log "FFmpeg $FFMPEG_VERSION"
export PKG_CONFIG_PATH="$WORK/deps/lib/pkgconfig:$PREFIX/lib/pkgconfig"
gpu=(--enable-vaapi --enable-libdrm --enable-ffnvcodec --enable-nvenc --enable-nvdec --enable-cuvid --enable-cuda-llvm)
[ "$ARCH" = amd64 ] && gpu+=(--enable-libvpl)
cd ffmpeg
# shared libav* so ffmpeg and ffprobe don't each carry a copy (library paths are set with patchelf below)
./configure --prefix="$PREFIX" --extra-version=paddock$REV \
  --enable-gpl --enable-version3 --disable-autodetect --enable-shared --disable-static \
  --disable-doc --disable-ffplay --disable-debug --disable-network --disable-indevs --enable-indev=lavfi --disable-outdevs \
  --enable-pthreads --enable-zlib --enable-iconv \
  --enable-libx264 --enable-libzimg --enable-libdav1d --enable-whisper \
  "${gpu[@]}" \
  --extra-cflags="-I$WORK/deps/include" \
  --extra-ldflags="-L$PREFIX/lib" >"$WORK/configure.log" 2>&1 || { tail -40 "$WORK/configure.log"; exit 1; }
make -j "$JOBS" >/dev/null
make install >/dev/null
cd ..

log "packaging"
strip "$PREFIX/bin/ffmpeg" "$PREFIX/bin/ffprobe"
rm -rf "$PREFIX/include" "$PREFIX/share/ffmpeg/examples" "$PREFIX/lib/pkgconfig" "$PREFIX/lib/cmake" "$PREFIX"/lib/*.a "$PREFIX/share/man" \
  "$PREFIX"/lib/libparakeet.so*   # whisper.cpp's other speech model; FFmpeg's filter doesn't use it
# ggml loads its CPU backends from the executable's directory
mv "$PREFIX"/lib/libggml-cpu*.so "$PREFIX/bin/" 2>/dev/null || true
# relocatable: everything finds its libraries relative to itself, wherever the tree is unpacked
for f in "$PREFIX"/bin/*; do [ -f "$f" ] && patchelf --set-rpath '$ORIGIN/../lib' "$f"; done
for f in "$PREFIX"/lib/*.so.*; do [ -L "$f" ] || patchelf --set-rpath '$ORIGIN' "$f"; done
readelf -d "$PREFIX/bin/ffmpeg" | grep -q 'RUNPATH.*\[\$ORIGIN/../lib\]' || { echo "ffmpeg RUNPATH is wrong" >&2; exit 1; }
find "$PREFIX" -name '*.so*' -type f -exec strip --strip-unneeded {} +
mkdir -p "$PREFIX/share/licenses"
cp ffmpeg/COPYING.GPLv3 "$PREFIX/share/licenses/FFmpeg-GPLv3.txt"
cp whisper/LICENSE "$PREFIX/share/licenses/whisper.cpp-MIT.txt"
head -n 30 nvheaders/include/ffnvcodec/nvEncodeAPI.h > "$PREFIX/share/licenses/nv-codec-headers-MIT.txt"
cat > "$PREFIX/BUILDINFO" <<EOF
Paddock FFmpeg $FFMPEG_VERSION-$REV ($ARCH)
Built from official sources (sha256-verified): FFmpeg $FFMPEG_VERSION, whisper.cpp $WHISPER_VERSION, nv-codec-headers $NVHEADERS_VERSION
Linked against Debian 13 libraries: x264, zimg, dav1d, libva, libdrm$([ "$ARCH" = amd64 ] && echo ", libvpl")
Source of this build script: https://github.com/AidenMoyer/paddock/tree/main/ffmpeg
EOF

# smoke test: the features Paddock relies on are really there
# (capture first: `ffmpeg | grep -q` would die of SIGPIPE under pipefail)
features="$("$PREFIX/bin/ffmpeg" -hide_banner -filters) $("$PREFIX/bin/ffmpeg" -hide_banner -encoders)"
want=(whisper zscale scale_cuda libx264 h264_nvenc h264_vaapi)
[ "$ARCH" = amd64 ] && want+=(h264_qsv)
for f in "${want[@]}"; do
  grep -qw "$f" <<<"$features" || { echo "missing FFmpeg feature: $f" >&2; exit 1; }
done
# Paddock's HDR → SDR chain (transcode._tonemap) on a test pattern tagged as HDR10 (BT.2020 + PQ)
"$PREFIX/bin/ffmpeg" -hide_banner -v error -f lavfi -i testsrc2=d=1 -vf "format=yuv420p10le,\
setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc,zscale=t=linear:npl=100,format=gbrpf32le,\
zscale=p=bt709,tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p" -c:v libx264 -f null -
echo "    features ok: ${want[*]}, HDR tone-mapping + x264 encode"

tar -C "$(dirname "$PREFIX")" -I 'zstd -19 -T0' -cf "$OUT/$NAME.tar.zst" "$(basename "$PREFIX")"
( cd "$OUT" && sha256sum "$NAME.tar.zst" > "$NAME.tar.zst.sha256" )
log "done: $OUT/$NAME.tar.zst ($(du -h "$OUT/$NAME.tar.zst" | cut -f1))"
