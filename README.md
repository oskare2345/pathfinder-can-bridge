# Pathfinder CAN bridge

Use **Linde Pathfinder** with a cheap generic USB-CAN adapter (CANable / slcan and similar) instead of
the original Linde diagnostic interface. It also includes read-only diagnostic tools, **automatic fault logging**
and a **compatibility report** for trying it on other truck types.

```
Pathfinder ── COM7 ══ virtual serial pair ══ COM8 ── bridge ── USB-CAN adapter ── truck diagnostic CAN
```

> ### ⚠️ Read this first
> - **Unofficial.** This project is not affiliated with, authorised or endorsed by Linde Material Handling
>   or KION Group. "Linde" and "Pathfinder" are trademarks of their owners and are used here only to say
>   what this tool works with.
> - **No Linde software or data is included.** You need your own **licensed** copy of Pathfinder.
>   Do not open issues about obtaining Pathfinder, licences or cracks – they will be closed.
> - **Safety.** Forklift settings control speed, braking, hydraulics and access control. A wrong value
>   can make a truck dangerous to its driver and to people around it. Only change what you understand,
>   test carefully in a safe area, and write down the original value first. Changing settings may void
>   the warranty, the type approval/CE conformity or your insurance cover.
> - **No warranty.** Provided "as is" under the MIT licence (see `LICENSE`). You use it entirely at your
>   own risk; the authors are not liable for any damage, injury or loss.

## What works

| Truck | Status |
|---|---|
| Linde E20 (type 386-02) | ✅ fully tested – reading, writing settings, fault lists |
| everything else | ❓ not tested yet – see [COMPATIBILITY.md](COMPATIBILITY.md) and please send a report |

## Files

| File | What it is |
|---|---|
| `start_bridge.bat` | double-click to open the window (installs python-can / pyserial the first time) |
| `pathfinder_gui.py` | the window |
| `pathfinder_can.py` | the bridge + command-line tools (the window uses it) |
| `pathfinder_gui.json` | your last settings (created automatically, not shared) |
| `bridge_YYYY-MM-DD.log` | log of each day's session (in the log folder you choose) |
| `faults.csv` | history of every fault list read (in the log folder; opens in Excel) |
| `compat_report_*.md` | compatibility reports (in the log folder) |

## One-time setup

1. Python 3.8+ from python.org ("Add to PATH" ticked).
2. A virtual serial port pair ([com0com](https://com0com.sourceforge.net/)), e.g. **COM7 ↔ COM8**.
3. Pathfinder set to use **COM7** (one end of the pair).
4. A USB-CAN adapter (slcan / CANable type) connected to the truck's diagnostic CAN (CAN-H, CAN-L, GND).
   It shows up as e.g. **COM6**. Use a good USB cable – a bad one causes "Could not read from serial
   device" crashes. **Check the pinout of your truck's diagnostic socket before connecting anything** –
   some sockets also carry battery voltage.

## Wiring

!!! In diagnostic connector make sure you do not connect adapter to power pin !!!
1. Connect GND of adapter to GND of forklift
2. Connect Can L of adapter to Can L of forklift
3. Connect Can H of adapter to Can H of forklift.
4. Double check everything - failure to connect properly might damage truch controller!


   
## Every time

1. Switch the truck **on**.
2. Double-click `start_bridge.bat`.
3. Check the fields (they are remembered):
   - **Pathfinder port** = the bridge end of the pair (**COM8**)
   - **CAN adapter port** = **COM6**, type **slcan**, adapter speed **115200**, CAN bitrate **500000**
   - **Truck name** = something that identifies the truck, e.g. `E20 386-02 warehouse` (keeps each
     truck's fault history apart)
4. Click **Start bridge**. The lamps should show:
   - CAN adapter: open (green)
   - Truck bus: frames/s (green; an E20 sends ~900 frames/s)
5. Connect in Pathfinder. Lamp "Pathfinder" turns green ("talking (traction, lift, ...)").
6. Click **Stop bridge** before closing / unplugging.

## Automatic fault logging

With **Save fault lists automatically** ticked, every fault list that Pathfinder reads while the bridge
runs is stored in `faults.csv` in the log folder. You can also click **Read faults** (bridge stopped)
to read all controllers' fault memories directly – read-only, nothing is cleared.

- A list is written the first time it is seen in a session and again whenever it changes.
- Each row is marked `first read`, `NEW`, `still present`, `CLEARED` or `no faults`, compared with
  the last time **this truck name** was read (remembered in `faults_state.json`, also across days).
- Columns: date, time, truck, node, controller, list (the request used), change, code (hex and
  decimal), details (extra bytes, e.g. hour-meter data on the display), source, tool version.
- If `faults.csv` is open in Excel (locked), rows go to `faults_while_csv_was_open.csv` instead.
- Codes are stored as numbers. Their meaning is in your Pathfinder / service documentation.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "COMx does not exist" | wrong number – click **Refresh ports**, check Device Manager |
| "COMx is used by another program" | close other bridge windows / terminals; Pathfinder must be on the *other* end of the pair |
| "adapter is stuck" / semaphore timeout | unplug the USB-CAN adapter 5 s, plug back in; try another USB cable |
| Truck bus: silent | truck off, adapter not connected to CAN-H/CAN-L, or wrong bitrate (E20 = 500000; try 250000 / 125000 on other trucks) |
| Bus silent, but the truck is on | some (older) trucks send nothing by themselves – tick **Quiet bus**, then **Find controllers** |
| Pathfinder lamp stays yellow | Pathfinder uses the wrong COM port (must be the other end of the pair) |
| "request NOT sent: CAN bus is silent" | Pathfinder asked while the truck was off – switch it on and reconnect |
| "no answer from …" | truck restarted at that moment, or that controller is not fitted |
| "NEGATIVE … notSupported/invalidFormat" | the controller doesn't have that value (often: controller software newer than Pathfinder's data) |
| "NEGATIVE … conditionsNotCorrect" | value only readable in a certain truck state (seat, key, standstill …) |
| Truck switches itself off after about a minute | often an access-control module's auto-logout time |
| Pathfinder seems to freeze on a screen | note the time and send the log of that day – see "Reporting problems" |

Read-only check buttons (the bridge must be stopped):

- **Test CAN adapter** – only listens; counts truck frames for 3 s.
- **Find controllers** – asks controllers 1–15 for their name and version.
- **Read faults** – reads every controller's fault memory into `faults.csv`.
- **Compatibility report** – all of the above in one Markdown file for a GitHub issue.

Tick **Only problems** above the log to see just warnings, errors and faults; **Copy all** puts the log
on the clipboard.

## Command line (optional)

```
py pathfinder_can.py ports                       list serial ports
py pathfinder_can.py bridge -p COM8 -c COM6      bridge in the console (Ctrl+C stops), --log file.txt
py pathfinder_can.py test  -c COM6               adapter test
py pathfinder_can.py nodes -c COM6               list controllers           (--quiet-bus, -b 250000 ...)
py pathfinder_can.py faults -c COM6 --truck "E20 386-02"                    read fault memories -> faults.csv
py pathfinder_can.py report -c COM6 --truck "H40D 352-03 2001"              compatibility report
py pathfinder_can.py diag  -c COM6 --node 1 "1A 87" "21 B6"                 read values (read-only)
py pathfinder_can.py diag  -c COM6 --node 1 --scan 21                       read all local IDs
py pathfinder_can.py decode "02 81 07 02 02 10 81 FF FF FF FF FF E8"
py pathfinder_can.py --version
```

Defaults: slcan, COM6, adapter 115200, CAN 500000, Pathfinder port COM8. Other adapters supported by
python-can that use a serial port: `-i seeedstudio`, `-i robotell`.

## Reporting problems / new trucks – "Send to developer"

The easiest way: click **✉ Send to developer...** in the window.

1. Choose the file: today's log, the latest compatibility report, `faults.csv` or any other file.
2. Write what happened and roughly when (the time as shown in the log).
3. Click **Create anonymised copy and open GitHub**. The program
   - writes a copy named `SHARE_…` next to the original, with private data removed:
     truck and controller serial numbers, SIM / IMEI-like numbers, telematics-module data,
     Windows user names, e-mail addresses and raw serial bytes;
   - opens the folder with that copy selected, and a pre-filled GitHub issue in your browser
     (tool version, truck, adapter settings and the last warnings are already filled in).
4. Look through the copy, drag it into the issue and click **Submit**. You need a free GitHub account.

**Nothing is uploaded by the program itself** – you always see and send the file yourself.
The anonymiser removes the usual identifiers, but it cannot know everything: if the truck name or the
description contains a company name, change it before posting.

Command line: `py pathfinder_can.py share bridge_2026-11-12.log --what "Pathfinder freezes" --when 16:46 --open`

Issue types: **Compatibility report** (a truck type not in COMPATIBILITY.md yet) and **Bug report**
(something does not work). Questions go to GitHub Discussions.

## How it works (reference)

Pathfinder serial frame: `02 | cmd | 07 | 8 CAN data bytes | XOR checksum`; every `02` inside a frame
is sent doubled (`02 02`). `cmd 8n` = request to controller *n* → CAN ID `0x780+n`; the answer from
`0x790+n` goes back as `cmd 9n`. The CAN data is KWP2000 over ISO-TP. Pathfinder never sends ISO-TP
flow control, so the bridge sends `30 00 00` itself when a controller starts a long answer.

Controllers on the E20 (386-02):

| Node | CAN IDs | Controller |
|---|---|---|
| 1 | 0x781 / 0x791 | traction |
| 2 | 0x782 / 0x792 | lift |
| 4 | 0x784 / 0x794 | display |
| 6 | 0x786 / 0x796 | access control / telematics |
| 0xA, 0xB | 0x78A, 0x78B | LAC modules |

Fault lists (read-only): `13 01` → `53 <count> <2-byte code, LSB first> …` (traction, lift, LAC);
`17 03` → `57 <count> <16-byte records>` (display). Standard KWP `18` answers are understood too.

Settings are written by Pathfinder with `3B <id> <value LSB first>` (local ID) or
`2E <id hi> <id lo> <value>` (common ID). The bridge logs every write as `WRITE to …`.

## Legal

The protocol was worked out by observing the communication between Pathfinder and a truck, for
interoperability with other hardware. No Linde code, data files or documents are part of this
repository – please do not add any (pull requests containing them will be rejected).
See [LICENSE](LICENSE) (MIT) and [NOTICE.md](NOTICE.md).
