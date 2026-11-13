---
name: Compatibility report
about: Tried the bridge on a truck type that is not in COMPATIBILITY.md
title: "[truck] <model> <type, e.g. 352-03> <year>"
labels: compatibility
---

**Truck** (from the data plate): model, type number (e.g. 386-02), year, electric / diesel / LPG

**Adapter**: make/model, interface (slcan / seeedstudio / robotell), CAN bitrate that worked

**Tool version** (window title or `py pathfinder_can.py --version`):

**Does Pathfinder have data for this truck?** (folder `SB_<type>…` exists: yes / no)

**Result**
- [ ] Test CAN adapter sees traffic
- [ ] Find controllers lists controllers
- [ ] Pathfinder connects through the bridge
- [ ] Values read correctly
- [ ] Fault lists saved to faults.csv

**Compatibility report** – attach `compat_report_*.md` or paste it here:

**Notes** (diagnostic socket location / pinout, anything unusual):

<!-- Remove serial numbers or other data you don't want public. No Linde files or documents, please. -->
