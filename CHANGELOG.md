# Changelog

Version numbers: `MAJOR.MINOR.PATCH` – PATCH = fixes, MINOR = new features / new truck support,
MAJOR = changes that break old settings or command lines. The version is in `pathfinder_can.py`
(`__version__`) and shown in the window title and in every log and report.

## 2.1.0 – 2026-11-13

- **Automatic fault logging**: every fault list Pathfinder reads is saved to `faults.csv`, with
  NEW / CLEARED marking compared with the last read of the same truck.
- **Read faults** button / `faults` command: reads all controllers' fault memories (read-only).
- **Compatibility report** button / `report` command: Markdown report for testing other truck types.
- **Truck name** field: keeps fault history of several trucks apart.
- **Quiet bus** option for trucks that send no CAN traffic by themselves.
- 29-bit CAN IDs are noted by the adapter test (engine computers).
- Truck-bus lamp no longer assumes E20 traffic levels.
- **Send to developer** button / `share` command: anonymised copy of a log, report or faults.csv
  (serial numbers, SIM/IMEI-like numbers, telematics data, user names, e-mails removed) and a
  pre-filled GitHub issue. Nothing is uploaded automatically.
- Public release: MIT licence, safety notice, compatibility list, issue templates.

## 2.0.0 – 2026-11-12

- Window (tkinter) with port selection, status lamps, coloured log, "only problems" filter.
- Automatic reconnect after USB errors; requests are not sent while the bus is silent.
- Timeout warning for unanswered requests; every write is logged.
- Read-only tools: adapter test, find controllers, `diag`, `decode`.

## 1.x – 2026-11

- First working bridge: Pathfinder serial frame format (with `02` escaping), node routing from the
  command byte, automatic ISO-TP flow control. Tested on a Linde E20 (386-02).
