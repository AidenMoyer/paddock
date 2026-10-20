#!/usr/bin/env bash
# Boot-test a Paddock OS ISO in QEMU (used by CI before anything is published).
#
#   sudo os/test/smoke.sh path/to/paddock-os.iso [live|full]
#
#   live:  boot "Try Paddock OS" and wait until Paddock answers over HTTPS
#   full:  + unattended install to a virtual disk, boot it with BIOS and with UEFI, create the first admin,
#          and build a RAID 1 pool from two blank virtual drives through Manage server → System's API
set -euo pipefail

ISO="$(realpath "$1")"
MODE="${2:-full}"
W="$(mktemp -d)"
trap 'pkill -f "qemu-system-x86_64.*$W" 2>/dev/null || true' EXIT
KVM=(); [ -w /dev/kvm ] && KVM=(-enable-kvm -cpu host)
QEMU=(qemu-system-x86_64 "${KVM[@]}" -m 3072 -smp 2 -nographic -no-reboot)
OVMF_CODE=/usr/share/OVMF/OVMF_CODE_4M.fd
OVMF_VARS=/usr/share/OVMF/OVMF_VARS_4M.fd

step() { echo; echo "==== $*"; }
die() { echo "FAILED: $*" >&2; for f in "$W"/*.log; do echo "---- $f (last 60 lines)"; tail -n 60 "$f"; done; exit 1; }

wait_health() {  # port seconds logfile
  local end=$((SECONDS + $2))
  while [ $SECONDS -lt $end ]; do
    if curl -fsk --max-time 3 "https://127.0.0.1:$1/api/health" >/dev/null 2>&1; then return 0; fi
    sleep 3
  done
  return 1
}

xorriso -osirrox on -indev "$ISO" -extract /live/vmlinuz "$W/vmlinuz" -extract /live/initrd.img "$W/initrd.img" >/dev/null 2>&1
[ -s "$W/vmlinuz" ] || die "kernel not found on the ISO"

step "live boot"
"${QEMU[@]}" -kernel "$W/vmlinuz" -initrd "$W/initrd.img" -append "boot=live paddock.live console=ttyS0,115200" \
  -cdrom "$ISO" -nic "user,model=virtio-net-pci,hostfwd=tcp::18443-:443" >"$W/live.log" 2>&1 </dev/null &
pid=$!
wait_health 18443 300 || die "Paddock did not come up in live mode"
echo "Paddock answers in live mode"
curl -fsk https://127.0.0.1:18443/api/auth/state | grep -q '"setup_required":true' || die "unexpected first-run state"
kill "$pid"; wait "$pid" 2>/dev/null || true
[ "$MODE" = live ] && { echo "live test passed"; exit 0; }

step "unattended install"
qemu-img create -q -f qcow2 "$W/system.qcow2" 24G
qemu-img create -q -f qcow2 "$W/data1.qcow2" 4G
qemu-img create -q -f qcow2 "$W/data2.qcow2" 4G
timeout 1200 "${QEMU[@]}" -kernel "$W/vmlinuz" -initrd "$W/initrd.img" \
  -append "boot=live paddock.installer paddock.autoinstall=/dev/vda console=ttyS0,115200" \
  -cdrom "$ISO" -drive "file=$W/system.qcow2,if=virtio" -nic none >"$W/install.log" 2>&1 </dev/null || true
grep -q PADDOCK-INSTALL-OK "$W/install.log" || die "the installer did not finish"
echo "installed"

boot_installed() {  # name port [extra qemu args]
  local name="$1" port="$2"; shift 2
  "${QEMU[@]}" "$@" -drive "file=$W/system.qcow2,if=virtio" -drive "file=$W/data1.qcow2,if=virtio" \
    -drive "file=$W/data2.qcow2,if=virtio" -nic "user,model=virtio-net-pci,hostfwd=tcp::$port-:443" \
    >"$W/$name.log" 2>&1 </dev/null &
  echo $!
}

step "installed system, BIOS boot"
pid=$(boot_installed bios 18444)
wait_health 18444 300 || die "installed system did not come up (BIOS)"
echo "Paddock answers after a BIOS boot"

step "first admin + System API + RAID 1 pool"
api() { curl -fsk -b "$W/jar" -c "$W/jar" -H 'content-type: application/json' -H 'x-paddock: 1' "$@"; }
PW="ci-$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
api -d "{\"username\":\"ci\",\"password\":\"$PW\",\"server_name\":\"CI\"}" https://127.0.0.1:18444/api/auth/setup >/dev/null || die "setup failed"
api https://127.0.0.1:18444/api/admin/system | grep -q '"managed":true' || die "the system agent is not reachable"
disks="$(api https://127.0.0.1:18444/api/admin/system/disks)" || die "listing drives failed"
echo "$disks" | grep -q '"/dev/vdb"' || die "data drive vdb not listed: $disks"
echo "$disks" | grep -q '"role":"system"' || die "system disk not recognised"
api -d '{"name":"ci","level":"raid1","disks":["/dev/vdb","/dev/vdc"],"confirm":"ci"}' \
  https://127.0.0.1:18444/api/admin/system/pools >/dev/null || die "creating the RAID 1 pool failed"
for _ in $(seq 1 30); do
  api https://127.0.0.1:18444/api/admin/system/disks | grep -q '"mountpoint":"/srv/media/ci"' && break
  sleep 2
done
api https://127.0.0.1:18444/api/admin/system/disks | grep -q '"mountpoint":"/srv/media/ci"' || die "the pool was not mounted for media"
api "https://127.0.0.1:18444/api/fs/browse?path=/srv/media" | grep -q '"/srv/media/ci"' || die "Paddock can't see the pool"
echo "RAID 1 pool created, mounted and visible to Paddock"
api -X PUT -d '{"engine":"ffmpeg"}' https://127.0.0.1:18444/api/admin/stt | grep -q '"ffmpeg_has_whisper":true' \
  || die "Paddock's FFmpeg on the OS has no whisper filter"
echo "built-in transcription (FFmpeg whisper) is available"
kill "$pid"; wait "$pid" 2>/dev/null || true

step "installed system, UEFI boot (pool must come back after a reboot)"
cp "$OVMF_VARS" "$W/vars.fd"
pid=$(boot_installed uefi 18445 -drive "if=pflash,format=raw,readonly=on,file=$OVMF_CODE" -drive "if=pflash,format=raw,file=$W/vars.fd")
wait_health 18445 300 || die "installed system did not come up (UEFI)"
for _ in $(seq 1 20); do
  api https://127.0.0.1:18445/api/admin/system/disks | grep -q '"mountpoint":"/srv/media/ci"' && break
  sleep 3
done
api https://127.0.0.1:18445/api/admin/system/disks | grep -q '"mountpoint":"/srv/media/ci"' || die "the pool did not come back after a reboot"
echo "Paddock answers after a UEFI boot and the pool is mounted again"
kill "$pid"; wait "$pid" 2>/dev/null || true

echo; echo "all Paddock OS tests passed"
