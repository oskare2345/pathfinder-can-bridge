## What does this change?

<!-- Short description. Link the issue it fixes, e.g. "Fixes #12". -->

## Trucks / adapters tested

<!-- Truck type (e.g. E20 386-02), adapter (e.g. CANable slcan), bitrate. "not tested on a truck" is OK – say so. -->

-

## Checklist

- [ ] No Linde software, data files (.cla, .xml, SB_ folders), PDFs or documentation are included.
- [ ] No private data in code, comments, tests or logs (truck/ECU serial numbers, SIM/IMEI,
      company names, Windows user names, e-mail addresses).
- [ ] New diagnostic tools are **read-only** by default; anything that writes to a truck is clearly
      marked and needs an explicit option.
- [ ] `py pathfinder_can.py --version` and the window still start.
- [ ] `CHANGELOG.md` updated (and `COMPATIBILITY.md` if a truck type was tested).
