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
