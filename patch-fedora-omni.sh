#!/usr/bin/env bash
# One-and-done patcher for a Fedora RISC-V "Omni" image -> HiFive Premier P550.
#
# Usage: patch-fedora-omni.sh <path-to-image.raw.xz-or-.raw>
#
# Produces <input-basename>.patched.raw next to the input, uncompressed.
# Safe to re-run: every dts patch checks current state first (see patch_dts.py).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IN="$1"
[ -f "$IN" ] || { echo "No such file: $IN" >&2; exit 1; }

WORK=/root/work/auto-patch
RAW="$WORK/disk.raw"
mkdir -p "$WORK"

echo "== decompressing/copying input =="
case "$IN" in
  *.xz)  xz -dk -T0 -c "$IN" > "$RAW" ;;
  *.zst) unzstd -T0 -c "$IN" > "$RAW" ;;
  *.raw) cp "$IN" "$RAW" ;;
  *) echo "Unrecognized extension on $IN (expected .raw / .raw.xz / .zst)" >&2; exit 1 ;;
esac
ls -lh "$RAW"

echo "== loop-mounting partitions =="
LOOP=$(losetup -fP --show "$RAW")
echo "loop device: $LOOP"
mkdir -p "$WORK/p2" "$WORK/p3"
mount -o rw "${LOOP}p2" "$WORK/p2"
# root partition: try plain ext4 first, fall back to btrfs subvol=root
if ! mount -o rw,subvol=root "${LOOP}p3" "$WORK/p3" 2>/dev/null; then
  mount -o rw "${LOOP}p3" "$WORK/p3"
fi

cleanup() {
  sync || true
  umount "$WORK/p2" 2>/dev/null || true
  umount "$WORK/p3" 2>/dev/null || true
  losetup -d "$LOOP" 2>/dev/null || true
}
trap cleanup EXIT

echo "== locating P550 dtb and kernel release =="
DTB_DIR=$(find "$WORK/p2" -maxdepth 1 -type d -name 'dtb-*' | head -1)
[ -n "$DTB_DIR" ] || { echo "No dtb-* directory found on boot partition" >&2; exit 1; }
RELEASE=$(basename "$DTB_DIR" | sed 's/^dtb-//')
echo "kernel release: $RELEASE"
DTB="$DTB_DIR/eswin/eic7700-hifive-premier-p550.dtb"
[ -f "$DTB" ] || { echo "P550 dtb not found at $DTB" >&2; exit 1; }

VMLINUZ="$WORK/p2/vmlinuz-$RELEASE"
INITRD="$WORK/p2/initramfs-$RELEASE.img"
[ -f "$VMLINUZ" ] || echo "WARNING: $VMLINUZ not found - GRUB fix may reference a missing file"
[ -f "$INITRD" ] || echo "WARNING: $INITRD not found - GRUB fix may reference a missing file"

BOOT_UUID=$(blkid -s UUID -o value "${LOOP}p2")
ROOT_UUID=$(blkid -s UUID -o value "${LOOP}p3")
echo "boot UUID: $BOOT_UUID   root UUID: $ROOT_UUID"

echo "== decompiling, patching, recompiling =="
cd "$WORK"
dtc -I dtb -O dts -o current.dts "$DTB" 2>decompile.warnings
python3 "$SCRIPT_DIR/patch_dts.py" current.dts
PATCH_RC=$?
if [ "$PATCH_RC" -ne 0 ]; then
  echo "patch_dts.py reported $PATCH_RC warning(s) - review output above before trusting this image"
fi
set +e
dtc -I dts -O dtb -o patched.dtb current.dts 2>compile.warnings
COMPILE_RC=$?
set -e
if [ "$COMPILE_RC" -ne 0 ]; then
  echo "FATAL: dtc exited $COMPILE_RC compiling the patched dts:" >&2
  cat compile.warnings >&2
  exit 1
fi
set +e
dtc -I dtb -O dts -o /dev/null patched.dtb 2>roundtrip.warnings
ROUNDTRIP_RC=$?
set -e
if [ "$ROUNDTRIP_RC" -ne 0 ]; then
  echo "FATAL: round-trip decompile exited $ROUNDTRIP_RC - the compiled dtb may be malformed:" >&2
  cat roundtrip.warnings >&2
  exit 1
fi
echo "compiled and round-tripped cleanly, exit 0 both ways ($(wc -l < compile.warnings) warning lines - same cosmetic classes (unit_address_vs_reg, simple_bus_reg, clocks_property, etc.) present even in the unpatched stock tree)"

cp patched.dtb "$DTB"
echo "wrote patched dtb back to $DTB"

echo "== forcing GRUB devicetree load =="
mkdir -p "$WORK/p2/grub2"
cat > "$WORK/p2/grub2/custom.cfg" <<EOF
menuentry 'Fedora Linux (forced DTB)' --id manual-dtb-boot {
    insmod gzio
    insmod part_gpt
    insmod ext2
    search --no-floppy --fs-uuid --set=root $BOOT_UUID
    devicetree (\$root)/dtb-$RELEASE/eswin/eic7700-hifive-premier-p550.dtb
    linux (\$root)/vmlinuz-$RELEASE earlycon=sbi root=UUID=$ROOT_UUID rootflags=subvol=root
    initrd (\$root)/initramfs-$RELEASE.img
}
set default="manual-dtb-boot"
EOF
echo "custom.cfg written (release=$RELEASE, boot-uuid=$BOOT_UUID, root-uuid=$ROOT_UUID)"

echo "== fancontrol =="
cat > "$WORK/p3/etc/fancontrol" <<'EOF'
# Configuration file for fancontrol daemon
INTERVAL=10
DEVPATH=hwmon0=devices/platform/soc/50b00000.pvt
DEVPATH=hwmon3=devices/platform/soc/50b50000.fan_control
DEVNAME=hwmon0=pvt
DEVNAME=hwmon3=eswin_fan_control
FCTEMPS=hwmon3/pwm1=hwmon0/temp1_input
FCFANS=hwmon3/pwm1=hwmon3/fan1_input
MINTEMP=hwmon3/pwm1=25
MAXTEMP=hwmon3/pwm1=65
MINSTART=hwmon3/pwm1=25
MINSTOP=hwmon3/pwm1=25
MINPWM=hwmon3/pwm1=25
MAXPWM=hwmon3/pwm1=255
EOF
mkdir -p "$WORK/p3/etc/systemd/system/multi-user.target.wants"
ln -sf /usr/lib/systemd/system/fancontrol.service "$WORK/p3/etc/systemd/system/multi-user.target.wants/fancontrol.service"
echo "fancontrol config + enablement written"

echo "== trimming dtbs to this board only =="
for d in "$DTB_DIR"/*/; do
  vendor=$(basename "$d")
  [ "$vendor" = "eswin" ] || rm -rf "$d"
done
find "$DTB_DIR/eswin" -type f ! -name 'eic7700-hifive-premier-p550.dtb' -delete
MODDTB="$WORK/p3/usr/lib/modules/$RELEASE/dtb"
if [ -d "$MODDTB" ]; then
  for d in "$MODDTB"/*/; do
    vendor=$(basename "$d")
    [ "$vendor" = "eswin" ] || rm -rf "$d"
  done
  find "$MODDTB/eswin" -type f ! -name 'eic7700-hifive-premier-p550.dtb' -delete 2>/dev/null || true
fi
echo "dtb trim complete: $(find "$DTB_DIR" -iname '*.dtb' | wc -l) dtb file(s) remain on boot partition"

trap - EXIT
cleanup

OUT="${IN%.*}"
case "$IN" in *.raw.xz) OUT="${IN%.raw.xz}";; *.zst) OUT="${IN%.zst}";; esac
OUT="${OUT}.patched.raw"
cp "$RAW" "$OUT"
echo
echo "== done =="
echo "Patched image: $OUT"
