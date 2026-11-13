# Compatibility

Status of truck types tried with the bridge. Please add yours: run **Compatibility report** and open a
GitHub issue with the *Compatibility report* template (or a pull request changing this table).

Legend: ✅ works · ⚠️ works with limits · ❌ does not work · ❓ not tried

| Truck | Type | Year | CAN bitrate | Controllers found | Bridge | Faults | Notes | Tool version |
|---|---|---|---|---|---|---|---|---|
| Linde E20 | 386-02 | – | 500000 | 1 traction, 2 lift, 4 display, 6 access, 0xA/0xB LAC | ✅ | ✅ | display software 10.100 newer than Pathfinder data – some display values refused; LAC long answers are slow (~2.5 s per part) | 2.1.0 |
| Linde H40D–H50D (IC) | 352 | – | ? | ? | ❓ | ❓ | diagnostic plug probably "X20"; may not be CAN – check pinout first | – |

## What can make a truck incompatible

- **Different CAN bitrate** – try 250000 or 125000 in the window.
- **Quiet bus** – the truck sends nothing until asked: tick *Quiet bus*.
- **Diagnostic IDs other than 0x780+n / 0x790+n**, or controllers above node 15 – needs a code change;
  the compatibility report shows what answers.
- **Engine computers** (diesel / LPG) usually use J1939 with 29-bit IDs and are not reachable through
  Pathfinder's frame format.
- **Older trucks** without CAN diagnostics (serial "Truck Doctor" era).
- **Pathfinder has no data for the truck** – check for a folder `SB_<type>…` in Pathfinder's `SB`
  directory; without it Pathfinder cannot open the truck at all.
- **Different fault-list layout** – the report shows which fault requests are answered; unknown layouts
  are stored raw in `faults.csv` (`unparsed`) so they can be worked out later.

## Adding support for a new truck (for developers)

1. Get a compatibility report and a bridge log of a Pathfinder session on that truck.
2. Put truck-specific knowledge in data, not in code paths, where possible (`NODE_NAMES`,
   `FAULT_REQUESTS`, `parse_faults`).
3. Add the truck to the table above, bump the MINOR version in `pathfinder_can.py` and add a line to
   `CHANGELOG.md`.
4. Never include Linde files or documents.
