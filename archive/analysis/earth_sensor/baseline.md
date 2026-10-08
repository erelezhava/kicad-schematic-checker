# Earth sensor: initial checker baseline

Inspected the saved Earth_Sensor_18x40 schematic using KiCad CLI 10.0.6. Source project was not edited. This is an extraction and ERC baseline, not a completed electrical design review. No manufacturer documents have been checked yet.

## Circuit inventory

One sheet, 32 components including four mounting holes and two DNP resistors, 22 nets including three explicitly unconnected pin nets.

- A1: MLX90640 sensor; Value ends in -SP, while PRT.NUM ends in -TU. Resolve intended ordering code before binding component-specific rules. This difference alone does not establish electrical incompatibility.
- U1: LTC4331HUFD-PBF.
- U2: LP591233MDRVREP regulator.
- U3: SBSPP1000472MXT in the incoming supply path.
- J2: 5040500691 connector.
- R10 and R13: DNP. R12 and R14 are populated zero-ohm ground straps. A populated-circuit checker must not treat R10/R13 as conducting paths.

Supply connectivity: J2 pin 6 → +5V → U3 pins 1/2 → +5V FLTR → U2 pin 6; U2 pin 1 connects to +3V3. J2 pin 1 connects directly to U2 EN on /LDO_Enable. Nominal voltages are inferred from net names, not independently established operating limits.

## KiCad ERC baseline

52 findings: 4 errors and 48 warnings, with excluded findings requested. Four check categories are disabled in project configuration: single_global_label, four_way_junction, simulation_model_issue, footprint_filter. This is not an all-checks-enabled result.

| Category | Count |
| --- | ---: |
| Undriven power-input errors | 3 |
| Power-output to power-output error | 1 |
| Pin compatibility warnings | 23 |
| Symbol library warnings | 14 |
| Footprint library warnings | 5 |
| Unconnected wire endpoints | 5 |
| Dangling no-connect marker | 1 |

The power-output conflict specifically involves U2 pin 5 (GND) and pin 7 (EPAD), both declared Power output by the symbol. Investigate symbol pin typing before interpreting this as a physical output conflict. Many warnings involve Unspecified pin types. Library warnings describe the current CLI environment; they do not independently prove a faulty board. Embedded schematic data was sufficient to export connectivity.

## First proposed checks

1. Component identity agrees across Value, manufacturer part number, and source documents.
2. Symbol pin numbers and electrical types match the selected part/package.
3. DNP configuration is honored; mutually exclusive straps cannot short supplies when populated.
4. Regulator input range and intended output match system requirements.
5. Regulator capacitors satisfy documented effective-capacitance requirements.
6. Enable signal levels and startup state are defined by the external controller.
7. Sensor and interface supply limits are respected.
8. I2C pull-ups (R4/R5: 2.7 kOhm) suit bus speed, capacitance, and sink capability.
9. LTC4331 mode, address, and speed straps match the intended remote/local system configuration.
10. Differential termination/bias (R27: 110 Ohm; R1/R3: 620 Ohm) matches the link requirements and remote end.
11. NC, exposed pad, and unused signal handling follows the exact datasheet requirements.
12. Connector pin assignment matches the mating board/cable.

These are candidate rules, not pass/fail judgments. Needed next: preferred datasheets/reference guides, expected cable/peer configuration and I2C rate, and the user's design rules. PCB placement, thermal performance, and layout remain outside this baseline.

## Artifacts

- netlist.xml: KiCad connectivity export.
- circuit.json: model-independent component/net inventory with source hash and DNP flags; raw nets are not collapsed across components.
- erc.json: complete KiCad report, including object identifiers.
