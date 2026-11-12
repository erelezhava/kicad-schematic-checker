# 14. Check the MCU's pins against STM32CubeMX

Compares the pin configuration in your CubeMX `.ioc` with the nets on the MCU's pins in the
schematic. Catches swapped UART/SPI/I²C lines, pins configured in firmware but not wired, wired
pins firmware doesn't know about, and outputs tied to a rail. Offline, no keys.

[← All guides](../USER_GUIDE.md) · Command: `mcu-pins` (and the `mcu_pins` step of `run`)

## Run it

`run` does it automatically when the project or its repository holds exactly one `.ioc`
(searched from the `.kicad_pro` folder up to the git root): results in `05_mcu_pins/mcu-pins.md`.
Several `.ioc` files: `run --ioc path/to/board.ioc`. On its own:

```sh
python3 -m kicad_checker mcu-pins <report>/01_schematic/circuit.json --ioc CubeMX/board.ioc --output runs/pins-001
```

The MCU is the part whose MPN/Value matches the `.ioc` (`Mcu.CPN`, or `Mcu.Name` such as
`STM32H743V(G-I)Hx`); `--ref U1` picks it by hand.

## What it reports

| Status | When |
| --- | --- |
| fail | a pin CubeMX **drives** (`GPIO_Output`, `..._TX`, MCO) is tied to a supply rail or ground |
| needs_review | a pin with a signal of unknown direction (timer channel, SPI line …) is tied to a rail |
| needs_review | configured in CubeMX, unconnected in the schematic |
| needs_review | net name does not resemble the CubeMX signal or GPIO label, or names the opposite role (net `..._TX` on a `UARTx_RX` pin) |
| needs_review | connected to a signal net in the schematic, not configured in CubeMX |
| needs_review | CubeMX uses a pin the symbol does not have (package/symbol mismatch) |
| info | unused pin, or an **input** (`GPIO_Input`, ADC, EXTI, `..._RX`), tied to a rail: straps and supply sensing are normal |
| pass | configured and connected to a net whose name matches the signal or label |

A net is a rail when another pin on it is a power pin in the schematic (pin type power input or
power output), or when its name is exactly a rail name (`GND`, `VDD`, `+3V3` …). A name that only
contains one (`VBAT_SENSE`, `VDD_EN`, `+3V3_PG`) is an ordinary signal and is compared by name.

STM32H7 `PC2_C` / `PC3_C` are kept apart from `PC2` / `PC3`. If the symbol and CubeMX disagree
only on the suffix (one side has `PC2_C`, the other only `PC2`), they are treated as the same pin.

Name matching ignores case, separators and the sheet path, and treats common variants as equal
(QUADSPI = QSPI, BANK = BK, D+ = DP, D- = DM, NCS/NSS = CS, SCK = CLK, USART = UART). A name
mismatch is a lead, not a verdict: rename the net or the CubeMX label so both tell the same story.

## Limits

- Port pins only (PA0…PK15). VDD/VSS, VCAP, VDDA/VREF, BOOT0, NRST and PDR_ON need datasheet
  rules ([4. Rules review](04-rules-review.md)).
- It does not know alternate-function tables: a pin that can't do the signal is CubeMX's job, and
  CubeMX already refuses that.
- Clock mode is not inferred from the parts: an active oscillator on OSC_IN wants CubeMX
  "BYPASS Clock Source", and then OSC_OUT stays free.
