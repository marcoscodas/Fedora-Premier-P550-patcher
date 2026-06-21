#!/usr/bin/env python3
"""
Applies the full set of HiFive Premier P550 fixes to a freshly-decompiled
Fedora Omni eic7700-hifive-premier-p550.dts, in place.

Design point: every cross-reference is resolved by node identity (a regex on
the node's opening line - hardware register addresses are stable across
Fedora releases) rather than by a hardcoded phandle number, since phandle
numbers are reassigned by dtc on every compile and are not safe to carry
across different Fedora image builds.

Usage: patch_dts.py <path-to-decompiled.dts>
Edits the file in place. Prints one line per patch: OK / SKIP / WARN.
Exit code is the number of WARNs (0 = everything applied cleanly).
"""
import re
import sys


class DTS:
    def __init__(self, path):
        self.path = path
        with open(path, encoding="utf-8") as f:
            self.lines = f.read().split("\n")
        self.warnings = []
        self._next_phandle = None

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lines))

    def ok(self, msg):
        print(f"OK:   {msg}")

    def skip(self, msg):
        print(f"SKIP: {msg}")

    def warn(self, msg):
        self.warnings.append(msg)
        print(f"WARN: {msg}")

    # ---- node/block location -------------------------------------------------

    def find_block(self, open_regex, start=0):
        """First node whose opening line matches open_regex. Returns (start, end) line
        indices (inclusive), end being the line with the closing '};' for that node."""
        pat = re.compile(open_regex)
        for i in range(start, len(self.lines)):
            if pat.search(self.lines[i]):
                depth = self.lines[i].count("{") - self.lines[i].count("}")
                j = i
                while depth > 0:
                    j += 1
                    if j >= len(self.lines):
                        return None
                    depth += self.lines[j].count("{") - self.lines[j].count("}")
                return (i, j)
        return None

    def find_all_blocks(self, open_regex):
        out = []
        pos = 0
        while True:
            r = self.find_block(open_regex, pos)
            if r is None:
                return out
            out.append(r)
            pos = r[1] + 1

    def block_from_open_line(self, open_idx):
        """Given a known node-opening line index, find its matching close."""
        depth = self.lines[open_idx].count("{") - self.lines[open_idx].count("}")
        j = open_idx
        while depth > 0:
            j += 1
            if j >= len(self.lines):
                return None
            depth += self.lines[j].count("{") - self.lines[j].count("}")
        return (open_idx, j)

    def find_node_by_property(self, prop_regex, start=0):
        """Find the node that *directly* contains a property/line matching
        prop_regex (e.g. a `compatible = "...";` value), by scanning backward
        from the match to its smallest enclosing `{`. Use this - not
        find_block() - when the identifying regex isn't the node's own
        opening line (find_block's brace counting starts at the matched line
        and would be wrong here)."""
        pat = re.compile(prop_regex)
        for i in range(start, len(self.lines)):
            if pat.search(self.lines[i]):
                balance = 0
                for j in range(i - 1, -1, -1):
                    balance += self.lines[j].count("}") - self.lines[j].count("{")
                    if balance < 0:
                        return self.block_from_open_line(j)
                return None
        return None

    def block_indent(self, start):
        return re.match(r"^(\s*)", self.lines[start]).group(1)

    def block_own_phandle(self, start, end):
        """phandle directly on this node (not a nested child's)."""
        depth = 0
        for i in range(start, end + 1):
            depth += self.lines[i].count("{") - self.lines[i].count("}")
            if depth == 1:
                m = re.search(r"phandle\s*=\s*<(0x[0-9a-fA-F]+)>;", self.lines[i])
                if m:
                    return int(m.group(1), 16)
        return None

    def max_phandle(self):
        mx = 0
        for l in self.lines:
            m = re.search(r"phandle\s*=\s*<0x([0-9a-fA-F]+)>;", l)
            if m:
                mx = max(mx, int(m.group(1), 16))
        return mx

    def alloc_phandle(self):
        if self._next_phandle is None:
            self._next_phandle = self.max_phandle()
        self._next_phandle += 1
        return self._next_phandle

    def ensure_own_phandle(self, start, end):
        ph = self.block_own_phandle(start, end)
        if ph is not None:
            return ph
        ph = self.alloc_phandle()
        indent = self.block_indent(start) + "\t"
        self.lines.insert(end, f"{indent}phandle = <{hex(ph)}>;")
        return ph

    def get_property(self, start, end, prop_name):
        depth = 0
        for i in range(start, end + 1):
            depth += self.lines[i].count("{") - self.lines[i].count("}")
            if depth == 1:
                m = re.match(rf"^\s*{re.escape(prop_name)}\s*=\s*(.*);\s*$", self.lines[i])
                if m:
                    return i, m.group(1)
        return None, None

    def set_property(self, start, end, prop_name, value, insert_after_open=True):
        """Set (replace or add) `prop_name = value;` directly on this node."""
        idx, _ = self.get_property(start, end, prop_name)
        indent = self.block_indent(start) + "\t"
        line = f"{indent}{prop_name} = {value};"
        if idx is not None:
            self.lines[idx] = line
            return "replaced"
        insert_at = start + 1 if insert_after_open else end
        self.lines.insert(insert_at, line)
        return "added"

    def set_flag(self, start, end, flag_name):
        """Set a boolean devicetree flag (e.g. `dma-noncoherent;`) if not present."""
        depth = 0
        for i in range(start, end + 1):
            depth += self.lines[i].count("{") - self.lines[i].count("}")
            if depth == 1 and re.match(rf"^\s*{re.escape(flag_name)}\s*;\s*$", self.lines[i]):
                return "present"
        indent = self.block_indent(start) + "\t"
        self.lines.insert(start + 1, f"{indent}{flag_name};")
        return "added"

    def node_phandle_by(self, open_regex, label, by_property=False):
        r = self.find_node_by_property(open_regex) if by_property else self.find_block(open_regex)
        if r is None:
            self.warn(f"{label}: node not found (pattern {open_regex!r}) - dependent patches will be skipped")
            return None
        return self.ensure_own_phandle(*r)


# =============================================================================
# Patch functions. Each takes the DTS object and does its own defensive
# checking; on anything unexpected, warn() and return without raising.
# =============================================================================

def patch_cpu_compatible(d):
    for r in d.find_all_blocks(r"(?<!\w)cpu@\d+\s*\{"):
        idx, val = d.get_property(*r, "compatible")
        if idx is None:
            d.warn(f"cpu node at line {r[0]+1}: no compatible property found")
            continue
        if 'sifive,p550' in val:
            d.skip(f"cpu node at line {r[0]+1}: compatible already correct")
            continue
        d.set_property(*r, "compatible", '"sifive,p550", "riscv"')
        d.ok(f"cpu node at line {r[0]+1}: fixed compatible -> sifive,p550")


def patch_soc_compatible(d):
    r = d.find_block(r"^\tsoc\s*\{")
    if r is None:
        d.warn("soc node not found")
        return
    idx, val = d.get_property(*r, "compatible")
    if idx is None:
        d.warn("/soc: no compatible property found")
        return
    if "fu800" in val.lower() or "FU800" in val:
        d.set_property(*r, "compatible", '"simple-bus"')
        d.ok("/soc: stripped leftover FU800-soc compatible junk")
    elif val.strip() == '"simple-bus"':
        d.skip("/soc: compatible already correct")
    else:
        d.warn(f"/soc: unexpected compatible value {val!r}, left untouched")


def patch_l3_cache(d):
    r = d.find_block(r"cache-controller@2010000\s*\{")
    if r is None:
        d.warn("cache-controller@2010000 not found")
        return
    idx, val = d.get_property(*r, "compatible")
    if idx is not None and "eswin,eic7700-l3-cache" in val:
        d.skip("cache-controller@2010000: already eswin,eic7700-l3-cache")
        return
    d.set_property(*r, "compatible", '"eswin,eic7700-l3-cache"')
    # correct reg (drop the bogus extra FU740-style range, keep just the base 0x4000)
    idx, val = d.get_property(*r, "reg")
    if idx is not None and val.count("0x") > 4:
        d.set_property(*r, "reg", "<0x00 0x2010000 0x00 0x4000>")
    for junk in ("next-level-cache", "reg-names", "sifive,a-mshr-count",
                 "sifive,bank-count", "sifive,ecc-granularity",
                 "sifive,max-master-id", "sifive,perfmon-counters"):
        idx, _ = d.get_property(*r, junk)
        if idx is not None:
            del d.lines[idx]
            r = (r[0], r[1] - 1)
    d.ok("cache-controller@2010000: fixed compatible/reg, stripped FU740-leftover properties")


def patch_pl2cache_compat(d):
    n = 0
    for i, l in enumerate(d.lines):
        if "sifive,pL2Cache0" in l:
            d.lines[i] = l.replace("sifive,pL2Cache0", "sifive,pl2cache1")
            n += 1
    if n:
        d.ok(f"per-core L2 cache: fixed {n} wrong-case compatible string(s) -> sifive,pl2cache1")
    else:
        d.skip("per-core L2 cache: no wrong-case compatible strings found")


def patch_plic(d):
    r = d.find_block(r"interrupt-controller@c000000\s*\{")
    if r is None:
        d.warn("PLIC (interrupt-controller@c000000) not found")
        return
    idx, val = d.get_property(*r, "compatible")
    if idx is not None and "eswin,eic7700-plic" not in val:
        d.set_property(*r, "compatible", '"eswin,eic7700-plic", "sifive,plic-1.0.0"')
        d.ok("PLIC: restored eswin,eic7700-plic compatible prefix")
    else:
        d.skip("PLIC: compatible already correct")
    idx, _ = d.get_property(*r, "#address-cells")
    if idx is None:
        d.set_property(*r, "#address-cells", "<0x00>")
        d.ok("PLIC: added missing #address-cells = <0>")
    else:
        d.skip("PLIC: #address-cells already present")


def patch_clint(d):
    if d.find_block(r"timer@2000000\s*\{") is not None:
        d.skip("CLINT (timer@2000000) already present")
        return
    cpu_intc_ph = []
    for r in d.find_all_blocks(r"(?<!\w)cpu@\d+\s*\{"):
        sub = d.find_block(r"interrupt-controller\s*\{", r[0])
        if sub is None or sub[0] > r[1]:
            d.warn(f"cpu node at line {r[0]+1}: no nested interrupt-controller found, CLINT skipped")
            return
        cpu_intc_ph.append(d.ensure_own_phandle(*sub))
    if len(cpu_intc_ph) != 4:
        d.warn(f"expected 4 CPU interrupt-controllers, found {len(cpu_intc_ph)} - CLINT skipped")
        return
    anchor = d.find_block(r"cache-controller@2010000\s*\{")
    if anchor is None:
        d.warn("cache-controller@2010000 not found - can't place CLINT, skipped")
        return
    parts = []
    for ph in cpu_intc_ph:
        parts += [hex(ph), "0x03", hex(ph), "0x07"]
    indent = d.block_indent(anchor[0])
    block = [
        f"{indent}timer@2000000 {{",
        f'{indent}\tcompatible = "eswin,eic7700-clint", "sifive,clint0";',
        f"{indent}\treg = <0x00 0x2000000 0x00 0x10000>;",
        f"{indent}\tinterrupts-extended = <{' '.join(parts)}>;",
        f"{indent}}};",
        "",
    ]
    d.lines[anchor[0]:anchor[0]] = block
    d.ok("CLINT (timer@2000000): added, wired to the 4 CPU interrupt-controllers")


def patch_smmu(d):
    existing = d.find_block(r"iommu@50c00000\s*\{")
    if existing is not None:
        smmu_ph = d.ensure_own_phandle(*existing)
        d.skip("SMMU (iommu@50c00000) already present")
    else:
        scu_ph = d.node_phandle_by(r"scu_sys_con@0x51810000\s*\{", "scu_sys_con")
        reset_ph = d.node_phandle_by(r'compatible = "eswin,eic7700-reset"', "reset-controller", by_property=True)
        plic_ph = d.node_phandle_by(r"interrupt-controller@c000000\s*\{", "PLIC")
        anchor = d.find_block(r"pcie@0x54000000\s*\{")
        if None in (scu_ph, reset_ph, plic_ph) or anchor is None:
            d.warn("SMMU: missing a dependency (scu/reset/plic/pcie anchor) - skipped")
            return
        smmu_ph = d.alloc_phandle()
        indent = d.block_indent(anchor[0])
        rst = [reset_ph, 0x05, 0x01, reset_ph, 0x05, 0x02, reset_ph, 0x05, 0x10,
               reset_ph, 0x05, 0x20, reset_ph, 0x05, 0x40, reset_ph, 0x05, 0x80,
               reset_ph, 0x05, 0x100, reset_ph, 0x05, 0x200, reset_ph, 0x05, 0x400,
               reset_ph, 0x05, 0x800]
        rst_str = " ".join(hex(v) for v in rst)
        block = [
            f"{indent}iommu@50c00000 {{",
            f'{indent}\tcompatible = "arm,smmu-v3";',
            f"{indent}\teswin,syscfg = <{hex(scu_ph)} 0x3fc>;",
            f"{indent}\tinterrupts = <0x164 0x168 0x165 0x166>;",
            f'{indent}\tinterrupt-names = "eventq", "gerror", "priq", "cmdq-sync";',
            f"{indent}\tinterrupt-parent = <{hex(plic_ph)}>;",
            f"{indent}\t#iommu-cells = <0x01>;",
            f"{indent}\treg = <0x00 0x50c00000 0x00 0x100000>;",
            f"{indent}\tresets = <{rst_str}>;",
            f'{indent}\treset-names = "axi_rst", "cfg_rst", "tbu0_rst", "tbu1_rst", "tbu2_rst", "tbu3_rst", "tbu4_rst", "tbu5_rst", "tbu6_rst", "tbu7_rst";',
            f'{indent}\tstatus = "okay";',
            f"{indent}\tphandle = <{hex(smmu_ph)}>;",
            f"{indent}}};",
            "",
        ]
        d.lines[anchor[0]:anchor[0]] = block
        d.ok("SMMU (iommu@50c00000): added")

    pcie = d.find_block(r"pcie@0x54000000\s*\{")
    if pcie is None:
        d.warn("pcie@0x54000000 not found - can't wire iommu-map")
        return
    idx, val = d.get_property(*pcie, "iommu-map")
    if idx is not None:
        d.skip("PCIe: iommu-map already present")
        return
    d.set_property(*pcie, "iommu-map", f"<0x00 {hex(smmu_ph)} 0xff0000 0xffffff>", insert_after_open=False)
    d.ok("PCIe: wired iommu-map to SMMU")


def patch_regulator_and_pinctrl(d):
    if d.find_block(r"^\tvcc1v8\s*\{") is not None:
        d.skip("vcc1v8 regulator already present")
        vcc_ph = d.ensure_own_phandle(*d.find_block(r"^\tvcc1v8\s*\{"))
    else:
        anchor = d.find_block(r"^\taliases\s*\{")
        if anchor is None:
            d.warn("aliases node not found - can't place vcc1v8 regulator, skipped")
            return
        vcc_ph = d.alloc_phandle()
        block = [
            "\tvcc1v8 {",
            '\t\tcompatible = "regulator-fixed";',
            '\t\tregulator-name = "vcc1v8";',
            "\t\tregulator-always-on;",
            "\t\tregulator-boot-on;",
            "\t\tregulator-min-microvolt = <0x1b7740>;",
            "\t\tregulator-max-microvolt = <0x1b7740>;",
            f"\t\tphandle = <{hex(vcc_ph)}>;",
            "\t};",
            "",
        ]
        d.lines[anchor[0]:anchor[0]] = block
        d.ok("vcc1v8 fixed-regulator: added")

    pinctrl = d.find_block(r"pinctrl@0x51600080\s*\{")
    if pinctrl is None:
        d.warn("pinctrl@0x51600080 not found - can't wire vrgmii-supply")
        return
    idx, _ = d.get_property(*pinctrl, "vrgmii-supply")
    if idx is not None:
        d.skip("pinctrl: vrgmii-supply already present")
        return
    d.set_property(*pinctrl, "vrgmii-supply", f"<{hex(vcc_ph)}>")
    d.ok("pinctrl: wired vrgmii-supply to vcc1v8")


def patch_disable_dsi_hdcp(d):
    targets = [
        (r"^\t\tdsi-output\s*\{", "dsi-output"),
        (r"mipi_dsi@50270000\s*\{", "mipi_dsi@50270000"),
        (r"dsi_panel@0\s*\{", "dsi_panel@0"),
        (r"hdmi-hdcp2@50290000\s*\{", "hdmi-hdcp2@50290000"),
    ]
    for regex, name in targets:
        r = d.find_block(regex)
        if r is None:
            d.warn(f"{name}: node not found")
            continue
        idx, val = d.get_property(*r, "status")
        if idx is not None and '"disabled"' in val:
            d.skip(f"{name}: already disabled")
            continue
        d.set_property(*r, "status", '"disabled"')
        d.ok(f"{name}: disabled")


def patch_ethernet_phy_mode(d):
    n = 0
    for r in d.find_all_blocks(r"ethernet@504\d0000\s*\{"):
        idx, val = d.get_property(*r, "phy-mode")
        if idx is None:
            d.warn(f"ethernet node at line {r[0]+1}: no phy-mode property")
            continue
        if val.strip() == '"rgmii-txid"':
            continue
        d.set_property(*r, "phy-mode", '"rgmii-txid"')
        n += 1
    if n:
        d.ok(f"Ethernet: set phy-mode = rgmii-txid on {n} MAC(s) (vendor-confirmed correct for this board)")
    else:
        d.skip("Ethernet: phy-mode already rgmii-txid on both MACs")


def patch_spi_compat(d):
    n = 0
    for r in d.find_all_blocks(r"spi@5081[04]000\s*\{"):
        idx, val = d.get_property(*r, "compatible")
        if idx is not None and val.strip() == '"eswin,eic770x-spi"':
            continue
        d.set_property(*r, "compatible", '"eswin,eic770x-spi"')
        n += 1
    if n:
        d.ok(f"SPI controllers: fixed compatible on {n} node(s)")
    else:
        d.skip("SPI controllers: compatible already correct")


def patch_spi_flash_wp(d):
    r = d.find_block(r"spi@51800000\s*\{")
    if r is None:
        d.warn("boot spi@51800000 not found")
        return
    idx, _ = d.get_property(*r, "wp-gpios")
    if idx is not None:
        d.skip("boot spi flash: wp-gpios already present")
        return
    csidx, csval = d.get_property(*r, "cs-gpios")
    if csidx is None:
        d.warn("boot spi flash: cs-gpios not found, can't derive gpio phandle for wp-gpios")
        return
    m = re.match(r"<\s*(0x[0-9a-fA-F]+)", csval)
    if not m:
        d.warn(f"boot spi flash: couldn't parse cs-gpios value {csval!r}")
        return
    gpio_ph = m.group(1)
    d.set_property(*r, "wp-gpios", f"<{gpio_ph} 0x04 0x01>")
    d.ok("boot spi flash: added wp-gpios")


def patch_gpio_port0_irq(d):
    r = d.find_block(r"gpio-port@0\s*\{")
    if r is None:
        d.warn("gpio-port@0 not found")
        return
    depth = 0
    has_ic = False
    for i in range(r[0], r[1] + 1):
        depth += d.lines[i].count("{") - d.lines[i].count("}")
        if depth == 1 and re.match(r"^\s*interrupt-controller\s*;\s*$", d.lines[i]):
            has_ic = True
    if has_ic:
        d.skip("gpio-port@0: interrupt-controller already present")
        return
    indent = d.block_indent(r[0]) + "\t"
    d.lines.insert(r[0] + 1, f"{indent}interrupt-controller;\n{indent}#interrupt-cells = <0x02>;")
    d.ok("gpio-port@0: added interrupt-controller / #interrupt-cells")


def patch_usb1_tbus(d):
    r = d.find_block(r"dwc3@50490000\s*\{")
    if r is None:
        d.warn("usb1 dwc3@50490000 not found")
        return
    idx, _ = d.get_property(*r, "tbus")
    if idx is not None:
        d.skip("usb1 dwc3: tbus already present")
        return
    d.set_property(*r, "tbus", "<0x02>")
    d.ok("usb1 dwc3: added tbus")


def patch_display_vpll(d):
    pc = d.find_block(r"power-controller@51808000\s*\{")
    if pc is None:
        d.warn("power-controller@51808000 not found - display power-domain fix skipped")
        pc_ph = None
    else:
        idx, _ = d.get_property(*pc, "#power-domain-cells")
        if idx is None:
            d.set_property(*pc, "#power-domain-cells", "<0x01>")
            d.ok("power-controller: added #power-domain-cells")
        else:
            d.skip("power-controller: #power-domain-cells already present")
        pc_ph = d.ensure_own_phandle(*pc)

    ds = d.find_block(r"^\t\tdisplay-subsystem\s*\{")
    if ds is None:
        d.warn("display-subsystem not found")
    else:
        idx, _ = d.get_property(*ds, "power-domains")
        if idx is None and pc_ph is not None:
            d.set_property(*ds, "power-domains", f"<{hex(pc_ph)} 0x03>")
            d.ok("display-subsystem: added power-domains")
        elif idx is not None:
            d.skip("display-subsystem: power-domains already present")
        d.set_flag(*ds, "dma-noncoherent")

    dc = d.find_block(r"display_control@502c0000\s*\{")
    if dc is None:
        d.warn("display_control@502c0000 not found")
        return
    idx, val = d.get_property(*dc, "clock-names")
    if idx is not None and "vpll_fout1" in val:
        d.skip("display_control: VPLL clock chain already present")
        return
    clk_idx, clk_val = d.get_property(*dc, "clocks")
    if clk_idx is None or idx is None:
        d.warn("display_control: clocks/clock-names property missing entirely")
        return
    m = re.match(r"<(0x[0-9a-fA-F]+)", clk_val)
    if not m:
        d.warn(f"display_control: couldn't parse clocks value {clk_val!r}")
        return
    clkctl_ph = m.group(1)
    new_clocks = clk_val[:-1] + f" {clkctl_ph} 0x2b {clkctl_ph} 0x0b {clkctl_ph} 0x0d>"
    new_names = val + ', "pix_mux", "spll2_fout2", "vpll_fout1"'
    d.set_property(*dc, "clocks", new_clocks)
    d.set_property(*dc, "clock-names", new_names)
    d.ok("display_control: added pix_mux/spll2_fout2/vpll_fout1 (dedicated video PLL chain)")


PATCHES = [
    patch_cpu_compatible,
    patch_soc_compatible,
    patch_l3_cache,
    patch_pl2cache_compat,
    patch_plic,
    patch_clint,
    patch_smmu,
    patch_regulator_and_pinctrl,
    patch_disable_dsi_hdcp,
    patch_ethernet_phy_mode,
    patch_spi_compat,
    patch_spi_flash_wp,
    patch_gpio_port0_irq,
    patch_usb1_tbus,
    patch_display_vpll,
]


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    d = DTS(sys.argv[1])
    for p in PATCHES:
        print(f"--- {p.__name__} ---")
        p(d)
    d.save()
    print(f"\n{len(d.warnings)} warning(s).")
    sys.exit(len(d.warnings))


if __name__ == "__main__":
    main()
