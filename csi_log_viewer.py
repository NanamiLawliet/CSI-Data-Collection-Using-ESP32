"""
csi_log_viewer.py  —  Standalone CSI data log viewer
Reads session CSVs written by web_app.py, tails them live.
Run separately from web_app.py:  python csi_log_viewer.py
No extra dependencies — only stdlib (tkinter, csv, json).
"""

import tkinter as tk
from tkinter import ttk, filedialog
import csv
import json
import os
import glob

SESSIONS_DIR = "sessions"
POLL_MS = 500       # how often to check for new rows (milliseconds)
MAX_ROWS = 20_000   # max rows kept in memory

# CSV fieldnames written by web_app.py (must match setup_csv_file)
FIELDNAMES = [
    'timestamp', 'group_id', 'tx_id',
    'packet_type', 'rssi', 'rate', 'channel', 'bandwidth',
    'data_length', 'esp_timestamp', 'csi_data', 'seq',
]

TX_TAGS = {
    '1': 'tx1',
    '2': 'tx2',
    '3': 'tx3',
    '4': 'tx4',
}

TX_COLORS = {
    'tx1': '#3ea6ff',
    'tx2': '#ff6b6b',
    'tx3': '#4ecdc4',
    'tx4': '#ffd93d',
}

SC_SAMPLE_INDICES = [1, 5, 9, 13]


def _find_latest_csv():
    files = glob.glob(os.path.join(SESSIONS_DIR, "**", "csi_data_*.csv"), recursive=True)
    return max(files, key=os.path.getmtime) if files else None


def _sample_subcarriers(csi_json_str):
    """Return dict of {idx: value_str} for SC_SAMPLE_INDICES."""
    try:
        data = json.loads(csi_json_str)
        return {i: f"{data[i]:5.0f}" if i < len(data) else "    —" for i in SC_SAMPLE_INDICES}
    except Exception:
        return {i: "    —" for i in SC_SAMPLE_INDICES}


class CsiLogViewer:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("CSI Log Viewer")
        self.root.geometry("1200x650")
        self.root.configure(bg="#1e1e1e")

        self.csv_path: str | None = None
        self.file_pos: int = 0      # byte position in currently open file
        self.header_read: bool = False
        self.fieldnames_detected: list | None = None
        self.all_rows: list = []    # bounded in-memory buffer
        self.poll_job = None

        self._build_ui()
        self._auto_open_latest()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_toolbar()
        self._build_filter_bar()
        self._build_text_area()
        self._build_statusbar()

    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg="#2d2d2d", padx=8, pady=6)
        bar.pack(fill=tk.X)

        def btn(text, cmd, color="#555"):
            return tk.Button(bar, text=text, command=cmd, bg=color, fg="white",
                             font=("Consolas", 9, "bold"), relief=tk.FLAT,
                             padx=10, pady=4, cursor="hand2")

        btn("Open CSV",       self._open_file,   "#457b9d").pack(side=tk.LEFT, padx=3)
        btn("Latest Session", self._open_latest, "#7D8CA3").pack(side=tk.LEFT, padx=3)
        btn("Clear",          self._clear_display, "#59544B").pack(side=tk.LEFT, padx=3)

        tk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        self.auto_scroll = tk.BooleanVar(value=True)
        tk.Checkbutton(bar, text="Auto-scroll", variable=self.auto_scroll,
                       bg="#2d2d2d", fg="#cccccc", selectcolor="#3a3a3a",
                       activebackground="#2d2d2d", activeforeground="white",
                       font=("Consolas", 9)).pack(side=tk.LEFT)

        self.pause_var = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="Pause live", variable=self.pause_var,
                       bg="#2d2d2d", fg="#cccccc", selectcolor="#3a3a3a",
                       activebackground="#2d2d2d", activeforeground="white",
                       font=("Consolas", 9)).pack(side=tk.LEFT, padx=8)

    def _build_filter_bar(self):
        bar = tk.Frame(self.root, bg="#252526", padx=8, pady=5)
        bar.pack(fill=tk.X)

        lbl = lambda text: tk.Label(bar, text=text, bg="#252526", fg="#9cdcfe",
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

        vsb = tk.Scrollbar(frame, orient=tk.VERTICAL, command=self.text.yview)
        hsb = tk.Scrollbar(self.root, orient=tk.HORIZONTAL, command=self.text.xview)
        self.text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        vsb.pack(side=tk.RIGHT,  fill=tk.Y)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        hsb.pack(fill=tk.X)

        # colour tags
        for tag, color in TX_COLORS.items():
            self.text.tag_config(tag, foreground=color, font=("Consolas", 9, "bold"))
        self.text.tag_config("header", foreground="#6a9955", font=("Consolas", 8))
        self.text.tag_config("dim",    foreground="#444444")
        self.text.tag_config("ts",     foreground="#808080")

    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg="#007acc", padx=6, pady=2)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        self.status_var = tk.StringVar(value="No file open — click Latest Session or Open CSV")
        tk.Label(bar, textvariable=self.status_var, bg="#007acc", fg="white",
                 font=("Consolas", 8), anchor=tk.W).pack(fill=tk.X)

    # ── file handling ─────────────────────────────────────────────────────────

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

    def _auto_open_latest(self):
        path = _find_latest_csv()
        if path:
            self._load(path)
        else:
            self.status_var.set("No session found. Start web_app.py and begin logging.")

    def _load(self, path: str):
        self._stop_poll()
        self.csv_path = path
        self.file_pos = 0
        self.header_read = False
        self.fieldnames_detected = None
        self.all_rows.clear()
        self._clear_display()
        self._insert_header()
        self.status_var.set(f"Watching: {os.path.relpath(path)}")
        self._sync_tx_filter([])
        self._start_poll()

    # ── CSV tailing ───────────────────────────────────────────────────────────

    def _read_new_rows(self) -> list[dict]:
        """Read only bytes added since last call. Returns new CSI rows."""
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

            # Only process complete lines (up to last newline)
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
                # First chunk: first line is the header
                self.fieldnames_detected = next(csv.reader([lines[0]]), FIELDNAMES)
                self.header_read = True
                lines = lines[1:]

            if not lines:
                return []

            fn = self.fieldnames_detected or FIELDNAMES
            reader = csv.DictReader(lines, fieldnames=fn)
            for row in reader:
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
                # Sync TX filter with any newly seen tx_ids
                seen_ids = sorted({r.get('tx_id', '0') for r in self.all_rows})
                self._sync_tx_filter(seen_ids)
                # Only append rows that pass current filter
                self._append_rows([r for r in new if self._passes_filter(r)])
        self.poll_job = self.root.after(POLL_MS, self._do_poll)

    # ── display ───────────────────────────────────────────────────────────────

    def _insert_header(self):
        header = (
            f"{'TIME':>12}  {'GRP':>7}  {'TX':>4}  "
            f"{'RSSI':>5}  {'LEN':>4}  "
            + "  ".join(f"SC{i:>2}" for i in SC_SAMPLE_INDICES) + "\n"
        )
        self._write(header, "header")
        self._write("─" * 80 + "\n", "dim")

    def _format_row(self, row: dict) -> str:
        ts = row.get('timestamp', '')
        time_part = ts.split('T')[1][:12] if 'T' in ts else ts[:12]
        grp  = row.get('group_id', '—')
        tx   = row.get('tx_id',    '?')
        rssi = row.get('rssi',     '—')
        dlen = row.get('data_length', '—')
        sc   = _sample_subcarriers(row.get('csi_data', '[]'))
        sc_str = "  ".join(f"{sc[i]:>5}" for i in SC_SAMPLE_INDICES)
        return (
            f"{time_part:>12}  {grp:>7}  [TX{tx}]  "
            f"{rssi:>5}dBm  {dlen:>4}B  {sc_str}\n"
        )

    def _append_rows(self, rows: list[dict]):
        if not rows:
            return
        self.text.config(state=tk.NORMAL)
        for row in rows:
            tx_id = str(row.get('tx_id', '0'))
            tag = TX_TAGS.get(tx_id, 'dim')
            self.text.insert(tk.END, self._format_row(row), tag)
        self.text.config(state=tk.DISABLED)
        if self.auto_scroll.get():
            self.text.see(tk.END)
        # update row count
        total = self.text.index(tk.END).split('.')[0]
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
        if tx_f != "All":
            wanted = tx_f.replace("TX", "")
            if str(row.get('tx_id', '')) != wanted:
                return False
        if grp_f and str(row.get('group_id', '')) != grp_f:
            return False
        return True

    def _refilter(self):
        """Redraw everything from the in-memory buffer with current filters."""
        self._clear_display()
        self._insert_header()
        self._append_rows([r for r in self.all_rows if self._passes_filter(r)])

    def _reset_filter(self):
        self.tx_filter.set("All")
        self.group_filter.set("")
        self._refilter()

    def _sync_tx_filter(self, tx_ids: list[str]):
        """Add any newly observed TX IDs to the filter dropdown."""
        current = list(self.tx_combo['values'])
        changed = False
        for tx_id in tx_ids:
            label = f"TX{tx_id}"
            if label not in current:
                current.append(label)
                changed = True
        if changed:
            self.tx_combo['values'] = current


def main():
    root = tk.Tk()
    root.resizable(True, True)
    CsiLogViewer(root)
    root.mainloop()


if __name__ == '__main__':
    main()
