#!/usr/bin/env bash
# Build Paddock OS: a minimal Debian 13 appliance that runs Paddock and nothing else.
#
#   sudo os/build.sh [--variant standard|nvidia] [--version X.Y.Z] [--ffmpeg path/to/paddock-ffmpeg.tar.zst]
#
# Needs (Debian/Ubuntu host): mmdebstrap debian-archive-keyring squashfs-tools xorriso grub-pc-bin
#                             grub-efi-amd64-bin grub-common mtools dosfstools zstd
# Output: os/out/paddock-os[-nvidia]-<version>-amd64.iso (+ .sha256)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
VARIANT=standard
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO/server/paddock/__init__.py")"
FFMPEG_TAR=""
OUT="$HERE/out"
WORK="${WORK:-/var/tmp/paddock-os}"
MIRROR="${MIRROR:-http://deb.debian.org/debian}"
SUITE=trixie
PADDOCK_UID=568

while [ $# -gt 0 ]; do
  case "$1" in
    --variant) VARIANT="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --ffmpeg) FFMPEG_TAR="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
case "$VARIANT" in standard|nvidia) ;; *) echo "--variant must be standard or nvidia" >&2; exit 2 ;; esac
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
for t in mmdebstrap mksquashfs xorriso grub-mkrescue mformat zstd; do
  command -v "$t" >/dev/null || { echo "missing tool: $t" >&2; exit 1; }
done
if [ -z "$FFMPEG_TAR" ]; then FFMPEG_TAR="$(ls -1 "$REPO"/ffmpeg/out/paddock-ffmpeg-*-amd64.tar.zst 2>/dev/null | sort -V | tail -n1 || true)"; fi
[ -f "$FFMPEG_TAR" ] || { echo "no Paddock FFmpeg build found; run ffmpeg/build.sh first or pass --ffmpeg" >&2; exit 1; }

NAME="paddock-os$([ "$VARIANT" = nvidia ] && echo -nvidia)-$VERSION-amd64"
ROOT="$WORK/$VARIANT/rootfs"
ISO="$WORK/$VARIANT/iso"
KEYRING=/usr/share/keyrings/debian-archive-keyring.pgp
[ -f "$KEYRING" ] || KEYRING=/usr/share/keyrings/debian-archive-keyring.gpg

log() { echo "==> [$VARIANT] $*"; }

unmount_chroot() {
  for m in tmp run dev/pts dev sys proc; do
    mountpoint -q "$ROOT/$m" 2>/dev/null && umount -l "$ROOT/$m"
  done
  return 0
}
trap unmount_chroot EXIT

mount_chroot() {
  mount -t proc proc "$ROOT/proc"
  mount -t sysfs sys "$ROOT/sys"
  mount --bind /dev "$ROOT/dev"
  mount --bind /dev/pts "$ROOT/dev/pts"
  mount -t tmpfs tmpfs "$ROOT/run"
  mount -t tmpfs tmpfs "$ROOT/tmp"
}

in_chroot() {
  chroot "$ROOT" /usr/bin/env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/root LC_ALL=C.UTF-8 DEBIAN_FRONTEND=noninteractive "$@"
}

apt_install() { in_chroot apt-get install -y -q --no-install-recommends "$@"; }

pkglist() { grep -v '^\s*#' "$1" | grep -v '^\s*$' | tr '\n' ',' | sed 's/,$//'; }

unmount_chroot
rm -rf "$WORK/$VARIANT"
mkdir -p "$ROOT" "$ISO/live" "$ISO/boot/grub" "$OUT"

# ---------------------------------------------------------------------------------------------
log "bootstrapping Debian 13 (minimal)"
mmdebstrap --variant=minbase --keyring="$KEYRING" \
  --components="main contrib non-free non-free-firmware" \
  --aptopt='APT::Install-Recommends "false"' \
  --dpkgopt='path-exclude=/usr/share/doc/*' --dpkgopt='path-include=/usr/share/doc/*/copyright' \
  --dpkgopt='path-exclude=/usr/share/man/*' --dpkgopt='path-exclude=/usr/share/info/*' \
  --dpkgopt='path-exclude=/usr/share/locale/*' --dpkgopt='path-include=/usr/share/locale/locale.alias' \
  --dpkgopt='path-exclude=/usr/share/lintian/*' --dpkgopt='path-exclude=/usr/share/bug/*' \
  --include="$(pkglist "$HERE/config/packages.txt"),tzdata" \
  "$SUITE" "$ROOT" \
  "deb $MIRROR $SUITE main contrib non-free non-free-firmware" \
  "deb $MIRROR $SUITE-updates main contrib non-free non-free-firmware" \
  "deb http://security.debian.org/debian-security $SUITE-security main contrib non-free non-free-firmware"

mount_chroot
cp -L /etc/resolv.conf "$ROOT/etc/resolv.conf"
printf '#!/bin/sh\nexit 101\n' > "$ROOT/usr/sbin/policy-rc.d"; chmod 0755 "$ROOT/usr/sbin/policy-rc.d"
KVER="$(ls "$ROOT/lib/modules" | sort -V | tail -n1)"
log "kernel $KVER"

# ---------------------------------------------------------------------------------------------
if [ "$VARIANT" = nvidia ]; then
  log "NVIDIA driver: compiling the kernel module, then removing the compiler again"
  in_chroot apt-get update -q
  apt_install "linux-headers-$KVER" nvidia-kernel-dkms
  in_chroot dkms autoinstall -k "$KVER" || true
  mods="$(find "$ROOT/lib/modules/$KVER" -name 'nvidia*.ko*')"
  [ -n "$mods" ] || { echo "the NVIDIA module did not build" >&2; exit 1; }
  mkdir -p "$WORK/$VARIANT/nvidia-mods" && cp $mods "$WORK/$VARIANT/nvidia-mods/"
  # removing these lets autoremove take the compiler, headers and build tools they pulled in
  in_chroot apt-get purge -y -q nvidia-kernel-dkms dkms "linux-headers-$KVER"
  in_chroot apt-get autoremove -y -q --purge
  # the user-space packages accept a kernel module that isn't packaged by dkms
  NVVER="$(in_chroot dpkg-query -W -f='${Version}' nvidia-kernel-support 2>/dev/null || in_chroot apt-cache policy libcuda1 | sed -n 's/ *Candidate: //p')"
  NVUP="${NVVER%%-*}"
  mkdir -p "$ROOT/tmp/nvdummy/DEBIAN"
  cat > "$ROOT/tmp/nvdummy/DEBIAN/control" <<EOF
Package: paddock-nvidia-kernel
Version: $NVUP
Architecture: amd64
Maintainer: Paddock OS <noreply@github.com>
Provides: nvidia-kernel-$NVUP, nvidia-kernel-dkms (= $NVVER)
Description: NVIDIA kernel module compiled into Paddock OS at build time
EOF
  in_chroot dpkg-deb -b /tmp/nvdummy /tmp/paddock-nvidia-kernel.deb >/dev/null
  in_chroot dpkg -i /tmp/paddock-nvidia-kernel.deb
  apt_install $(grep -v '^\s*#' "$HERE/config/packages-nvidia.txt")
  install -d "$ROOT/lib/modules/$KVER/updates/nvidia"
  cp "$WORK/$VARIANT"/nvidia-mods/* "$ROOT/lib/modules/$KVER/updates/nvidia/"
  printf 'nvidia\nnvidia-uvm\n' > "$ROOT/etc/modules-load.d/paddock-nvidia.conf"
  printf 'blacklist nouveau\noptions nouveau modeset=0\n' > "$ROOT/etc/modprobe.d/paddock-no-nouveau.conf"
  rm -rf "$ROOT/lib/modules/$KVER/kernel/drivers/gpu/drm/nouveau"
  cat > "$ROOT/etc/systemd/system/paddock-nvidia.service" <<'EOF'
[Unit]
Description=Paddock: create NVIDIA device nodes for transcoding
Before=paddock.service
After=systemd-modules-load.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=-/usr/bin/nvidia-modprobe -c 0 -u
ExecStart=-/usr/bin/nvidia-smi -L
[Install]
WantedBy=multi-user.target
EOF
fi

# ---------------------------------------------------------------------------------------------
log "Paddock FFmpeg ($(basename "$FFMPEG_TAR"))"
tar -C "$ROOT/opt" -I zstd -xf "$FFMPEG_TAR"
in_chroot /opt/paddock-ffmpeg/bin/ffmpeg -hide_banner -version | head -n1
missing="$(in_chroot ldd /opt/paddock-ffmpeg/bin/ffmpeg | grep 'not found' || true)"
[ -z "$missing" ] || { echo "ffmpeg is missing libraries:" >&2; echo "$missing" >&2; exit 1; }

log "Paddock $VERSION"
in_chroot useradd --system --uid "$PADDOCK_UID" --user-group --home-dir /srv/data/paddock --no-create-home \
  --shell /usr/sbin/nologin --groups video,render paddock
install -d "$ROOT/opt/paddock/app"
cp -r "$REPO/server/paddock" "$ROOT/opt/paddock/app/"
find "$ROOT/opt/paddock/app" -name '__pycache__' -prune -exec rm -rf {} +
cp "$REPO/server/requirements.txt" "$ROOT/tmp/requirements.txt"
in_chroot python3 -m venv /opt/paddock/venv
in_chroot /opt/paddock/venv/bin/pip install -q --no-cache-dir --disable-pip-version-check -r /tmp/requirements.txt
in_chroot /opt/paddock/venv/bin/pip uninstall -q -y pip
find "$ROOT/opt/paddock/venv" -type d \( -name tests -o -name testing \) -path '*/numpy/*' -prune -exec rm -rf {} +
in_chroot python3 -m compileall -q -j 0 /opt/paddock >/dev/null || true
in_chroot apt-get purge -y -q --auto-remove python3-venv python3-pip-whl python3-setuptools-whl >/dev/null || true

log "Paddock OS configuration"
cp -r "$HERE/overlay/." "$ROOT/"
install -m 0755 "$REPO/deploy/wireguard/paddock-wg.sh" "$ROOT/usr/local/sbin/paddock-wg"
chmod 0755 "$ROOT"/usr/local/sbin/paddock-*
cat > "$ROOT/etc/paddock-os-release" <<EOF
PADDOCK_OS_VERSION=$VERSION
PADDOCK_OS_VARIANT=$VARIANT
PADDOCK_OS_BUILD=$(date -u +%Y-%m-%d)
PADDOCK_FFMPEG=$(basename "$FFMPEG_TAR" .tar.zst)
EOF
echo paddock > "$ROOT/etc/hostname"
printf '127.0.0.1\tlocalhost\n127.0.1.1\tpaddock\n::1\t\tlocalhost ip6-localhost ip6-loopback\n' > "$ROOT/etc/hosts"
echo "Etc/UTC" > "$ROOT/etc/timezone"; ln -sf /usr/share/zoneinfo/Etc/UTC "$ROOT/etc/localtime"
cat > "$ROOT/etc/mdadm/mdadm.conf" <<'EOF'
# Paddock OS: pools are found by scanning drives; Manage server → System adds ARRAY lines here.
HOMEHOST <ignore>
PROGRAM /usr/bin/logger
EOF
: > "$ROOT/etc/fstab"
install -d "$ROOT/srv/data" "$ROOT/srv/media"
in_chroot passwd -l root >/dev/null
in_chroot systemctl enable systemd-networkd systemd-resolved systemd-timesyncd \
  paddock.service paddock-wg.service paddock-agent.socket paddock-installer.service >/dev/null
in_chroot systemctl disable smartmontools.service smartd.service >/dev/null 2>&1 || true
[ "$VARIANT" = nvidia ] && in_chroot systemctl enable paddock-nvidia.service >/dev/null
in_chroot systemctl mask apt-daily.timer apt-daily-upgrade.timer e2scrub_all.timer >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------------------------
log "trimming kernel modules Paddock never uses"
M="$ROOT/lib/modules/$KVER/kernel"
rm -rf "$M/sound" "$M/drivers/net/wireless" "$M/net/wireless" "$M/net/mac80211" "$M/net/bluetooth" \
  "$M/drivers/bluetooth" "$M/drivers/infiniband" "$M/drivers/isdn" "$M/drivers/staging" "$M/drivers/iio" \
  "$M/drivers/net/can" "$M/net/can" "$M/drivers/nfc" "$M/net/nfc" "$M/drivers/input/joystick" \
  "$M/drivers/input/touchscreen" "$M/drivers/input/tablet" "$M/drivers/gpu/drm/radeon" "$M/fs/ocfs2" "$M/fs/gfs2"
find "$M/drivers/media" -mindepth 1 -maxdepth 1 ! -name cec ! -name mc -exec rm -rf {} +
in_chroot depmod -a "$KVER"
# every module we kept must still load (no dependency on something we removed)
bad="$(in_chroot depmod -a -e -F "/boot/System.map-$KVER" "$KVER" 2>&1 | grep -i 'unknown symbol' || true)"
[ -z "$bad" ] || { echo "module trimming broke dependencies:" >&2; echo "$bad" | head >&2; exit 1; }
for m in i915 xe amdgpu ahci nvme e1000e igb igc r8169 ext4 xfs btrfs ntfs3 exfat vfat raid456 raid1 raid0 raid10 wireguard usb-storage uas squashfs overlay; do
  in_chroot modprobe -S "$KVER" --show-depends "$m" >/dev/null || { echo "module $m is missing" >&2; exit 1; }
done
in_chroot update-initramfs -u -k "$KVER" >/dev/null

# ---------------------------------------------------------------------------------------------
log "cleaning up"
rm -f "$ROOT/usr/sbin/policy-rc.d"
in_chroot apt-get clean
rm -rf "$ROOT"/var/lib/apt/lists/* "$ROOT"/var/cache/apt/* "$ROOT"/var/cache/debconf/*-old "$ROOT"/var/log/*.log \
  "$ROOT"/var/log/apt/* "$ROOT"/root/.cache "$ROOT"/tmp/* "$ROOT"/var/tmp/*
: > "$ROOT/etc/machine-id"
rm -f "$ROOT/var/lib/dbus/machine-id"
unmount_chroot
ln -sf /run/systemd/resolve/stub-resolv.conf "$ROOT/etc/resolv.conf"
du -sh "$ROOT" | sed 's/^/    system size: /'

# ---------------------------------------------------------------------------------------------
log "building the ISO"
cp "$ROOT/boot/vmlinuz-$KVER" "$ISO/live/vmlinuz"
cp "$ROOT/boot/initrd.img-$KVER" "$ISO/live/initrd.img"
mksquashfs "$ROOT" "$ISO/live/filesystem.squashfs" -comp zstd -Xcompression-level 19 -b 1M -noappend -quiet \
  -e proc sys dev run tmp
touch "$ISO/PADDOCK_OS"
sed -e "s/@VERSION@/$VERSION/g" -e "s/@EDITION@/$([ "$VARIANT" = nvidia ] && echo ' (NVIDIA)')/g" \
  "$HERE/iso/grub.cfg" > "$ISO/boot/grub/grub.cfg"
grub-mkrescue --locales="" --fonts="" --themes="" -o "$OUT/$NAME.iso" "$ISO" -- -volid PADDOCK_OS >/dev/null 2>&1
( cd "$OUT" && sha256sum "$NAME.iso" > "$NAME.iso.sha256" )
log "done: $OUT/$NAME.iso ($(du -h "$OUT/$NAME.iso" | cut -f1))"
