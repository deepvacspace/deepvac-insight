---
title: Shroud, platen and the test article's thermocouples
type: concept
tags: [thermocouple, dut, test article, shroud, platen, stabilisation, soak, sensor fault, open circuit, frozen, spikes]
sources: [insight app/api/campaign_supervisor.py, insight app/services/campaign_service.py, insight app/services/hold_advisor.py, simulator internal/plc/vacuum.go, simulator README.md]
updated: 2026-11-13
---
# Shroud, platen and the test article's thermocouples

What each temperature in a campaign is, when a plateau counts as stabilised, and how a faulty
thermocouple is found and handled. The decision points they raise are in [[decision-points]].

## The temperatures

| Channel | What it is |
|---|---|
| `temp` (Shroud) | the surface the chamber's PID controls ([[pid-control]]); the campaign's setpoints are for it |
| `temp_platen` (Platen) | the plate the test article is bolted to; it follows the shroud with a lag of minutes in vacuum |
| `temp1` … `temp4` | thermocouples on the test article, named by the test procedure; they follow the platen (conduction through the mounts) and the shroud (radiation) more slowly |

In vacuum heat moves by radiation and conduction only, so the test article lags the shroud by
tens of minutes; vented, convection makes everything faster. Deepvac's recorded runs have no
platen or test-article thermocouples: in the demo they come from the chamber simulator and are
**synthetic**. The thermocouples' names, which ones are stabilisation sensors and the simulator's
model of each are in the standards pack's articles; the demo's articles are placeholders, not real
parts.

## Stabilisation (when the soak starts)

The test procedure names its **stabilisation sensors**. A dwell starts when every valid
stabilisation sensor is inside the band around the plateau (±3 °C in the demo pack) and changes
less than the procedure's rate (3 °C/h over 15 min in the demo pack): the soak is on the test
article, not on the shroud reaching its setpoint. If none of them is valid, the article's other
valid thermocouples are used; with no thermocouples at all, the chamber temperature inside the
band. Not stabilised within the stabilisation timeout, a decision point ([[decision-points]]).

The plateau results report, for each dwell, the shroud's range, the stabilisation sensors' range
and how long the article took to stabilise.

## Sensor validity

Checked about every 15 s of chamber time (the pack's `sensor_rule`):

| Fault | Seen as | Usual cause (general) |
|---|---|---|
| Open | a reading outside −200 … 400 °C (an open type K reads its over-range, about 1372 °C), or none | broken wire, connector off |
| Frozen | exactly the same reading for 5 min | stuck input card, a channel not updating |
| Spiking | 4 jumps over 10 °C within 10 min | loose connector, noise pickup |

An invalid thermocouple is marked invalid, logged as a deviation and left out of the charts and
results. If it is **not** a stabilisation sensor the campaign goes on. If it **is**, during cycling
a decision point asks a person to exclude it from the criterion, check it again or abort: only a
person takes a stabilisation sensor out ([[limits-and-safety]]). A sensor that reads well again
is marked valid again, unless a person excluded it.

Related: [[procedure-test-campaign]], [[vacuum-and-outgassing]].
