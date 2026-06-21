# Fedora Omni to HiFive Premier P550 auto-patcher

Applies this project's full set of devicetree and image fixes to a fresh Fedora RISC-V "Omni" image for the SiFive HiFive Premier P550, so a new image drop doesn't mean redoing the investigation by hand.

## Usage

Runs on native Linux or inside WSL2. Needs `dtc`, `losetup`, `mount`, and `python3`.

```bash
sudo ./patch-fedora-omni.sh /path/to/Fedora-Server-Host-Omni-*.raw.xz
```

Accepts `.raw.xz`, `.zst`, or `.raw`. Produces `<input>.patched.raw` next to the input, uncompressed and ready to flash.

Safe to re-run on the same or a newer image. Every patch checks its target's current state first, so re-running it, or running it against an image where some fixes already landed upstream, just prints `SKIP` for what's already correct instead of double-applying or corrupting anything.

## What it patches

| Area | Fix |
|---|---|
| CPU nodes | `compatible` to `"sifive,p550","riscv"` |
| `/soc` | strips a leftover `FU800-soc` string from an unrelated SoC family |
| L3 cache | fixes `compatible`/`reg`, strips bogus FU740-template properties |
| Per-core L2 caches | fixes wrong-case compatible (`pL2Cache0` to `pl2cache1`) |
| PLIC | restores `eswin,eic7700-plic` compatible prefix, adds `#address-cells` |
| CLINT | adds the node back, wired to all 4 CPU interrupt-controllers |
| SMMU/IOMMU | adds the node back, wires PCIe's `iommu-map` to it |
| Power rail | adds `vcc1v8` regulator, wires it to pinctrl's `vrgmii-supply` |
| DSI/HDCP2 | disables `dsi-output`, `mipi_dsi`, `dsi_panel@0`, `hdmi-hdcp2` |
| Ethernet | forces `phy-mode = "rgmii-txid"` (confirmed correct against ESWIN's own vendor tree) |
| SPI | restores correct `compatible`; adds boot-flash `wp-gpios` |
| GPIO port 0 | adds `interrupt-controller`/`#interrupt-cells` |
| USB1 | adds missing `tbus` property |
| Display | adds the missing Video-PLL clock chain and power-domain wiring, fixing capped display resolutions |

It also forces GRUB to load the patched DTB. This board's firmware otherwise silently supplies its own DTB instead of the patched one on disk, and the kernel boots with the wrong devicetree and hangs without this fix. It configures `fancontrol` (`MINTEMP=25`, `MAXTEMP=65`, PWM 25 to 255), and trims the image to this board's DTB only (Fedora's Omni image ships roughly 40 boards' worth).

## Design: phandles resolved by identity, not hardcoded

`dtc` reassigns phandle numbers fresh on every compile, based on whatever nodes exist in that build. Hardcoding "the clock-controller is `0x03`" would silently wire a new property to whatever node holds that number in a different image, and that's the kind of wrong that doesn't fail loudly. So every cross-reference is looked up by the target's own identity instead: a stable hardware register address, or a distinguishing property. Two helpers in `patch_dts.py`'s `DTS` class handle this:

- `find_block(open_regex)`: pattern matches the node's own opening line.
- `find_node_by_property(prop_regex)`: pattern matches a property inside the node. Scans backward to find the real enclosing `{`, since brace counting from a property line (which has no braces of its own) would be wrong.

Mixing these up caused one of the bugs caught in testing, described below.

## Defensive behavior

Each patch checks its target's current state before changing anything, then prints `OK:` if it applied, `SKIP:` if the target was already correct, or `WARN:` if the target wasn't found or was in an unexpected shape. A `WARN:` skips that one patch and the run continues. Exit code is the warning count, so 0 means a clean run. A future image with a structure this script doesn't recognize should produce warnings to review, not a silent miscompile or an aborted run.

## Known limitations

NPU support is out of scope. ESWIN's NPU kernel module is version-locked to their vendor kernel (it's `vermagic`-checked) and isn't a portable standalone module. Using it means running ESWIN's vendor kernel, not Fedora's.

Two fixes are reasoning-based rather than independently confirmed on hardware: the Ethernet `phy-mode` revert, which is corroborated by ESWIN's own vendor devicetree but hadn't been re-tested on real hardware as of writing, and the display Video-PLL clock fix, which is structurally sound but hadn't been checked against an actual ultrawide monitor. Verify both before relying on them.

The script assumes this project's partition layout (GPT: p1 EFI vfat, p2 boot ext4, p3 root btrfs with a `root` subvolume). A layout change means updating the mount logic in `patch-fedora-omni.sh`, not `patch_dts.py`.

Kernel release string and partition UUIDs are auto-detected at run time, from the `dtb-*` directory name and `blkid`. These are the only two things that can't be hardcoded, since they change every release.

## Adding a new patch

Write `def patch_foo(d: DTS): ...` that does its own state checks and calls `d.ok()`, `d.skip()`, or `d.warn()`, then add it to the `PATCHES` list near the bottom of `patch_dts.py`. Order only matters where one patch reads another's output. CLINT and SMMU, for example, both need CPU and PLIC phandles resolved first.

## Testing performed

Run against the original, untouched Fedora image, with output independently re-verified by mounting the produced `.patched.raw` fresh and checking dtb count, every patch's actual effect, and the GRUB/fancontrol config. This caught three real bugs: an unanchored regex matching `cpu@0` inside `lpcpu@0` as a substring, a phandle lookup using the wrong search method and fabricating a bogus phandle instead of finding the real one, and a string-slicing bug that ate a closing quote and broke the `dtc` compile.

## Flashing the patched image

Copy `<input>.patched.raw` to a USB drive as a `.raw` file. On the board, burn it onto the eMMC with ESWIN and SiFive's `es_burn write` command, following SiFive's [image update procedure](https://www.sifive.com/document-file/hifive-premier-p550-image-update-procedure).

If the board doesn't already have a bootloader flashed, flash [this bootloader image](https://github.com/sifiveinc/freedom-u-sdk/releases/tag/2025.11.00-HFP550) first; the patches in this repo correspond to that bootloader version.
