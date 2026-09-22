# Manufacturer Profile Research — v1

Human-compiled research dump for the six reference vehicles (Nissan Leaf,
Tesla Model 3, Chevrolet Bolt, Audi e-tron, BMW i3, Kia Niro EV). This is
**not** a verified primary-source document — it's a starting point compiled
from a mix of manufacturer specs, teardown reports, academic/lab testing
(e.g. ORNL), aggregated fleet telemetry (e.g. Geotab), owner/forum reports,
and general Li-ion principles, with sourcing quality varying field by field.
`configs/profiles/*.yaml` cites this document (as `docs/research/
profiles_v1.md`) per field, alongside a `confidence` tag (`high`/`medium`/
`low`) reflecting how strong the underlying source actually is — this
document itself makes no claim to have checked every number against a
manufacturer datasheet. Treat every number below as "reported," not
"verified," until traced to its own citation.

This is a versioned document: future research passes that revise, correct,
or add to this information should land in a new `profiles_v2.md` (etc.)
rather than editing this file in place, so profile YAMLs that cite a
specific version stay reproducible.

---

## Nissan Leaf (2011–2017, Gen 1)

**Battery:** 24 kWh nominal, 21.3 kWh usable. 48 modules, 192 pouch cells (4
per module). LMO/NCA cathode. 360 V nominal. Thermal management: none —
passive air cooling only, no active cooling or heating in early models.
Heaters added later for cold-weather charging. Degradation: ~4.2% per year
in hot climates; some owners reported ~20% loss after 12 months in hot
climates. Nissan originally claimed 80% retention after 5 years; hot-climate
reality far worse. Thermal limit: owner manual warns against sustained high
battery temps from high ambient or repeated fast charging.

**Motor:** Gen 1 80 kW / 280 Nm, liquid-cooled. Continuous 80 kW at 7,000
rpm with 65°C coolant. ORNL testing: 50 kW for 1 hour → motor below 110°C;
80 kW for 1 hour → winding below 135°C. Motor wire insulation rated
180–220°C. Gen 2 (2018+): 110 kW / 320 Nm. Derating: power limitation
triggers on high motor, inverter, coolant, or battery temp, or very low
battery temp (~−20°C).

**Story:** heat has nowhere to go. Pack absorbs heat from fast charging and
hot ambient, only sheds via convection.

---

## Tesla Model 3 (2017+)

**Battery:** ~75 kWh usable, ~80.5 kWh total. 96s cell config, ~400 V
nominal. Pack weight 480 kg, 168 Wh/kg. NCA with reduced cobalt,
silicon-graphite anodes. Thermal: liquid-cooled, flat coolant tubes in
direct contact with cylindrical 2170 cells. Coolant flow 35 L/min (vs. 19
L/min on Model S P100D). Target ~45°C; fast charging target 40–50°C
surface, cooled below 50°C. Pre-octovalve (2017–2020): PTC heater, no heat
pump. Octovalve (late 2020+): integrated heat pump with 15 operating modes,
waste heat recovery from motor and power electronics. Degradation: ~2.3%
per year (Geotab, 22,700 vehicles). After 100,000 km: 92.8–93.3% SoH
depending on cell supplier.

**Motor:** Rear (3D7) PMSM 194 kW / 340 Nm, oil-cooled stator + rotor. Base
speed ~4,275 rpm, max 15,000 rpm. Front (3D3, AWD) induction 137 kW / 219
Nm, oil-cooled. Oil-cooled with integrated transmission heat exchanger.
Continuous torque 40–50% higher than water-cooled equivalent, efficiency
+3–5% across wide speed range. Derating: Track Mode reduces output as speed
increases; ~2 laps at full power before major reduction, ~300 hp by lap 3.

---

## Chevrolet Bolt (2017–2022)

**Battery:** 60 kWh usable. 288 prismatic pouch cells from LG Chem,
nickel-rich. Pack weight 440 kg. Nominal 350 V. Thermal: liquid-cooled tray
circulating 6.9 L DEX-COOL. Separate heating element for cold warm-up.
Battery cooling can use A/C compressor (chiller) when needed. Target
~15–35°C. Cools aggressively when plugged in (~27°C target); unplugged may
allow up to 37°C. Degradation: ~2.0% per year hot climate. After 100,000
km: ~89–90% SoH. Real problem: LG Chem manufacturing defects (torn anode +
folded separator) caused fires → recall of every Bolt. Charging limited to
80–90% via software until packs replaced. Thermal design fine; cell quality
and charge strategy weren't.

**Motor:** 150 kW / 360 Nm PM. Final drive 7.05:1. Cooling: oil-cooled with
enclosed circulation, oil passes through interior, two oil jets dedicated
to end windings per side. Separate coolant circuits for electronics/drive
unit, HV battery, cabin heater.

---

## Audi e-tron (2019+)

**Battery:** 95 kWh gross, 86 kWh usable. 36 modules, 12 cells per module.
Prismatic (later models; pouch at launch). 396 V nominal. Thermal: indirect
liquid cooling via flat extruded aluminum sections. Thermal gel beneath
each module. 22 liters coolant, 40 meters of cooling lines, four
independent circuits. Target 25–35°C, tightly controlled. Heat pump
standard; recovers up to 3 kW waste heat from HV battery. Degradation:
~1.2% over 18 months in one example. Audi official: ~90% SoH at 160,000 km,
~88% at 200,000 km after 5 years. After 100,000 km: 92.9–93.0% SoH.

**Motor:** Front 135 kW, rear 165 kW (D mode 265 kW / 561 Nm combined;
boost 300 kW / 664 Nm). Three-motor S variant: 370 kW / 973 Nm boost.
Cooling: rotor internal cooling — coolant through hollow rotor shafts,
keeps temps below 180°C. Stators and end shields water-cooled. Cooling
lances for rotor internal cooling, stator jacket cooling, bearing plate
cooling. Continuous power: after 30 min full load at high ambient, still 2
× 70 kW rear + 95 kW front. Thermal derating "hardly possible even under
highly dynamic driving."

**Story:** conservative buffer (86 usable from 95 gross), large coolant
volume, aggressive cooling, heat pump recovering waste heat. Trades range
for longevity.

---

## BMW i3 (2013+)

**Battery:** 22 kWh (early), 42.2 kWh (later). 8 modules × 12 cells.
~352–360 V nominal. Samsung cells. Pack weight ~230 kg. Thermal: direct
expansion (DX) refrigerant cooling using R134a from A/C system directly
through microchannel extrusions in battery pack. Eight channels in battery
box floor. No glycol loop in pack. No additional pumps or fans. Target
~20°C. Preconditioning via wallbox before departure in cold. Operating temp
range: charge 0–60°C; discharge −25–60°C. Heat pump optional; saves up to
30% energy in city driving vs. resistive heat, extends range up to 30%.
Degradation: after 100,000 km, 93.77% SoH (best in class for its era). One
example at ~200,000 km / 10 years: 82.4%. Another at 330,000 km with
frequent fast charging: still 82.5%.

**Motor:** 125 kW / 250 Nm hybrid synchronous. Motor mass 42 kg. Water
jacket cooling.

**Story:** DX cooling elegant (no glycol, no extra pumps, max refrigeration
efficiency) but fragile — if A/C compressor fails internally ("Black
Death"), entire battery cooling system can't be flushed, requiring full
pack + cooling system replacement.

---

## Kia Niro EV (2018+)

**Battery:** 64 kWh (long range), 39.2 kWh (standard). 356 V, 180 Ah.
Nickel-rich NMC. Pouch or prismatic from LG Chem / SK Innovation. Thermal:
liquid-cooled, integrated with A/C via chiller. Coolant loop runs through
battery, motor, inverter, heat pump. Can cool battery with no air movement
(stationary). Separate blue EV battery coolant, not standard green
antifreeze. Preconditioning to 20–25°C before fast charging when
navigating to charger. Active heating below 20°C; target max 30°C.
Cold-weather heating system insulates and warms pack while plugged in.
Heat pump scavenges waste heat from power electronics. Degradation: BEST IN
CLASS. After 100,000 km: 97.25% SoH — top-ranked in a study of ~10,000 EVs.
After 62,000 miles: 97.25%.

**Motor:** 150 kW / 395 Nm (64 kWh) PMSM. Shared coolant loop with battery
and inverter. Heat from motor/electronics used for cabin heating via heat
exchanger on heat pump models. Derating: power limited when motor temp
high, battery temp too high or low, or cooling system fault. Known issue:
2020 Niro drive motor erroneously recorded overheating, cut power to
turtle mode — required motor replacement.

**Story:** failure mode is integration, not hardware. Liquid cooling and
heat pump present, but early models lacked battery preconditioning for DC
fast charging, and shared coolant loop sometimes prioritized cabin comfort
over pack conditioning.

---

## Derating curve caveat

Real derating curves are proprietary and not published. Encode as linear
ramps from documented threshold to documented limit, tagged
`confidence: low`. For Leaf: aggressive derating at high battery temp
(documented in manual). Model 3: Track Mode reduces power as speed
increases; ~2 laps before major reduction. Bolt: cold "Propulsion Reduced";
hot cooling to 27°C when plugged in. e-tron: "hardly possible" to derate —
set high threshold. i3: charge 0–60°C, discharge −25–60°C. Niro: limited
when motor or battery temp high or low.
