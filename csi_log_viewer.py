"""
csi_log_viewer.py  —  Standalone CSI collector + live viewer
Connects to the ESP32 RX (Slave) serial port, writes session CSVs,
and displays rows live — no web_app.py needed.

Run:  python csi_log_viewer.py
Deps: pyserial  (pip install pyserial)  — everything else is stdlib
"""

import tkinter as tk
from tkinter import ttk, filedialog
import csv
import json
import os
import re
import glob
import threading
import datetime
import time
import uuid

try:
    import serial
    SERIAL_OK = True
except ImportError:
    SERIAL_OK = False

# ── constants ─────────────────────────────────────────────────────────────────

SESSIONS_DIR   = "sessions"
POLL_MS        = 400
MAX_ROWS       = 20_000
GROUP_WINDOW_MS = 50   # must match firmware SEND_INTERVAL_MS

CSI_REGEX = re.compile(r'CSI_START(\{.+\})CSI_END')

FIELDNAMES = [
    'timestamp', 'group_id', 'tx_id',
    'packet_type', 'rssi', 'rate', 'channel', 'bandwidth',
    'data_length', 'esp_timestamp', 'csi_data', 'seq',
]

SC_INDICES = [1, 5, 9, 13]

TX_TAGS   = {str(i): f'tx{i}' for i in range(1, 11)}
TX_COLORS = {
    'tx1': '#3ea6ff', 'tx2': '#ff6b6b', 'tx3': '#4ecdc4', 'tx4': '#ffd93d',
    'tx5': '#a8e6cf', 'tx6': '#dcedc1', 'tx7': '#ffd3b6', 'tx8': '#ffaaa5',
    'tx9': '#ff8b94', 'tx10': '#b5eedd'
}


def _find_latest_csv():
    files = glob.glob(os.path.join(SESSIONS_DIR, "**", "csi_data_*.csv"), recursive=True)
    return max(files, key=os.path.getmtime) if files else None


def _sample_sc(csi_json_str):
    try:
        data = json.loads(csi_json_str)
        return {i: f"{data[i]:5.0f}" if i < len(data) else "    —" for i in SC_INDICES}
    except Exception:
        return {i: "    —" for i in SC_INDICES}


# ── main class ────────────────────────────────────────────────────────────────

class CsiLogViewer:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("CSI Log Viewer")
        self.root.geometry("1250x700")
        self.root.configure(bg="#1e1e1e")

        # serial / recording state
        self.serial_conn   = None
        self.serial_thread = None
        self.serial_running = False
        self.csv_file      = None
        self.csv_writer    = None
        self.session_dir   = None
        self.session_start = None
        self.pkt_count     = 0

        # viewer state
        self.csv_path: str | None = None
        self.file_pos    = 0
        self.header_read = False
        self.fn_detected = None
        self.all_rows: list = []
        self.poll_job  = None

        self._build_ui()
        if not SERIAL_OK:
            self.status_var.set("WARNING: pyserial not installed — serial recording disabled")

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_serial_bar()
        self._build_filter_bar()
        self._build_text_area()
        self._build_statusbar()

    def _build_serial_bar(self):
        bar = tk.Frame(self.root, bg="#2d2d2d", padx=8, pady=6)
        bar.pack(fill=tk.X)

        def lbl(text):
            return tk.Label(bar, text=text, bg="#2d2d2d", fg="#9cdcfe",
                            font=("Consolas", 9))

        def btn(text, cmd, color="#555"):
            return tk.Button(bar, text=text, command=cmd, bg=color, fg="white",
                             font=("Consolas", 9, "bold"), relief=tk.FLAT,
                             padx=10, pady=4, cursor="hand2")

        # ── serial section ──
        lbl("Port:").pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value="COM9")
        tk.Entry(bar, textvariable=self.port_var, width=8,
                 bg="#3c3c3c", fg="#d4d4d4", insertbackground="white",
                 relief=tk.FLAT, font=("Consolas", 9)).pack(side=tk.LEFT, padx=(3, 4))

        self.btn_connect = btn("⏺  Record", self._toggle_serial, "#c0392b")
        self.btn_connect.pack(side=tk.LEFT, padx=3)

        self.rec_label = tk.Label(bar, text="", bg="#2d2d2d", fg="#c0392b",
                                  font=("Consolas", 9, "bold"))
        self.rec_label.pack(side=tk.LEFT, padx=4)

        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)

        # ── viewer section ──
        btn("Open CSV",       self._open_file,      "#457b9d").pack(side=tk.LEFT, padx=3)
        btn("Latest Session", self._open_latest,    "#7D8CA3").pack(side=tk.LEFT, padx=3)
        btn("Clear View",     self._clear_display,  "#59544B").pack(side=tk.LEFT, padx=3)

        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)

        self.auto_scroll = tk.BooleanVar(value=True)
        tk.Checkbutton(bar, text="Auto-scroll", variable=self.auto_scroll,
                       bg="#2d2d2d", fg="#cccccc", selectcolor="#3a3a3a",
                       activebackground="#2d2d2d", activeforeground="white",
                       font=("Consolas", 9)).pack(side=tk.LEFT)

        self.pause_var = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="Pause view", variable=self.pause_var,
                       bg="#2d2d2d", fg="#cccccc", selectcolor="#3a3a3a",
                       activebackground="#2d2d2d", activeforeground="white",
                       font=("Consolas", 9)).pack(side=tk.LEFT, padx=8)

    def _build_filter_bar(self):
        bar = tk.Frame(self.root, bg="#252526", padx=8, pady=5)
        bar.pack(fill=tk.X)

        def lbl(text):
            return tk.Label(bar, text=text, bg="#252526", fg="#9cdcfe",
                            font=("Consolas", 9))

        lbl("TX:").pack(side=tk.LEFT)
        self.tx_filter = tk.StringVar(value="All")
        self.tx_combo = ttk.Combobox(bar, textvariable=self.tx_filter,
                                     values=["All"], state="readonly",
                                     width=9, font=("Consolas", 9))
        self.tx_combo.pack(side=tk.LEFT, padx=(3, 12))
        self.tx_combo.bind("<<ComboboxSelected>>", lambda _: self._refilter())

        lbl("Group ID:").pack(side=tk.LEFT)
        self.group_filter = tk.StringVar()
        tk.Entry(bar, textvariable=self.group_filter, width=8,
                 bg="#3c3c3c", fg="#d4d4d4", insertbackground="white",
                 relief=tk.FLAT, font=("Consolas", 9)).pack(side=tk.LEFT, padx=(3, 6))

        tk.Button(bar, text="Filter", command=self._refilter,
                  bg="#3a3a3a", fg="#cccccc", relief=tk.FLAT,
                  font=("Consolas", 9), padx=8).pack(side=tk.LEFT)
        tk.Button(bar, text="Reset", command=self._reset_filter,
                  bg="#3a3a3a", fg="#cccccc", relief=tk.FLAT,
                  font=("Consolas", 9), padx=8).pack(side=tk.LEFT, padx=4)

        self.row_count_var = tk.StringVar(value="0 rows")
        tk.Label(bar, textvariable=self.row_count_var, bg="#252526", fg="#6a9955",
                 font=("Consolas", 9)).pack(side=tk.RIGHT, padx=8)

    def _build_text_area(self):
        frame = tk.Frame(self.root)
        frame.pack(fill=tk.BOTH, expand=True)

        self.text = tk.Text(frame, bg="#1e1e1e", fg="#d4d4d4",
                            font=("Consolas", 9), wrap=tk.NONE,
                            insertbackground="white", state=tk.DISABLED,
                            selectbackground="#264f78")

        vsb = tk.Scrollbar(frame, orient=tk.VERTICAL,   command=self.text.yview)
        hsb = tk.Scrollbar(self.root, orient=tk.HORIZONTAL, command=self.text.xview)
        self.text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        hsb.pack(fill=tk.X)

        for tag, color in TX_COLORS.items():
            self.text.tag_config(tag, foreground=color, font=("Consolas", 9, "bold"))
        self.text.tag_config("header", foreground="#6a9955", font=("Consolas", 8))
        self.text.tag_config("dim",    foreground="#444444")

    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg="#007acc", padx=6, pady=2)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        self.status_var = tk.StringVar(value="Ready")
        tk.Label(bar, textvariable=self.status_var, bg="#007acc", fg="white",
                 font=("Consolas", 8), anchor=tk.W).pack(fill=tk.X)

    # ── serial recording ──────────────────────────────────────────────────────

    def _toggle_serial(self):
        if self.serial_running:
            self._stop_serial()
        else:
            self._start_serial()

    def _start_serial(self):
        if not SERIAL_OK:
            self.status_var.set("pyserial not installed — pip install pyserial")
            return
        port = self.port_var.get().strip()
        try:
            self.serial_conn = serial.Serial(port, 921600, timeout=1)
        except Exception as e:
            self.status_var.set(f"Serial error: {e}")
            return

        # create session directory + CSV
        sid = str(uuid.uuid4())[:8]
        self.session_dir = os.path.join(SESSIONS_DIR, f"session-{sid}")
        os.makedirs(self.session_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        csv_path = os.path.join(self.session_dir, f"csi_data_{ts}.csv")

        self.csv_file = open(csv_path, 'w', newline='', encoding='utf-8')
        self.csv_writer = csv.DictWriter(self.csv_file, fieldnames=FIELDNAMES)
        self.csv_writer.writeheader()
        self.csv_file.flush()

        self.session_start = time.time()
        self.pkt_count = 0
        self.serial_running = True

        self.btn_connect.config(text="⏹  Stop", bg="#27ae60")
        self._blink_rec()

        # open the new CSV in the viewer immediately
        self._load(csv_path)

        self.serial_thread = threading.Thread(target=self._serial_loop, daemon=True)
        self.serial_thread.start()
        self.status_var.set(f"Recording → {os.path.relpath(csv_path)}")

    def _stop_serial(self):
        self.serial_running = False
        if self.serial_conn:
            try:
                self.serial_conn.close()
            except Exception:
                pass
            self.serial_conn = None
        if self.csv_file:
            self.csv_file.close()
            self.csv_file = None
        self.csv_writer = None
        self.btn_connect.config(text="⏺  Record", bg="#c0392b")
        self.rec_label.config(text="")
        self.status_var.set(f"Stopped. {self.pkt_count} CSI frames written.")

    def _blink_rec(self):
        if not self.serial_running:
            return
        cur = self.rec_label.cget("text")
        self.rec_label.config(text="● REC" if cur == "" else "")
        self.root.after(600, self._blink_rec)

    def _serial_loop(self):
        """Runs in background thread: reads serial, parses CSI, writes CSV."""
        while self.serial_running:
            try:
                if self.serial_conn and self.serial_conn.in_waiting > 0:
                    raw = self.serial_conn.readline()
                    line = raw.decode('utf-8', errors='ignore').strip()
                    if line:
                        self._handle_line(line)
                else:
                    time.sleep(0.005)
            except Exception:
                pass

    def _handle_line(self, line: str):
        m = CSI_REGEX.search(line)
        if not m:
            return
        try:
            pkt = json.loads(m.group(1))
        except json.JSONDecodeError:
            return

        data_length = pkt.get('data_length', 0)
        if data_length <= 0:
            return

        tx_id     = int(pkt.get('tx_id', 0))
        esp_ts_us = int(pkt.get('esp_timestamp', 0))
        group_id  = int((esp_ts_us / 1000) / GROUP_WINDOW_MS)

        csi_data = pkt.get('csi_data', [])
        row = {
            'timestamp':    datetime.datetime.now().isoformat(),
            'group_id':     group_id,
            'tx_id':        tx_id,
            'packet_type':  'csi',
            'rssi':         pkt.get('rssi', 0),
            'rate':         pkt.get('rate', 0),
            'channel':      pkt.get('channel', 0),
            'bandwidth':    pkt.get('bandwidth', 0),
            'data_length':  data_length,
            'esp_timestamp':pkt.get('esp_timestamp', 0),
            'csi_data':     json.dumps(csi_data),
            'seq':          '',
        }
        if self.csv_writer:
            self.csv_writer.writerow(row)
            self.csv_file.flush()
            self.pkt_count += 1

    # ── CSV viewer (tail) ─────────────────────────────────────────────────────

    def _open_file(self):
        path = filedialog.askopenfilename(
            initialdir=SESSIONS_DIR,
            title="Open CSI CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self._load(path)

    def _open_latest(self):
        path = _find_latest_csv()
        if path:
            self._load(path)
        else:
            self.status_var.set("No CSV found in sessions/")

    def _load(self, path: str):
        self._stop_poll()
        self.csv_path    = path
        self.file_pos    = 0
        self.header_read = False
        self.fn_detected = None
        self.all_rows.clear()
        self._clear_display()
        self._insert_header()
        self._sync_tx_filter([])
        if not self.serial_running:
            self.status_var.set(f"Watching: {os.path.relpath(path)}")
        self._start_poll()

    def _read_new_rows(self) -> list:
        if not self.csv_path:
            return []
        rows = []
        try:
            fsize = os.path.getsize(self.csv_path)
            if fsize <= self.file_pos:
                return []
            with open(self.csv_path, 'rb') as f:
                f.seek(self.file_pos)
                raw = f.read()
            last_nl = raw.rfind(b'\n')
            if last_nl == -1:
                return []
            raw = raw[:last_nl + 1]
            self.file_pos += len(raw)
            text = raw.decode('utf-8', errors='ignore')
            lines = text.splitlines()
            if not lines:
                return []
            if not self.header_read:
                self.fn_detected = next(csv.reader([lines[0]]), FIELDNAMES)
                self.header_read = True
                lines = lines[1:]
            if not lines:
                return []
            fn = self.fn_detected or FIELDNAMES
            for row in csv.DictReader(lines, fieldnames=fn):
                if row.get('packet_type') == 'csi':
                    rows.append(row)
        except Exception:
            pass
        return rows

    def _start_poll(self):
        self._do_poll()

    def _stop_poll(self):
        if self.poll_job:
            self.root.after_cancel(self.poll_job)
            self.poll_job = None

    def _do_poll(self):
        if not self.pause_var.get():
            new = self._read_new_rows()
            if new:
                self.all_rows.extend(new)
                if len(self.all_rows) > MAX_ROWS:
                    self.all_rows = self.all_rows[-MAX_ROWS:]
                seen = sorted({r.get('tx_id', '0') for r in self.all_rows})
                self._sync_tx_filter(seen)
                self._append_rows([r for r in new if self._passes_filter(r)])
        self.poll_job = self.root.after(POLL_MS, self._do_poll)

    # ── display helpers ───────────────────────────────────────────────────────

    def _insert_header(self):
        header = (
            f"{'TIME':>12}  {'GRP':>7}  {'TX':>4}  "
            f"{'RSSI':>5}  {'LEN':>4}  "
            + "  ".join(f"SC{i:>2}" for i in SC_INDICES) + "\n"
        )
        self._write(header, "header")
        self._write("─" * 80 + "\n", "dim")

    def _fmt_row(self, row: dict) -> str:
        ts   = row.get('timestamp', '')
        tpart = ts.split('T')[1][:12] if 'T' in ts else ts[:12]
        grp  = row.get('group_id', '—')
        tx   = row.get('tx_id', '?')
        rssi = row.get('rssi', '—')
        dlen = row.get('data_length', '—')
        sc   = _sample_sc(row.get('csi_data', '[]'))
        sc_s = "  ".join(f"{sc[i]:>5}" for i in SC_INDICES)
        return f"{tpart:>12}  {grp:>7}  [TX{tx}]  {rssi:>5}dBm  {dlen:>4}B  {sc_s}\n"

    def _append_rows(self, rows: list):
        if not rows:
            return
        self.text.config(state=tk.NORMAL)
        for row in rows:
            tag = TX_TAGS.get(str(row.get('tx_id', '0')), 'dim')
            self.text.insert(tk.END, self._fmt_row(row), tag)
        self.text.config(state=tk.DISABLED)
        if self.auto_scroll.get():
            self.text.see(tk.END)
        self.row_count_var.set(f"{len(self.all_rows)} rows in buffer")

    def _write(self, text: str, tag: str = ""):
        self.text.config(state=tk.NORMAL)
        self.text.insert(tk.END, text, tag)
        self.text.config(state=tk.DISABLED)

    def _clear_display(self):
        self.text.config(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.config(state=tk.DISABLED)
        self.row_count_var.set("0 rows in buffer")

    # ── filter ────────────────────────────────────────────────────────────────

    def _passes_filter(self, row: dict) -> bool:
        tx_f  = self.tx_filter.get()
        grp_f = self.group_filter.get().strip()
        if tx_f != "All" and str(row.get('tx_id', '')) != tx_f.replace("TX", ""):
            return False
        if grp_f and str(row.get('group_id', '')) != grp_f:
            return False
        return True

    def _refilter(self):
        self._clear_display()
        self._insert_header()
        self._append_rows([r for r in self.all_rows if self._passes_filter(r)])

    def _reset_filter(self):
        self.tx_filter.set("All")
        self.group_filter.set("")
        self._refilter()

    def _sync_tx_filter(self, tx_ids: list):
        current = list(self.tx_combo['values'])
        changed = False
        for tx_id in tx_ids:
            label = f"TX{tx_id}"
            if label not in current:
                current.append(label)
                changed = True
        if changed:
            self.tx_combo['values'] = current

    # ── cleanup ───────────────────────────────────────────────────────────────

    def on_close(self):
        self._stop_serial()
        self._stop_poll()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = CsiLogViewer(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == '__main__':
    main()
