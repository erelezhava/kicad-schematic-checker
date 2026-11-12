"""STM32CubeMX .ioc vs schematic pin check (kicad_checker.cubemx)."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from kicad_checker import cli, cubemx

IOC = r"""#MicroXplorer Configuration settings - do not modify
Mcu.CPN=STM32H743VIH6
Mcu.Name=STM32H743V(G-I)Hx
Mcu.Package=TFBGA100
PA11.Mode=Host_Only
PA11.Signal=USB_OTG_FS_DM
PB10.GPIO_Label=EEPROM1_SCL
PB10.Signal=I2C2_SCL
PE0.GPIO_Label=UHF_RX
PE0.Signal=UART8_RX
PE1.GPIO_Label=UHF_TX
PE1.Signal=UART8_TX
PC10.GPIO_Label=LoRa_RESET
PC10.Signal=GPIO_Output
PC14-OSC32_IN\ (OSC32_IN).Signal=RCC_OSC32_IN
PH1-OSC_OUT\ (PH1).Signal=RCC_OSC_OUT
PD7.Signal=GPIO_Output
PK3.Signal=GPIO_Input
VP_SYS_VS_Systick.Signal=SYS_VS_Systick
"""

PINS = {  # ball: (symbol name, net)
    "C10": ("PA11", "USB_OTG_FS_D-"), "J7": ("PB10", "/MCU/EEPROM1_I2C2_SCL"),
    "D4": ("PE0", "UHF_UART8_TX"),        # swapped with PE1
    "C4": ("PE1", "UHF_UART8_RX"),
    "B9": ("PC10", "LORA_RESET"), "A1": ("PC14", "/MCU/OSC_32_IN"),
    "D1": ("PH1", "unconnected-(U1-PH1-PadD1)"),
    "C7": ("PD7", "GND"),                 # configured output on ground
    "E5": ("PA5", "SPARE_SIGNAL"),        # connected, not configured
    "E6": ("PA6", "GND"),                 # unused pin tied low
    "E1": ("NRST", "NRST"),
}


def circuit():
    comp = {"reference": "U1", "value": "STM32H743VIH6", "part_number": "STM32H743VIH6",
            "symbol_pins": {b: {"name": n} for b, (n, _) in PINS.items()},
            "pins": {b: {"net": net} for b, (_, net) in PINS.items()}}
    nets = {}
    for b, (_, net) in PINS.items():
        nets.setdefault(net, [{"ref": "U1", "pin": b}])
    for net, nodes in nets.items():
        if not net.startswith("unconnected"):
            nodes.append({"ref": "J1", "pin": "1"})
    return {"components": {"U1": comp, "C1": {"reference": "C1", "value": "100n"}},
            "nets": [{"name": n, "nodes": v} for n, v in nets.items()]}


class Pins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ioc = Path(self.tmp.name) / "board.ioc"
        self.ioc.write_text(IOC)
        self.report = cubemx.check(circuit(), self.ioc)
        self.by_pin = {r["pin"]: r for r in self.report["results"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_mcu_is_found_and_virtual_pins_ignored(self):
        self.assertEqual(self.report["reference"], "U1")
        self.assertNotIn("VP_SYS_VS_Systick", self.by_pin)

    def test_matching_names_pass(self):
        for pin in ("PA11", "PB10", "PC10", "PC14"):
            self.assertEqual(self.by_pin[pin]["status"], "pass", (pin, self.by_pin[pin]))

    def test_swapped_uart_is_flagged_with_the_role(self):
        self.assertEqual(self.by_pin["PE0"]["status"], "needs_review")
        self.assertIn("role differs", self.by_pin["PE0"]["reason"])

    def test_configured_but_unconnected(self):
        self.assertEqual(self.by_pin["PH1"]["status"], "needs_review")
        self.assertIn("unconnected", self.by_pin["PH1"]["reason"])

    def test_configured_pin_on_ground_fails(self):
        self.assertEqual(self.by_pin["PD7"]["status"], "fail")

    def test_pin_missing_from_symbol(self):
        self.assertIn("no such pin", self.by_pin["PK3"]["reason"])

    def test_schematic_only_pins(self):
        self.assertEqual(self.by_pin["PA5"]["status"], "needs_review")
        self.assertEqual(self.by_pin["PA6"]["status"], "info")
        self.assertNotIn("NRST", self.by_pin)

    def test_cli_writes_report(self):
        cj = Path(self.tmp.name) / "circuit.json"
        cj.write_text(json.dumps(circuit()))
        out = Path(self.tmp.name) / "out"
        with redirect_stdout(io.StringIO()):
            code = cli.main(["mcu-pins", str(cj), "--ioc", str(self.ioc), "--output", str(out)])
        self.assertEqual(code, 1)
        md = (out / "mcu-pins.md").read_text()
        self.assertIn("| fail | PD7 |", md)


def rail_circuit(pins, extra_nodes=None):
    """pins: {ball: (symbol pin name, net)}; extra_nodes: {net: [node dicts]} besides the MCU pin."""
    comp = {"reference": "U1", "value": "STM32H743ZIT6", "part_number": "STM32H743ZIT6",
            "symbol_pins": {b: {"name": n} for b, (n, _) in pins.items()},
            "pins": {b: {"net": net} for b, (_, net) in pins.items()}}
    nets = {}
    for b, (_, net) in pins.items():
        nets.setdefault(net, []).append({"ref": "U1", "pin": b, "pintype": "bidirectional"})
    for net, nodes in nets.items():
        nodes.extend((extra_nodes or {}).get(net, [{"ref": "R1", "pin": "1", "pintype": "passive"}]))
    return {"components": {"U1": comp}, "nets": [{"name": n, "nodes": v} for n, v in nets.items()]}


class Rails(unittest.TestCase):
    """A rail is decided from pin types or an exact rail name, never from a name that only contains one."""

    def rows(self, ioc_text, pins, extra_nodes=None):
        with tempfile.TemporaryDirectory() as tmp:
            ioc = Path(tmp) / "b.ioc"
            ioc.write_text("Mcu.CPN=STM32H743ZIT6\n" + ioc_text)
            report = cubemx.check(rail_circuit(pins, extra_nodes), ioc)
        return {r["pin"]: r for r in report["results"]}

    def test_sense_and_enable_nets_are_signals_not_rails(self):
        rows = self.rows("PA0.Signal=ADC1_INP16\nPA0.GPIO_Label=VBAT_SENSE\n"
                         "PA1.Signal=GPIO_Output\nPA1.GPIO_Label=VDD_EN\n"
                         "PA2.Signal=GPIO_Input\nPA2.GPIO_Label=3V3_PG\n",
                         {"1": ("PA0", "/Power/VBAT_SENSE"), "2": ("PA1", "/Power/VDD_EN"), "3": ("PA2", "+3V3_PG")})
        self.assertEqual({p: r["status"] for p, r in rows.items()}, {"PA0": "pass", "PA1": "pass", "PA2": "pass"})

    def test_input_strap_on_a_rail_is_info(self):
        rows = self.rows("PB1.Signal=GPIO_Input\nPB1.GPIO_Label=HW_REV0\nPB2.Signal=ADC1_INP5\n",
                         {"1": ("PB1", "GND"), "2": ("PB2", "+3V3")})
        self.assertEqual(rows["PB1"]["status"], "info")
        self.assertEqual(rows["PB2"]["status"], "info")

    def test_driven_pin_on_a_rail_fails(self):
        rows = self.rows("PD7.Signal=GPIO_Output\nPD8.Signal=USART3_TX\n", {"1": ("PD7", "GND"), "2": ("PD8", "+3V3")})
        self.assertEqual(rows["PD7"]["status"], "fail")
        self.assertEqual(rows["PD8"]["status"], "fail")

    def test_unknown_direction_on_a_rail_needs_review(self):
        rows = self.rows("PA8.Signal=TIM1_CH1\n", {"1": ("PA8", "GND")})
        self.assertEqual(rows["PA8"]["status"], "needs_review")
        self.assertIn("direction", rows["PA8"]["reason"])

    def test_rail_is_recognised_by_pin_type_whatever_its_name(self):
        power = {"V_CORE_AUX": [{"ref": "U2", "pin": "5", "pintype": "power_out"}]}
        rows = self.rows("PC0.Signal=GPIO_Output\n", {"1": ("PC0", "V_CORE_AUX")}, power)
        self.assertEqual(rows["PC0"]["status"], "fail")
        # the same name without a power pin on the net is an ordinary signal
        rows = self.rows("PC0.Signal=GPIO_Output\n", {"1": ("PC0", "V_CORE_AUX")})
        self.assertNotEqual(rows["PC0"]["status"], "fail")

    def test_exact_rail_names(self):
        for name in ("GND", "/Sheet/GND", "AGND", "VSS", "VDD", "VDDA", "+3V3", "3V3", "+5V", "+3.3V", "VBAT", "+1V8"):
            self.assertTrue(cubemx.RAIL_NAME.search(name), name)
        for name in ("VBAT_SENSE", "VDD_EN", "+3V3_PG", "+5V_MON", "GND_DETECT", "VCC_SENSE", "LED_GND_SW", "12V_EN"):
            self.assertFalse(cubemx.RAIL_NAME.search(name), name)


class H7CPins(unittest.TestCase):
    """PC2 and PC2_C are different pins on STM32H7 packages that bond out both."""

    def rows(self, ioc_text, pins):
        with tempfile.TemporaryDirectory() as tmp:
            ioc = Path(tmp) / "b.ioc"
            ioc.write_text("Mcu.CPN=STM32H743ZIT6\n" + ioc_text)
            report = cubemx.check(rail_circuit(pins), ioc)
        return {r["pin"]: r for r in report["results"]}

    def test_both_pins_are_kept_apart(self):
        rows = self.rows("PC2.Signal=SPI2_MISO\nPC2_C.Signal=ADC3_INP0\nPC2_C.GPIO_Label=ISENSE\n",
                         {"28": ("PC2", "SPI2_MISO"), "29": ("PC2_C", "ISENSE")})
        self.assertEqual((rows["PC2"]["ball"], rows["PC2"]["status"]), ("28", "pass"))
        self.assertEqual((rows["PC2_C"]["ball"], rows["PC2_C"]["status"]), ("29", "pass"))

    def test_suffix_only_on_one_side_is_the_same_pin(self):
        rows = self.rows("PC3_C.Signal=ADC3_INP1\nPC3_C.GPIO_Label=VSENSE\n", {"30": ("PC3", "VSENSE")})
        self.assertEqual(list(rows), ["PC3_C"])
        self.assertEqual((rows["PC3_C"]["ball"], rows["PC3_C"]["status"]), ("30", "pass"))

    def test_other_names_starting_with_c_are_not_the_suffix(self):
        m = cubemx.PORT_PIN.match("PB8_CAN1_RX")
        self.assertIsNone(m.group(3))


class FindIoc(unittest.TestCase):
    def test_searches_up_to_repo_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / "CubeMX").mkdir()
            (repo / "CubeMX" / "board.ioc").write_text(IOC)
            (repo / "hw" / "board_report_R0").mkdir(parents=True)
            (repo / "hw" / "board_report_R0" / "old.ioc").write_text(IOC)
            pro = repo / "hw" / "board.kicad_pro"
            pro.write_text("{}")
            self.assertEqual([p.name for p in cubemx.find_ioc(pro)], ["board.ioc"])


if __name__ == "__main__":
    unittest.main()
