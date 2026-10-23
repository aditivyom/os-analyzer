

import os
import sys
import time
import queue
import platform
import threading
import datetime
import collections
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import psutil
except ImportError:
    print("psutil is missing. Install it with:  pip install psutil")
    sys.exit(1)

# ---------------------------------------------------------------- theme ----
BG, BG2, CARD = "#0f1220", "#151933", "#1b2040"
FG, MUTED, ACCENT = "#e8eaf6", "#8b93b8", "#6c8cff"
GOOD, WARN, BAD, TRACK = "#3ddc97", "#ffb84d", "#ff5c7a", "#2a3057"
PURPLE = "#c77dff"
FONT = "Segoe UI" if platform.system() == "Windows" else "Helvetica"
REFRESH_SECONDS = 2
HISTORY = 60

IS_WIN = platform.system() == "Windows"
NCPU = psutil.cpu_count() or 1


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def pct_color(p):
    return GOOD if p < 60 else WARN if p < 85 else BAD


def score_color(s):
    return GOOD if s < 35 else WARN if s < 65 else BAD


def fmt_uptime(sec):
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} day{'s' if d != 1 else ''} {h} h"
    return f"{h} h {m} min"


def verdict(score):
    if score <= 20:
        return "Running smoothly"
    if score <= 40:
        return "Pretty good"
    if score <= 60:
        return "Getting slow"
    if score <= 80:
        return "Slow"
    return "Very slow"


# ------------------------------------------------------- data collection ----
def get_temp():
    try:
        temps = psutil.sensors_temperatures()
    except Exception:
        return None
    if not temps:
        return None
    for key in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
        if temps.get(key):
            return max(t.current for t in temps[key])
    for readings in temps.values():
        if readings:
            return readings[0].current
    return None


def get_startup_programs():
    """Returns list of (name, command/location, verdict)."""
    items = []
    system = platform.system()

    if system == "Windows":
        try:
            import winreg
            run = r"Software\Microsoft\Windows\CurrentVersion\Run"
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, run) as key:
                        i = 0
                        while True:
                            try:
                                name, val, _ = winreg.EnumValue(key, i)
                                items.append((name, str(val)))
                                i += 1
                            except OSError:
                                break
                except OSError:
                    pass
        except Exception:
            pass
        folder = os.path.join(os.environ.get("APPDATA", ""),
                              r"Microsoft\Windows\Start Menu\Programs\Startup")
        if os.path.isdir(folder):
            for f in os.listdir(folder):
                if f.lower() != "desktop.ini":
                    items.append((f, folder))
    elif system == "Darwin":
        for folder in (os.path.expanduser("~/Library/LaunchAgents"),
                       "/Library/LaunchAgents"):
            if os.path.isdir(folder):
                for f in os.listdir(folder):
                    items.append((f, folder))
    else:
        for folder in (os.path.expanduser("~/.config/autostart"),
                       "/etc/xdg/autostart"):
            if os.path.isdir(folder):
                for f in os.listdir(folder):
                    if f.endswith(".desktop"):
                        items.append((f.replace(".desktop", ""), folder))

    heavy = ("update", "helper", "onedrive", "teams", "spotify", "discord",
             "steam", "skype", "adobe", "zoom", "dropbox", "epic", "launcher",
             "cortana", "torrent", "creative cloud", "acrobat", "java")
    result = []
    for name, cmd in items:
        flag = "Review" if any(k in name.lower() for k in heavy) else "OK"
        result.append((name, cmd, flag))
    result.sort(key=lambda r: (r[2] != "Review", r[0].lower()))
    return result


def collect(prev):
    d = {}
    d["time"] = datetime.datetime.now()
    d["cpu"] = psutil.cpu_percent(interval=None)

    vm = psutil.virtual_memory()
    d["ram"] = vm.percent
    d["ram_used"], d["ram_total"] = vm.used, vm.total

    root = (os.environ.get("SystemDrive", "C:") + "\\") if IS_WIN else "/"
    du = psutil.disk_usage(root)
    d["disk_pct"] = du.percent
    d["disk_free"] = du.free
    d["disk_total"] = du.total

    now = time.time()
    rate = 0.0
    try:
        io = psutil.disk_io_counters()
        if io and prev.get("io"):
            dt = max(now - prev["t"], 0.001)
            moved = (io.read_bytes + io.write_bytes) - \
                    (prev["io"].read_bytes + prev["io"].write_bytes)
            rate = moved / dt / 1e6
        prev["io"], prev["t"] = io, now
    except Exception:
        pass
    d["disk_rate"] = rate

    procs, count = [], 0
    for p in psutil.process_iter(["name", "cpu_percent", "memory_percent"]):
        count += 1
        try:
            i = p.info
            if i["name"] in ("System Idle Process", "Idle"):
                continue
            procs.append((i["name"] or "?", p.pid,
                          (i["cpu_percent"] or 0) / NCPU,
                          i["memory_percent"] or 0))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    procs.sort(key=lambda r: r[2] + r[3], reverse=True)
    d["procs"], d["top"] = count, procs[:12]

    d["uptime"] = time.time() - psutil.boot_time()
    d["temp"] = get_temp()
    try:
        d["battery"] = psutil.sensors_battery()
    except Exception:
        d["battery"] = None
    return d


# ------------------------------------------------------------- analysis ----
def analyze(d, startup):
    parts = [
        ("CPU load",             30 * clamp((d["cpu"] - 30) / 65), 30),
        ("RAM usage",            30 * clamp((d["ram"] - 50) / 45), 30),
        ("Disk space",           15 * clamp((d["disk_pct"] - 70) / 25), 15),
        ("Startup programs",      8 * clamp((len(startup) - 5) / 15), 8),
        ("Uptime (no restart)",   7 * clamp((d["uptime"] / 86400 - 3) / 11), 7),
        ("Background processes",  5 * clamp((d["procs"] - 100) / 150), 5),
        ("Temperature",           5 * clamp(((d["temp"] or 0) - 65) / 25), 5),
    ]
    score = round(sum(p for _, p, _ in parts))

    issues, recs = [], []
    top = d["top"][0] if d["top"] else None
    free_gb = d["disk_free"] / 1e9

    if d["cpu"] >= 85:
        issues.append(("bad", "CPU overload detected"))
        recs.append("Close heavy apps or end processes using the most CPU.")
    elif d["cpu"] >= 65:
        issues.append(("warn", "CPU usage is high"))
        recs.append("Check the Top Processes tab for CPU-hungry programs.")

    if d["ram"] >= 90:
        issues.append(("bad", "RAM usage is very high"))
        recs.append("Close unused apps and browser tabs; consider more RAM.")
    elif d["ram"] >= 75:
        issues.append(("warn", "High RAM usage detected"))
        recs.append("Close unnecessary applications.")

    if d["disk_pct"] >= 90 or free_gb < 10:
        issues.append(("bad", "Low disk space"))
        recs.append("Free storage: uninstall unused apps, empty temp/recycle bin.")
    elif d["disk_pct"] >= 80:
        issues.append(("warn", "Disk is getting full"))
        recs.append("Clean up large or old files.")

    if d["disk_rate"] > 100:
        issues.append(("warn", "Heavy disk activity right now"))
        recs.append("A program is reading/writing a lot (update, scan, backup).")

    if d["procs"] > 150:
        issues.append(("warn", "Too many background processes"))
        recs.append("Stop background apps you don't need.")

    review = sum(1 for s in startup if s[2] == "Review")
    if len(startup) > 10 or review >= 3:
        issues.append(("warn", f"Many startup programs ({len(startup)})"))
        recs.append("Disable unneeded startup apps (Task Manager > Startup).")

    if d["uptime"] > 7 * 86400:
        issues.append(("warn", "PC has not been restarted for a long time"))
        recs.append("Restart your PC to clear memory leaks and apply updates.")

    t = d["temp"]
    if t is not None and t >= 85:
        issues.append(("bad", "CPU temperature is critical"))
        recs.append("Clean dust from fans/vents; check cooling.")
    elif t is not None and t >= 75:
        issues.append(("warn", "CPU is running hot"))
        recs.append("Improve airflow; the CPU may be throttling.")

    b = d["battery"]
    if b and not b.power_plugged and b.percent < 20:
        issues.append(("warn", "Battery is low (power saving may slow the PC)"))
        recs.append("Plug in the charger.")

    if not issues:
        issues.append(("ok", "No major problems found"))
        recs.append("Your system looks healthy. Keep it up!")

    hog = None
    if top and (d["cpu"] >= 65 or d["ram"] >= 75):
        hog = f"Biggest resource user: {top[0]} (CPU {top[2]:.0f}%, RAM {top[3]:.1f}%)"
    return score, parts, issues, list(dict.fromkeys(recs)), hog


def build_report(d, startup, analysis):
    score, parts, issues, recs, hog = analysis
    icon = lambda v, bad, warn: "⚠️" if v >= warn else "✓"
    free_gb = d["disk_free"] / 1e9
    line = "=" * 32
    out = [line, "        OS ANALYZER", line, "",
           f"CPU Usage       : {d['cpu']:.0f}% {icon(d['cpu'], 85, 65)}",
           f"RAM Usage       : {d['ram']:.0f}% {icon(d['ram'], 90, 75)}",
           f"Disk Usage      : {d['disk_pct']:.0f}% {icon(d['disk_pct'], 90, 80)}",
           f"Free Storage    : {free_gb:.0f} GB {'⚠️' if free_gb < 20 else '✓'}",
           f"Running Process : {d['procs']}",
           f"Startup Programs: {len(startup)}",
           f"System Uptime   : {fmt_uptime(d['uptime'])}"]
    if d["temp"] is not None:
        out.append(f"CPU Temperature : {d['temp']:.0f} °C")
    if d["battery"]:
        out.append(f"Battery         : {d['battery'].percent:.0f}%")
    out += ["", f"SLOW PC SCORE   : {score}/100  ({verdict(score)})", "",
            "-" * 32, "DIAGNOSIS", "-" * 32]
    sym = {"bad": "⚠", "warn": "⚠", "ok": "✓"}
    out += [f"{sym[l]} {m}" for l, m in issues]
    if hog:
        out.append(f"• {hog}")
    out += ["", "Recommendation:"] + [f"- {r}" for r in recs]
    out += ["", f"Generated: {d['time']:%Y-%m-%d %H:%M:%S}", line]
    return "\n".join(out)


# ----------------------------------------------------------- GUI widgets ----
class Card(tk.Frame):
    def __init__(self, master, title):
        super().__init__(master, bg=CARD, padx=14, pady=10)
        self.pct, self.color = 0, GOOD
        tk.Label(self, text=title.upper(), bg=CARD, fg=MUTED,
                 font=(FONT, 9, "bold")).pack(anchor="w")
        self.value = tk.Label(self, text="--", bg=CARD, fg=FG,
                              font=(FONT, 20, "bold"))
        self.value.pack(anchor="w")
        self.sub = tk.Label(self, text=" ", bg=CARD, fg=MUTED, font=(FONT, 9))
        self.sub.pack(anchor="w")
        self.bar = tk.Canvas(self, height=6, bg=TRACK, highlightthickness=0)
        self.bar.pack(fill="x", pady=(6, 0))
        self.bar.bind("<Configure>", lambda e: self._draw())

    def _draw(self):
        self.bar.delete("all")
        w = self.bar.winfo_width()
        self.bar.create_rectangle(0, 0, w * clamp(self.pct / 100), 6,
                                  fill=self.color, width=0)

    def set(self, value, sub, pct, color):
        self.value.config(text=value, fg=color if color != GOOD else FG)
        self.sub.config(text=sub)
        self.pct, self.color = pct, color
        self._draw()


def make_button(master, text, cmd, primary=False):
    bg = ACCENT if primary else CARD
    b = tk.Button(master, text=text, command=cmd, bg=bg, fg="white" if primary else FG,
                  activebackground="#8aa2ff" if primary else "#262c55",
                  activeforeground="white", relief="flat", bd=0,
                  font=(FONT, 10, "bold"), padx=16, pady=8, cursor="hand2")
    hover = "#8aa2ff" if primary else "#262c55"
    b.bind("<Enter>", lambda e: b.config(bg=hover))
    b.bind("<Leave>", lambda e: b.config(bg=bg))
    return b


# ------------------------------------------------------------------ app ----
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Why Is My PC Slow? - OS Analyzer")
        self.geometry("1120x840")
        self.minsize(1000, 760)
        self.configure(bg=BG)

        self.q = queue.Queue()
        self.prev = {}
        self.paused = False
        self.latest = None
        self.analysis = None
        self.hist_cpu = collections.deque([0] * HISTORY, maxlen=HISTORY)
        self.hist_ram = collections.deque([0] * HISTORY, maxlen=HISTORY)
        self.shown_score = 0.0
        self.target_score = 0.0
        self.startup = get_startup_programs()

        self._style()
        self._build()
        psutil.cpu_percent(None)  # prime the counter
        threading.Thread(target=self._worker, daemon=True).start()
        self.after(300, self._poll)
        self.after(30, self._animate)

    # ---- styling
    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Treeview", background=CARD, fieldbackground=CARD,
                    foreground=FG, rowheight=28, borderwidth=0, font=(FONT, 10))
        s.configure("Treeview.Heading", background=BG2, foreground=MUTED,
                    relief="flat", font=(FONT, 10, "bold"))
        s.map("Treeview", background=[("selected", ACCENT)])
        s.map("Treeview.Heading", background=[("active", BG2)])
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=BG2, foreground=MUTED,
                    padding=(18, 9), font=(FONT, 10, "bold"), borderwidth=0)
        s.map("TNotebook.Tab", background=[("selected", CARD)],
              foreground=[("selected", FG)])

    # ---- layout
    def _build(self):
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=22, pady=(16, 8))
        tk.Label(head, text="🖥  Why Is My PC Slow?", bg=BG, fg=FG,
                 font=(FONT, 20, "bold")).pack(side="left")
        tk.Label(head, text="  OS Analyzer", bg=BG, fg=ACCENT,
                 font=(FONT, 20, "bold")).pack(side="left")
        btns = tk.Frame(head, bg=BG)
        btns.pack(side="right")
        self.pause_btn = make_button(btns, "⏸  Pause", self._toggle_pause)
        self.pause_btn.pack(side="left", padx=4)
        make_button(btns, "↻  Startup", self._reload_startup).pack(side="left", padx=4)
        make_button(btns, "💾  Export report", self._export, True).pack(side="left", padx=4)

        mid = tk.Frame(self, bg=BG)
        mid.pack(fill="x", padx=22)

        # score gauge
        g = tk.Frame(mid, bg=CARD)
        g.pack(side="left", padx=(0, 12), fill="y")
        tk.Label(g, text="SLOW PC SCORE", bg=CARD, fg=MUTED,
                 font=(FONT, 10, "bold")).pack(pady=(14, 0))
        self.gauge = tk.Canvas(g, width=300, height=270, bg=CARD,
                               highlightthickness=0)
        self.gauge.pack(padx=14)
        box = (40, 25, 260, 245)
        self.gauge.create_arc(*box, start=225, extent=-270, style="arc",
                              width=20, outline=TRACK)
        self.g_arc = self.gauge.create_arc(*box, start=225, extent=-0.1,
                                           style="arc", width=20, outline=GOOD)
        self.g_num = self.gauge.create_text(150, 125, text="0", fill=FG,
                                            font=(FONT, 52, "bold"))
        self.gauge.create_text(150, 168, text="0 = fast   100 = very slow",
                               fill=MUTED, font=(FONT, 9))
        self.g_txt = self.gauge.create_text(150, 205, text="Analyzing...",
                                            fill=FG, font=(FONT, 14, "bold"))

        # cards
        grid = tk.Frame(mid, bg=BG)
        grid.pack(side="left", fill="both", expand=True)
        names = ["CPU Usage", "RAM Usage", "Disk Usage", "Free Storage",
                 "Processes", "Uptime", "Battery", "CPU Temp"]
        self.cards = {}
        for i, n in enumerate(names):
            c = Card(grid, n)
            c.grid(row=i // 4, column=i % 4, sticky="nsew", padx=5, pady=5)
            self.cards[n] = c
        for i in range(4):
            grid.columnconfigure(i, weight=1, uniform="c")
        for i in range(2):
            grid.rowconfigure(i, weight=1)

        # live chart
        ch = tk.Frame(self, bg=CARD)
        ch.pack(fill="x", padx=22, pady=10)
        top = tk.Frame(ch, bg=CARD)
        top.pack(fill="x", padx=14, pady=(8, 0))
        tk.Label(top, text="LIVE USAGE (last 2 min)", bg=CARD, fg=MUTED,
                 font=(FONT, 9, "bold")).pack(side="left")
        tk.Label(top, text="● RAM", bg=CARD, fg=PURPLE,
                 font=(FONT, 9, "bold")).pack(side="right", padx=6)
        tk.Label(top, text="● CPU", bg=CARD, fg=ACCENT,
                 font=(FONT, 9, "bold")).pack(side="right", padx=6)
        self.chart = tk.Canvas(ch, height=120, bg=CARD, highlightthickness=0)
        self.chart.pack(fill="x", padx=14, pady=(2, 10))
        self.chart.bind("<Configure>", lambda e: self._draw_chart())

        # tabs
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=22, pady=(0, 6))

        t1 = tk.Frame(nb, bg=CARD)
        self.diag = tk.Text(t1, bg=CARD, fg=FG, bd=0, padx=18, pady=14,
                            font=(FONT, 11), wrap="word", state="disabled",
                            highlightthickness=0, cursor="arrow")
        self.diag.pack(fill="both", expand=True)
        for tag, col in (("bad", BAD), ("warn", WARN), ("ok", GOOD)):
            self.diag.tag_config(tag, foreground=col, font=(FONT, 11, "bold"))
        self.diag.tag_config("h", foreground=ACCENT, font=(FONT, 12, "bold"),
                             spacing1=10, spacing3=4)
        self.diag.tag_config("muted", foreground=MUTED)
        nb.add(t1, text="  Diagnosis  ")

        self.proc_tree = self._tree(nb, ("Process", "PID", "CPU %", "RAM %"),
                                    (380, 90, 100, 100), "  Top Processes  ")
        self.start_tree = self._tree(nb, ("Program", "Location", "Verdict"),
                                     (260, 520, 100), "  Startup Programs  ")
        self.score_tree = self._tree(nb, ("Factor", "Points added", "Max"),
                                     (380, 140, 100), "  Score Breakdown  ")

        self.status = tk.Label(self, text="Starting...", bg=BG, fg=MUTED,
                               font=(FONT, 9), anchor="w")
        self.status.pack(fill="x", padx=24, pady=(0, 8))
        self._fill_startup()

    def _tree(self, nb, cols, widths, title):
        f = tk.Frame(nb, bg=CARD)
        tree = ttk.Treeview(f, columns=cols, show="headings", selectmode="browse")
        for c, w in zip(cols, widths):
            tree.heading(c, text=c)
            tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(f, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tree.tag_configure("review", foreground=WARN)
        tree.tag_configure("hot", foreground=BAD)
        nb.add(f, text=title)
        return tree

    # ---- background sampling
    def _worker(self):
        time.sleep(0.6)
        while True:
            if not self.paused:
                try:
                    self.q.put(collect(self.prev))
                except Exception as e:
                    print("collect error:", e)
            time.sleep(REFRESH_SECONDS)

    def _poll(self):
        data = None
        while not self.q.empty():
            data = self.q.get()
        if data:
            self._update(data)
        self.after(300, self._poll)

    # ---- updating the UI
    def _update(self, d):
        self.latest = d
        self.analysis = analyze(d, self.startup)
        score, parts, issues, recs, hog = self.analysis
        self.target_score = score

        self.hist_cpu.append(d["cpu"])
        self.hist_ram.append(d["ram"])
        self._draw_chart()

        c = self.cards
        c["CPU Usage"].set(f"{d['cpu']:.0f}%", f"{NCPU} logical cores",
                           d["cpu"], pct_color(d["cpu"]))
        c["RAM Usage"].set(f"{d['ram']:.0f}%",
                           f"{d['ram_used']/1e9:.1f} / {d['ram_total']/1e9:.1f} GB",
                           d["ram"], pct_color(d["ram"]))
        c["Disk Usage"].set(f"{d['disk_pct']:.0f}%",
                            f"Activity {d['disk_rate']:.1f} MB/s",
                            d["disk_pct"], pct_color(d["disk_pct"]))
        free_gb = d["disk_free"] / 1e9
        c["Free Storage"].set(f"{free_gb:.0f} GB",
                              f"of {d['disk_total']/1e9:.0f} GB total",
                              100 - d["disk_free"] / d["disk_total"] * 100,
                              BAD if free_gb < 10 else WARN if free_gb < 20 else GOOD)
        c["Processes"].set(str(d["procs"]), "running now",
                           clamp(d["procs"] / 250) * 100,
                           BAD if d["procs"] > 200 else WARN if d["procs"] > 150 else GOOD)
        days = d["uptime"] / 86400
        c["Uptime"].set(fmt_uptime(d["uptime"]), "since last boot",
                        clamp(days / 14) * 100,
                        BAD if days > 10 else WARN if days > 7 else GOOD)
        b = d["battery"]
        if b:
            state = "Charging" if b.power_plugged else "On battery"
            if not b.power_plugged and b.secsleft and b.secsleft > 0:
                state += f" · {fmt_uptime(b.secsleft)} left"
            c["Battery"].set(f"{b.percent:.0f}%", state, b.percent,
                             BAD if b.percent < 20 and not b.power_plugged else GOOD)
        else:
            c["Battery"].set("N/A", "No battery (desktop PC)", 0, MUTED)
        t = d["temp"]
        if t is not None:
            c["CPU Temp"].set(f"{t:.0f} °C", "sensor reading", t,
                              BAD if t >= 85 else WARN if t >= 75 else GOOD)
        else:
            c["CPU Temp"].set("N/A", "Not supported on this system", 0, MUTED)

        self._fill_diag(issues, recs, hog)
        self._fill_processes(d["top"])
        self._fill_scores(parts)
        self.status.config(text=f"Last scan {d['time']:%H:%M:%S}  ·  "
                                f"{platform.system()} {platform.release()}  ·  "
                                f"refreshes every {REFRESH_SECONDS}s"
                                + ("  ·  PAUSED" if self.paused else ""))

    def _fill_diag(self, issues, recs, hog):
        t = self.diag
        t.config(state="normal")
        t.delete("1.0", "end")
        t.insert("end", "DIAGNOSIS\n", "h")
        for level, msg in issues:
            t.insert("end", ("✓  " if level == "ok" else "⚠  ") + msg + "\n", level)
        if hog:
            t.insert("end", "\n•  " + hog + "\n", "muted")
        t.insert("end", "\nRECOMMENDATION\n", "h")
        for r in recs:
            t.insert("end", "→  " + r + "\n")
        t.config(state="disabled")

    def _fill_processes(self, procs):
        tree = self.proc_tree
        tree.delete(*tree.get_children())
        for name, pid, cpu, mem in procs:
            tag = ("hot",) if cpu > 25 or mem > 10 else ()
            tree.insert("", "end", values=(name, pid, f"{cpu:.1f}", f"{mem:.1f}"),
                        tags=tag)

    def _fill_scores(self, parts):
        tree = self.score_tree
        tree.delete(*tree.get_children())
        for name, pts, mx in parts:
            tree.insert("", "end", values=(name, f"{pts:.1f}", mx),
                        tags=("hot",) if pts > mx * 0.6 else ())

    def _fill_startup(self):
        tree = self.start_tree
        tree.delete(*tree.get_children())
        if not self.startup:
            tree.insert("", "end", values=("No startup entries found", "", ""))
        for name, cmd, flag in self.startup:
            tree.insert("", "end", values=(name, cmd, flag),
                        tags=("review",) if flag == "Review" else ())

    # ---- drawing
    def _draw_chart(self):
        c = self.chart
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 50:
            return
        for p in (25, 50, 75):
            y = h - h * p / 100
            c.create_line(0, y, w, y, fill=TRACK, dash=(2, 4))
            c.create_text(4, y - 7, text=f"{p}%", fill=MUTED, anchor="w",
                          font=(FONT, 8))
        for data, col in ((self.hist_ram, PURPLE), (self.hist_cpu, ACCENT)):
            step = w / (HISTORY - 1)
            pts = []
            for i, v in enumerate(data):
                pts += [i * step, h - 3 - (h - 6) * v / 100]
            c.create_line(*pts, fill=col, width=2, smooth=True)

    def _draw_gauge(self):
        s = self.shown_score
        col = score_color(s)
        self.gauge.itemconfig(self.g_arc, extent=-max(0.1, 270 * s / 100),
                              outline=col)
        self.gauge.itemconfig(self.g_num, text=str(round(s)), fill=col)
        self.gauge.itemconfig(self.g_txt, text=verdict(s), fill=col)

    def _animate(self):
        diff = self.target_score - self.shown_score
        if abs(diff) > 0.2:
            self.shown_score += diff * 0.12
            self._draw_gauge()
        self.after(30, self._animate)

    # ---- buttons
    def _toggle_pause(self):
        self.paused = not self.paused
        self.pause_btn.config(text="▶  Resume" if self.paused else "⏸  Pause")

    def _reload_startup(self):
        self.startup = get_startup_programs()
        self._fill_startup()

    def _export(self):
        if not self.latest:
            messagebox.showinfo("OS Analyzer", "Please wait for the first scan.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt", initialfile="os_analyzer_report.txt",
            filetypes=[("Text file", "*.txt")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(build_report(self.latest, self.startup, self.analysis))
            messagebox.showinfo("OS Analyzer", f"Report saved:\n{path}")


if __name__ == "__main__":
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text
        except Exception:
            pass
    App().mainloop()
