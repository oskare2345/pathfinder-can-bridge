#!/usr/bin/env python3
"""Small window for the Pathfinder CAN bridge (pathfinder_can.py). Unofficial, MIT licence, no warranty.

Start:  python pathfinder_gui.py      (or double-click start_bridge.bat)
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pathfinder_can as pc

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(HERE, "pathfinder_gui.json")
TAG_COLORS = {
    "ERROR": "#c62828", "WARN": "#b26a00", "INFO": "#1565c0", "PF>CAN": "#222222",
    "CAN>PF": "#2e7d32", "FC": "#777777", "BUS": "#777777", "RAW": "#6a1b9a", "STATUS": "#555555",
    "TOOL": "#00695c", "FAULT": "#ad1457",
}


def load_settings():
    s = dict(pc.DEFAULTS, log_dir=HERE, write_log=True, raw_serial=False, show_bus=False, truck="",
             fault_log=True, quiet_bus=False)
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


class App:
    def __init__(self, root):
        self.root = root
        self.s = load_settings()
        self.q = queue.Queue()
        self.bridge = None
        self.busy = False
        root.title(f"Pathfinder CAN bridge  v{pc.__version__}  (unofficial)")
        root.minsize(820, 560)
        self._build()
        self.refresh_ports()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)

    # ------------------------------------------------------------ layout
    def _build(self):
        pad = dict(padx=4, pady=3)
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="x")

        conn = ttk.LabelFrame(top, text="Connection", padding=6)
        conn.pack(side="left", fill="x", expand=True)
        self.v = {k: tk.StringVar(value=str(self.s[k])) for k in
                  ("pf_port", "can_port", "interface", "adapter_baud", "bitrate", "log_dir", "truck")}
        self.b = {k: tk.BooleanVar(value=bool(self.s[k])) for k in
                  ("write_log", "raw_serial", "show_bus", "fault_log", "quiet_bus")}

        ttk.Label(conn, text="Pathfinder port").grid(row=0, column=0, sticky="w", **pad)
        self.cb_pf = ttk.Combobox(conn, textvariable=self.v["pf_port"], width=34)
        self.cb_pf.grid(row=0, column=1, sticky="we", **pad)
        ttk.Label(conn, text="bridge end of the virtual pair (Pathfinder uses the other end)",
                  foreground="#777").grid(row=0, column=2, columnspan=3, sticky="w", **pad)

        ttk.Label(conn, text="CAN adapter port").grid(row=1, column=0, sticky="w", **pad)
        self.cb_can = ttk.Combobox(conn, textvariable=self.v["can_port"], width=34)
        self.cb_can.grid(row=1, column=1, sticky="we", **pad)
        ttk.Label(conn, text="type").grid(row=1, column=2, sticky="e", **pad)
        ttk.Combobox(conn, textvariable=self.v["interface"], width=11, state="readonly",
                     values=["slcan", "seeedstudio", "robotell"]).grid(row=1, column=3, sticky="w", **pad)

        ttk.Label(conn, text="Adapter speed").grid(row=2, column=0, sticky="w", **pad)
        ttk.Combobox(conn, textvariable=self.v["adapter_baud"], width=12,
                     values=["115200", "230400", "460800", "921600", "1000000", "2000000"]).grid(
            row=2, column=1, sticky="w", **pad)
        ttk.Label(conn, text="CAN bitrate").grid(row=2, column=2, sticky="e", **pad)
        ttk.Combobox(conn, textvariable=self.v["bitrate"], width=11,
                     values=["500000", "250000", "125000", "1000000"]).grid(row=2, column=3, sticky="w", **pad)
        ttk.Button(conn, text="Refresh ports", command=self.refresh_ports).grid(row=2, column=4, **pad)

        ttk.Checkbutton(conn, text="Write log file to", variable=self.b["write_log"]).grid(
            row=3, column=0, sticky="w", **pad)
        ttk.Entry(conn, textvariable=self.v["log_dir"]).grid(row=3, column=1, columnspan=3, sticky="we", **pad)
        ttk.Button(conn, text="Browse...", command=self.pick_dir).grid(row=3, column=4, **pad)

        ttk.Label(conn, text="Truck name").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(conn, textvariable=self.v["truck"]).grid(row=4, column=1, sticky="we", **pad)
        ttk.Label(conn, text='e.g. "E20 386-02 yard" - keeps fault history of each truck apart',
                  foreground="#777").grid(row=4, column=2, columnspan=3, sticky="w", **pad)
        ttk.Checkbutton(conn, text="Save fault lists automatically (faults.csv in the log folder)",
                        variable=self.b["fault_log"]).grid(row=5, column=0, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(conn, text="Quiet bus (truck sends nothing by itself)",
                        variable=self.b["quiet_bus"]).grid(row=5, column=2, columnspan=3, sticky="w", **pad)
        conn.columnconfigure(1, weight=1)

        act = ttk.LabelFrame(top, text="Actions", padding=6)
        act.pack(side="left", fill="y", padx=(6, 0))
        self.btn_start = ttk.Button(act, text="▶  Start bridge", command=self.toggle_bridge, width=20)
        self.btn_start.pack(fill="x", pady=2)
        self.btn_test = ttk.Button(act, text="Test CAN adapter", command=self.run_test)
        self.btn_test.pack(fill="x", pady=2)
        self.btn_nodes = ttk.Button(act, text="Find controllers", command=self.run_nodes)
        self.btn_nodes.pack(fill="x", pady=2)
        self.btn_faults = ttk.Button(act, text="Read faults", command=self.run_faults)
        self.btn_faults.pack(fill="x", pady=2)
        self.btn_report = ttk.Button(act, text="Compatibility report", command=self.run_report)
        self.btn_report.pack(fill="x", pady=2)
        self.tool_buttons = (self.btn_test, self.btn_nodes, self.btn_faults, self.btn_report)
        ttk.Button(act, text="Open log folder", command=self.open_log_dir).pack(fill="x", pady=2)
        ttk.Button(act, text="✉  Send to developer...", command=self.send_dialog).pack(fill="x", pady=2)
        ttk.Checkbutton(act, text="Show raw Pathfinder bytes", variable=self.b["raw_serial"]).pack(anchor="w")
        ttk.Checkbutton(act, text="Show other CAN traffic", variable=self.b["show_bus"]).pack(anchor="w")

        # status lamps
        st = ttk.Frame(self.root, padding=(6, 0))
        st.pack(fill="x")
        self.lamps = {}
        for key, label in (("adapter", "CAN adapter"), ("truck", "Truck bus"), ("pf", "Pathfinder")):
            f = ttk.Frame(st)
            f.pack(side="left", padx=(0, 18))
            c = tk.Canvas(f, width=14, height=14, highlightthickness=0)
            c.pack(side="left")
            dot = c.create_oval(2, 2, 12, 12, fill="#bbb", outline="")
            lbl = ttk.Label(f, text=f"{label}: -")
            lbl.pack(side="left", padx=4)
            self.lamps[key] = (c, dot, lbl, label)
        self.counts = ttk.Label(st, text="")
        self.counts.pack(side="left")

        # log
        lf = ttk.Frame(self.root, padding=6)
        lf.pack(fill="both", expand=True)
        bar = ttk.Frame(lf)
        bar.pack(fill="x")
        self.only_problems = tk.BooleanVar(value=False)
        self.autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Only problems", variable=self.only_problems).pack(side="left")
        ttk.Checkbutton(bar, text="Auto-scroll", variable=self.autoscroll).pack(side="left", padx=8)
        ttk.Button(bar, text="Clear", command=lambda: self.text.delete("1.0", "end")).pack(side="right")
        ttk.Button(bar, text="Copy all", command=self.copy_log).pack(side="right", padx=4)
        self.text = tk.Text(lf, wrap="none", font=("Consolas", 9), height=20)
        ys = ttk.Scrollbar(lf, orient="vertical", command=self.text.yview)
        xs = ttk.Scrollbar(lf, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        xs.pack(side="bottom", fill="x")
        ys.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        for tag, col in TAG_COLORS.items():
            self.text.tag_configure(tag, foreground=col)
        self.text.tag_configure("ERROR", foreground=TAG_COLORS["ERROR"], font=("Consolas", 9, "bold"))

        self.line("INFO", "1) truck ON   2) Start bridge   3) in Pathfinder connect via the other end of the "
                          "virtual pair.   Troubleshooting: 'Test CAN adapter' / 'Find controllers' (read-only).")
        self.line("WARN", "Unofficial tool, no warranty. Changing truck settings can make it unsafe - "
                          "you are responsible for every change. See README.")

    # ------------------------------------------------------------ helpers
    def cfg(self):
        try:
            c = dict(pf_port=self.v["pf_port"].get().split()[0], can_port=self.v["can_port"].get().split()[0],
                     interface=self.v["interface"].get(), adapter_baud=int(self.v["adapter_baud"].get()),
                     bitrate=int(self.v["bitrate"].get()))
        except (ValueError, IndexError):
            raise pc.FriendlyError("check the ports, adapter speed and bitrate fields")
        return c

    def save(self):
        s = {k: v.get() for k, v in self.v.items()}
        s.update({k: v.get() for k, v in self.b.items()})
        for k in ("pf_port", "can_port"):
            s[k] = (s[k].split() or [""])[0]
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2)
        except OSError:
            pass

    def refresh_ports(self):
        ports = pc.list_ports()
        vals = [f"{d}   {desc}" for d, desc in ports] or ["(no serial ports found - is pyserial installed?)"]
        self.cb_pf["values"] = vals
        self.cb_can["values"] = vals
        if ports:
            self.line("INFO", "serial ports: " + "; ".join(f"{d} = {desc}" for d, desc in ports))

    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.v["log_dir"].get() or HERE)
        if d:
            self.v["log_dir"].set(d)

    def open_log_dir(self):
        d = self.v["log_dir"].get() or HERE
        if sys.platform.startswith("win"):
            os.startfile(d)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", d])

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.text.get("1.0", "end"))

    def line(self, tag, text):
        self.q.put((tag, (pc.stamp(), text)))      # timestamp now, not when the window gets to it

    def lamp(self, key, color, text):
        c, dot, lbl, label = self.lamps[key]
        c.itemconfigure(dot, fill=color)
        lbl.configure(text=f"{label}: {text}")

    # ------------------------------------------------------------ bridge
    def toggle_bridge(self):
        if self.bridge and self.bridge.running:
            self.btn_start.configure(state="disabled", text="stopping ...")
            threading.Thread(target=self._stop_bridge, daemon=True).start()
            return
        try:
            c = self.cfg()
        except pc.FriendlyError as e:
            messagebox.showerror("Settings", str(e))
            return
        if c["pf_port"] == c["can_port"]:
            messagebox.showerror("Settings", "Pathfinder port and CAN adapter port must be different.")
            return
        self.save()
        log = None
        if self.b["write_log"].get():
            log = os.path.join(self.v["log_dir"].get() or HERE, time.strftime("bridge_%Y-%m-%d.log"))
            self.line("INFO", f"logging to {log}")
        self.bridge = pc.Bridge(c["pf_port"], c["can_port"], c["interface"], c["adapter_baud"], c["bitrate"],
                                log_path=log, raw_serial=self.b["raw_serial"].get(),
                                show_bus=self.b["show_bus"].get(), on_line=self.line, on_stats=self.on_stats,
                                fault_log=self.fault_log() if self.b["fault_log"].get() else None,
                                quiet_bus=self.b["quiet_bus"].get())
        self.bridge.start()
        self.btn_start.configure(text="■  Stop bridge")
        for b in self.tool_buttons:
            b.configure(state="disabled")

    def fault_log(self):
        return pc.FaultLog(self.v["log_dir"].get() or HERE, self.v["truck"].get())

    def _stop_bridge(self):
        self.bridge.stop()
        self.q.put(("_stopped", ""))

    def on_stats(self, s):
        self.q.put(("_stats", s))

    def show_stats(self, s):
        self.lamp("adapter", "#2e7d32" if s["adapter"] else "#c62828", "open" if s["adapter"] else "not open")
        if not s["adapter"]:
            self.lamp("truck", "#bbb", "-")
        elif s["fps"] > 20:
            self.lamp("truck", "#2e7d32", f"{s['fps']:.0f} frames/s")
        elif s["fps"] > 0:
            self.lamp("truck", "#f9a825", f"{s['fps']:.0f} frames/s (very low)")
        else:
            self.lamp("truck", "#c62828", "silent - truck off?")
        if not s["pf_port"]:
            self.lamp("pf", "#c62828", "port not open")
        elif s["last_request"] and time.time() - s["last_request"] < 15:
            nodes = ", ".join(pc.node_name(n).split(" (")[0] for n in s["nodes"])
            self.lamp("pf", "#2e7d32", f"talking ({nodes})")
        else:
            self.lamp("pf", "#f9a825", "waiting for Pathfinder")
        self.counts.configure(text=f"requests {s['requests']}  answers {s['answers']}  "
                                   f"refused {s['negatives']}  no answer {s['no_answer']}  "
                                   f"writes {s['writes']}  truck restarts {s['truck_restarts']}  "
                                   f"fault lists saved {s.get('fault_lists', 0)}")

    # ------------------------------------------------------------ tools
    def _tool(self, fn, title, **extra):
        if self.busy:
            return
        try:
            c = self.cfg()
        except pc.FriendlyError as e:
            messagebox.showerror("Settings", str(e))
            return
        self.save()
        self.busy = True
        for b in self.tool_buttons + (self.btn_start,):
            b.configure(state="disabled")
        self.line("TOOL", f"--- {title} ---")

        def work():
            try:
                fn(c["interface"], c["can_port"], c["adapter_baud"], c["bitrate"],
                   out=lambda t: self.line("TOOL", t), quiet_bus=self.b["quiet_bus"].get(), **extra)
            except pc.FriendlyError as e:
                self.line("ERROR", str(e))
            except Exception as e:  # noqa: BLE001
                self.line("ERROR", f"{type(e).__name__}: {e}")
            self.q.put(("_tool_done", ""))
        threading.Thread(target=work, daemon=True).start()

    def run_test(self):
        self._tool(pc.test_adapter, "Test CAN adapter (read-only)")

    def run_nodes(self):
        self._tool(pc.find_controllers, "Find controllers (read-only)")

    def run_faults(self):
        self._tool(pc.read_faults, "Read faults (read-only, nothing is cleared)", fault_log=self.fault_log())

    def run_report(self):
        if not self.v["truck"].get().strip():
            messagebox.showinfo("Compatibility report", "Please type the truck type in 'Truck name' first "
                                "(e.g. 'H40D 352-03 2001' - it is on the data plate).")
            return
        self._tool(pc.compat_report, "Compatibility report (read-only)",
                   folder=self.v["log_dir"].get() or HERE, truck=self.v["truck"].get())

    # ------------------------------------------------------------ send to developer
    def _share_candidates(self):
        d = self.v["log_dir"].get() or HERE
        today = os.path.join(d, time.strftime("bridge_%Y-%m-%d.log"))
        try:
            files = [os.path.join(d, f) for f in os.listdir(d)]
        except OSError:
            files = []
        logs = sorted((f for f in files if os.path.basename(f).startswith("bridge_") and f.endswith(".log")),
                      key=os.path.getmtime, reverse=True)
        reports = sorted((f for f in files if os.path.basename(f).startswith("compat_report_")),
                         key=os.path.getmtime, reverse=True)
        out = []
        if os.path.exists(today):
            out.append(("Today's bridge log", today))
        elif logs:
            out.append((f"Latest bridge log ({os.path.basename(logs[0])})", logs[0]))
        if reports:
            out.append((f"Latest compatibility report ({os.path.basename(reports[0])})", reports[0]))
        if os.path.exists(os.path.join(d, "faults.csv")):
            out.append(("Fault history (faults.csv)", os.path.join(d, "faults.csv")))
        return out

    def send_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("Send to developer")
        win.transient(self.root)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=10)
        f.pack(fill="both")
        ttk.Label(f, text="Makes an anonymised copy of a file and opens a pre-filled GitHub issue in your\n"
                          "browser. Nothing is sent automatically - you check the copy and attach it yourself.\n"
                          "You need a free GitHub account.", foreground="#555").pack(anchor="w")
        ttk.Label(f, text="File", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(8, 0))
        cands = self._share_candidates()
        choice = tk.StringVar(value=cands[0][1] if cands else "")
        for label, path in cands:
            ttk.Radiobutton(f, text=label, value=path, variable=choice).pack(anchor="w")

        def other():
            p = filedialog.askopenfilename(initialdir=self.v["log_dir"].get() or HERE,
                                           filetypes=[("Logs / reports", "*.log *.md *.csv *.txt"), ("All", "*.*")])
            if p:
                choice.set(p)
                other_lbl.configure(text=os.path.basename(p))
        row = ttk.Frame(f)
        row.pack(anchor="w", fill="x")
        ttk.Button(row, text="Other file...", command=other).pack(side="left")
        other_lbl = ttk.Label(row, text="" if cands else "(no logs found in the log folder)", foreground="#777")
        other_lbl.pack(side="left", padx=6)

        ttk.Label(f, text="What happened?", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(8, 0))
        what = tk.Text(f, width=70, height=4, wrap="word", font=("Segoe UI", 9))
        what.pack(fill="x")
        r2 = ttk.Frame(f)
        r2.pack(fill="x", pady=(6, 0))
        ttk.Label(r2, text="About what time (as in the log)?").pack(side="left")
        when = tk.StringVar()
        ttk.Entry(r2, textvariable=when, width=12).pack(side="left", padx=6)
        ttk.Label(r2, text="Truck").pack(side="left", padx=(12, 0))
        truck = tk.StringVar(value=self.v["truck"].get())
        ttk.Entry(r2, textvariable=truck, width=24).pack(side="left", padx=6)

        def go():
            path = choice.get()
            if not path or not os.path.exists(path):
                messagebox.showerror("Send to developer", "Choose a file first.", parent=win)
                return
            try:
                new, found = pc.make_share_copy(path)
                with open(new, encoding="utf-8") as fh:
                    summary = pc.problem_summary(fh.read()) if new.endswith((".log", ".txt")) else []
            except OSError as e:
                messagebox.showerror("Send to developer", f"Could not create the copy: {e}", parent=win)
                return
            kind = "compatibility" if os.path.basename(path).startswith("compat_report") else "bug"
            text = what.get("1.0", "end").strip()
            title = (f"[truck] {truck.get().strip()}" if kind == "compatibility"
                     else f"[bug] {text.splitlines()[0][:70] if text else 'problem'}")
            try:
                c = self.cfg()
                adapter = f"{c['interface']}, adapter speed {c['adapter_baud']}, CAN {c['bitrate']}"
            except pc.FriendlyError:
                adapter = ""
            url = pc.issue_url(kind, title, pc.issue_body(kind, text, when.get().strip(), truck.get().strip(),
                                                          adapter, summary, os.path.basename(new)))
            removed = "\n".join(f"  - {n} x {w}" for w, n in found.items()) or "  - nothing found"
            self.line("TOOL", f"anonymised copy for the developer: {new}")
            if not messagebox.askokcancel(
                    "Send to developer",
                    f"Anonymised copy created:\n{new}\n\nRemoved:\n{removed}\n\n"
                    "Next: the folder opens with the file selected, and the GitHub issue opens in your browser.\n"
                    "Please look through the file, then drag it into the issue and click 'Submit'.",
                    parent=win):
                return
            import webbrowser
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(new)])
            webbrowser.open(url)
            win.destroy()

        b = ttk.Frame(f)
        b.pack(fill="x", pady=(10, 0))
        ttk.Button(b, text="Create anonymised copy and open GitHub", command=go).pack(side="right")
        ttk.Button(b, text="Cancel", command=win.destroy).pack(side="right", padx=6)
        what.focus_set()

    # ------------------------------------------------------------ event pump
    def poll(self):
        try:
            for _ in range(500):
                tag, text = self.q.get_nowait()
                if tag == "_stats":
                    self.show_stats(text)
                elif tag == "_stopped":
                    self.btn_start.configure(state="normal", text="▶  Start bridge")
                    for b in self.tool_buttons:
                        b.configure(state="normal")
                    for k in self.lamps:
                        self.lamp(k, "#bbb", "-")
                elif tag == "_tool_done":
                    self.busy = False
                    for b in self.tool_buttons + (self.btn_start,):
                        b.configure(state="normal")
                else:
                    ts, msg = text
                    color = "WARN" if tag == "CAN>PF" and "NEGATIVE" in msg else tag
                    if self.only_problems.get() and color not in ("ERROR", "WARN", "TOOL", "FAULT"):
                        continue
                    self.text.insert("end", f"{ts}  {tag:<7} {msg}\n", color)
        except queue.Empty:
            pass
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > 20000:
            self.text.delete("1.0", f"{lines - 15000}.0")
        if self.autoscroll.get():
            self.text.see("end")
        self.root.after(100, self.poll)

    def on_close(self):
        self.save()
        if self.bridge and self.bridge.running:
            self.bridge.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform.startswith("win") else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
