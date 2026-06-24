from flask import Flask, render_template, jsonify, request
import serial
import json
import re
import datetime
import csv
import threading
import time
import os
import atexit
import signal
from collections import deque
import uuid

CSI_REGEX = re.compile(r'CSI_START(\{.+\})CSI_END')
ESPNOW_REGEX = re.compile(r'RX,SEQ=(\d+),RSSI=(-?\d+),TS=(\d+)')
ESPNOW_LOG_REGEX = re.compile(r'seq=(\d+)(?:\s+tx_id=(\d+))?\s+rssi=(-?\d+)', re.IGNORECASE)

# Base diag fields; txN=M pairs are parsed separately via TX_DIAG_REGEX for N-TX extensibility
CSI_DIAG_REGEX = re.compile(
    r'callbacks=(\d+)\s+matches=(\d+)\s+zero_len=(\d+)\s+queue_drop=(\d+)',
    re.IGNORECASE,
)
TX_DIAG_REGEX = re.compile(r'tx(\d+)=(\d+)', re.IGNORECASE)

# Must match firmware SEND_INTERVAL_MS.  Frames arriving within the same window
# share a group_id so a pandas groupby('group_id') gives you synchronized sets.
GROUP_WINDOW_MS = 50

app = Flask(__name__)


class ESPNOWDataLogger:
    def __init__(self, port, baud_rate=115200):
        self.port = port
        self.baud_rate = baud_rate
        self.serial_conn = None
        self.csv_writer = None
        self.csv_file = None
        self.logging_thread = None
        self.is_running = False
        self.packet_count = 0
        self.session_start_time = None
        self.session_id = str(uuid.uuid4())[:8]
        self.session_dir = f"sessions/session-{self.session_id}"
        os.makedirs(self.session_dir, exist_ok=True)

        self.recent_data = deque(maxlen=100)
        self.latest_by_tx = {}          # {tx_id: display_data_dict}
        self.plot_data_by_tx = {}       # {tx_id: deque(maxlen=200)}
        self.available_subcarriers = set()
        self.raw_lines = deque(maxlen=50)
        self.raw_line_count = 0
        self.csi_packet_count = 0
        self.espnow_packet_count = 0
        self.last_packet_time = None
        self.last_csi_time = None
        self.last_packet_type = None
        self.last_raw_line = None
        self.last_raw_line_time = None
        self.csi_diag = {}
        self.tx_stats = {}              # {tx_id: {packet_count, last_rssi, last_time}}
        self.pending_seq_by_tx = {}     # {tx_id: seq}
        self.pending_seq_time_by_tx = {}

    # ── serial ────────────────────────────────────────────────────────────────

    def connect(self):
        try:
            self.serial_conn = serial.Serial(self.port, self.baud_rate, timeout=1)
            print(f"Connected to ESP32 on {self.port}")
            return True
        except serial.SerialException as e:
            print(f"Failed to connect: {e}")
            return False

    # ── CSV ───────────────────────────────────────────────────────────────────

    def setup_csv_file(self):
        try:
            self._close_csv_file()
            timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"csi_data_{timestamp}.csv"
            filepath = os.path.join(self.session_dir, filename)
            os.makedirs(self.session_dir, exist_ok=True)
            self.csv_file = open(filepath, 'w', newline='', encoding='utf-8')

            # group_id ties together frames from different TXs in the same time window.
            # tx_id identifies which transmitter sent the frame.
            # Use pandas: df.groupby('group_id') to get synchronized TX pairs/sets.
            fieldnames = [
                'timestamp', 'group_id', 'tx_id',
                'packet_type', 'rssi', 'rate', 'channel', 'bandwidth',
                'data_length', 'esp_timestamp', 'csi_data', 'seq',
            ]
            self.csv_writer = csv.DictWriter(self.csv_file, fieldnames=fieldnames)
            self.csv_writer.writeheader()
            self.csv_file.flush()
            return filepath
        except Exception as e:
            print(f"[ERROR] Failed to setup CSV file: {e}")
            import traceback
            traceback.print_exc()
            raise

    def _close_csv_file(self):
        if self.csv_file:
            self.csv_file.close()
            self.csv_file = None
        self.csv_writer = None

    # ── parsers ───────────────────────────────────────────────────────────────

    def parse_csi_line(self, line):
        match = CSI_REGEX.search(line)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            print(f"[ERROR] CSI JSON parse failed: {exc} | line={line}")
            return None

    def parse_espnow_line(self, line):
        match = ESPNOW_REGEX.search(line)
        if match:
            try:
                return {
                    'seq': int(match.group(1)),
                    'tx_id': 0,
                    'rssi': int(match.group(2)),
                    'esp_timestamp': int(match.group(3)),
                }
            except ValueError as e:
                print(f"[ERROR] ESP-NOW parse failed: {e} | line={line}")
                return None

        match = ESPNOW_LOG_REGEX.search(line)
        if match:
            try:
                return {
                    'seq': int(match.group(1)),
                    'tx_id': int(match.group(2)) if match.group(2) else 0,
                    'rssi': int(match.group(3)),
                    'esp_timestamp': 0,
                }
            except ValueError as e:
                print(f"[ERROR] ESP-NOW log parse failed: {e} | line={line}")
                return None

        return None

    def parse_csi_diag_line(self, line):
        match = CSI_DIAG_REGEX.search(line)
        if not match:
            return None
        try:
            result = {
                'callbacks': int(match.group(1)),
                'matches': int(match.group(2)),
                'zero_len': int(match.group(3)),
                'queue_drop': int(match.group(4)),
                'tx_counts': {},
            }
            for tx_match in TX_DIAG_REGEX.finditer(line):
                result['tx_counts'][tx_match.group(1)] = int(tx_match.group(2))
            return result
        except ValueError as e:
            print(f"[ERROR] CSI diag parse failed: {e} | line={line}")
            return None

    # ── packet processing ─────────────────────────────────────────────────────

    def _process_packet(self, packet, packet_type='unknown'):
        python_timestamp = datetime.datetime.now().isoformat()

        tx_id = int(packet.get('tx_id', 0))

        # Use ESP32 hardware timestamp (µs) so both TXs get the same group_id
        # regardless of when Python dequeues them from the serial buffer.
        esp_ts_us = int(packet.get('esp_timestamp', 0))
        group_id = int((esp_ts_us / 1000) / GROUP_WINDOW_MS)

        csi_data = packet.get('csi_data', [])
        if isinstance(csi_data, str):
            try:
                csi_data = json.loads(csi_data)
            except Exception:
                csi_data = []

        data_length = packet.get('data_length', len(csi_data) if isinstance(csi_data, list) else 0)

        # seq correlation per TX
        seq = packet.get('seq', '')
        if packet_type == 'espnow':
            if tx_id:
                self.pending_seq_by_tx[tx_id] = seq
                self.pending_seq_time_by_tx[tx_id] = time.time()
            self.espnow_packet_count += 1
        elif packet_type == 'csi':
            if not seq and tx_id and tx_id in self.pending_seq_by_tx:
                if time.time() - self.pending_seq_time_by_tx.get(tx_id, 0) <= 1.0:
                    seq = self.pending_seq_by_tx[tx_id]
            self.csi_packet_count += 1
            self.last_csi_time = time.time()

        # per-TX stats (works for any tx_id value, no hardcoding)
        if tx_id not in self.tx_stats:
            self.tx_stats[tx_id] = {'packet_count': 0, 'last_rssi': None, 'last_time': None}
        self.tx_stats[tx_id]['packet_count'] += 1
        self.tx_stats[tx_id]['last_rssi'] = packet.get('rssi', 0)
        self.tx_stats[tx_id]['last_time'] = python_timestamp

        row = {
            'timestamp': python_timestamp,
            'group_id': group_id,
            'tx_id': tx_id,
            'packet_type': packet_type,
            'rssi': packet.get('rssi', 0),
            'rate': packet.get('rate', 0),
            'channel': packet.get('channel', 0),
            'bandwidth': packet.get('bandwidth', 0),
            'data_length': data_length,
            'esp_timestamp': packet.get('esp_timestamp', 0),
            'csi_data': json.dumps(csi_data) if csi_data else '[]',
            'seq': seq,
        }

        should_write_csv = packet_type == 'csi' and data_length > 0
        if self.csv_writer and should_write_csv:
            try:
                self.csv_writer.writerow(row)
                self.csv_file.flush()
            except Exception as exc:
                print(f"[ERROR] Failed to write CSV row: {exc}")

        self.packet_count += 1
        self.last_packet_time = time.time()
        self.last_packet_type = packet_type

        elapsed = time.time() - self.session_start_time if self.session_start_time else 0
        display_data = {
            'packet_num': self.packet_count,
            'timestamp': python_timestamp,
            'tx_id': tx_id,
            'group_id': group_id,
            'rssi': packet.get('rssi', 0),
            'rate': packet.get('rate', 0),
            'channel': packet.get('channel', 0),
            'bandwidth': packet.get('bandwidth', 0),
            'data_length': data_length,
            'esp_timestamp': packet.get('esp_timestamp', 0),
            'seq': seq,
            'packet_type': packet_type,
            'time_passed': elapsed,
        }

        if isinstance(csi_data, list):
            for idx, value in enumerate(csi_data):
                display_data[f'subcarrier_{idx}'] = value
                self.available_subcarriers.add(idx)

        self.recent_data.append(display_data)
        self.latest_by_tx[tx_id] = display_data

        if tx_id not in self.plot_data_by_tx:
            self.plot_data_by_tx[tx_id] = deque(maxlen=200)
        self.plot_data_by_tx[tx_id].append({
            'time': elapsed,
            'rssi': packet.get('rssi', 0),
            'subcarriers': csi_data if isinstance(csi_data, list) else [],
        })

    # ── logging loop ──────────────────────────────────────────────────────────

    def _log_loop(self):
        try:
            while self.is_running:
                if self.serial_conn and self.serial_conn.in_waiting > 0:
                    try:
                        line = self.serial_conn.readline().decode('utf-8', errors='ignore').strip()
                        if line:
                            self.raw_lines.append(line)
                            self.raw_line_count += 1
                            self.last_raw_line = line
                            self.last_raw_line_time = time.time()

                            packet = self.parse_csi_line(line)
                            packet_type = 'csi'
                            if packet is None:
                                packet = self.parse_espnow_line(line)
                                packet_type = 'espnow'

                            if packet is not None:
                                self._process_packet(packet, packet_type=packet_type)
                            else:
                                diag = self.parse_csi_diag_line(line)
                                if diag is not None:
                                    self.csi_diag = diag
                    except Exception as e:
                        print(f"Error processing serial line: {e}")
                        import traceback
                        traceback.print_exc()
                time.sleep(0.01)
        except Exception as e:
            print(f"Logging error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.is_running = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start_logging(self):
        if not self.serial_conn:
            print("Not connected to ESP32")
            return False
        if self.is_running:
            print("Already logging")
            return False
        self.is_running = True
        self.session_start_time = time.time()
        self.csv_filename = self.setup_csv_file()
        self.logging_thread = threading.Thread(target=self._log_loop)
        self.logging_thread.daemon = True
        self.logging_thread.start()
        return True

    def stop_logging(self):
        self.is_running = False
        if self.logging_thread is not None:
            self.logging_thread.join(timeout=1)
            self.logging_thread = None
        self._close_csv_file()

    def close(self):
        self.stop_logging()
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
        self.serial_conn = None

    # ── data accessors ────────────────────────────────────────────────────────

    def get_status(self):
        now = time.time()
        csi_age = round(now - self.last_csi_time, 2) if self.last_csi_time else None
        raw_age = round(now - self.last_raw_line_time, 2) if self.last_raw_line_time else None

        return {
            'connected': self.serial_conn is not None and self.serial_conn.is_open,
            'logging': self.is_running,
            'packet_count': self.packet_count,
            'raw_line_count': self.raw_line_count,
            'csi_packet_count': self.csi_packet_count,
            'espnow_packet_count': self.espnow_packet_count,
            'last_packet_type': self.last_packet_type,
            'serial_receiving': self.last_raw_line_time is not None and (now - self.last_raw_line_time) <= 3,
            'last_raw_seconds_ago': raw_age,
            'last_raw_line': self.last_raw_line,
            'csi_receiving': self.last_csi_time is not None and (now - self.last_csi_time) <= 3,
            'last_csi_seconds_ago': csi_age,
            'csi_diag': self.csi_diag,
            'tx_stats': {str(k): v for k, v in self.tx_stats.items()},
            'observed_tx_ids': sorted(str(k) for k in self.tx_stats),
            'port': self.port,
            'session_id': self.session_id,
            'session_dir': self.session_dir,
        }

    def get_recent_data(self):
        return list(self.recent_data)

    def get_latest_by_tx(self):
        return {str(k): v for k, v in self.latest_by_tx.items()}

    def get_available_subcarriers(self):
        return sorted(self.available_subcarriers)

    def get_raw_lines(self):
        return list(self.raw_lines)

    def get_plot_data(self, selected_subcarriers=None, tx_filter=None):
        """
        Returns {x, y} point arrays so Chart.js can plot multiple TX series
        on a common time axis without alignment gymnastics.

        tx_filter: None = show all TXs, int = show only that TX.
        """
        if selected_subcarriers is None:
            selected_subcarriers = [1, 5, 9, 13]
        selected_subcarriers = sorted(set(int(x) for x in selected_subcarriers))

        tx_ids = sorted(self.plot_data_by_tx.keys())
        if tx_filter is not None:
            tx_ids = [t for t in tx_ids if t == int(tx_filter)]

        rssi_by_tx = {}
        subcarriers = {}

        for tx_id in tx_ids:
            tx_key = str(tx_id)
            rssi_by_tx[tx_key] = []

            for point in list(self.plot_data_by_tx[tx_id])[-200:]:
                t = round(point['time'], 3)
                rssi_by_tx[tx_key].append({'x': t, 'y': point['rssi']})

                for idx in selected_subcarriers:
                    values = point.get('subcarriers', [])
                    if idx < len(values):
                        sub_key = f'TX{tx_id} sub{idx}'
                        if sub_key not in subcarriers:
                            subcarriers[sub_key] = []
                        subcarriers[sub_key].append({'x': t, 'y': values[idx]})

        return {'rssi_by_tx': rssi_by_tx, 'subcarriers': subcarriers}


# ── global logger ─────────────────────────────────────────────────────────────

logger = None


def cleanup_logger():
    global logger
    if logger:
        logger.close()
        logger = None


def _handle_shutdown(signum, frame):
    cleanup_logger()
    raise SystemExit(0)


atexit.register(cleanup_logger)
signal.signal(signal.SIGINT, _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)

# ── HTML/JS ───────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return '''
<!DOCTYPE html>
<html>
<head>
    <title>ESP32 CSI Data Monitor</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.9.1/chart.min.js"></script>
    <style>
        :root {
            --primary: #36311F;
            --secondary: #59544B;
            --accent: #79A9D1;
            --success: #7D8CA3;
            --light-bg: #F5F6F8;
            --dark-text: #36311F;
            --white: #ffffff;
        }
        body {
            font-family: 'IBM Plex Mono', monospace;
            margin: 0; padding: 20px;
            background: var(--light-bg);
            color: var(--dark-text);
        }
        .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
        h1 {
            color: var(--primary); font-size: 2em; text-align: center;
            font-weight: 700; text-transform: uppercase; letter-spacing: 2px;
            margin-bottom: 1.5em;
        }
        h3 {
            color: var(--primary); font-size: 1.2em; font-weight: 600;
            text-transform: uppercase; letter-spacing: 1px; margin-bottom: 1em;
        }
        .card {
            background: var(--white); padding: 25px; margin: 20px 0;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            border-left: 4px solid var(--accent);
        }
        .status { display: flex; gap: 15px; align-items: center; flex-wrap: wrap; margin-bottom: 20px; }
        .status-item {
            padding: 10px 18px; font-weight: 600; font-size: 0.9em;
            text-transform: uppercase; letter-spacing: 1px; color: var(--white);
        }
        .connected    { background: var(--accent); }
        .disconnected { background: var(--primary); }
        .logging      { background: var(--success); }
        .stopped      { background: var(--secondary); }
        .csi-live     { background: #2d6a4f; }
        .csi-waiting  { background: #b08968; }
        .serial-live  { background: #457b9d; }
        .serial-waiting { background: #8d99ae; }
        button {
            padding: 10px 20px; margin: 4px; border: none; cursor: pointer;
            font-size: 0.9em; font-weight: 600; text-transform: uppercase;
            letter-spacing: 1px; font-family: inherit;
            box-shadow: 0 2px 4px rgba(0,0,0,0.2);
        }
        .btn-primary { background: var(--accent);   color: var(--white); }
        .btn-success { background: var(--success);  color: var(--white); }
        .btn-danger  { background: var(--primary);  color: var(--white); }
        .btn-warning { background: var(--secondary);color: var(--white); }
        input[type="text"], select {
            padding: 9px 14px; border: 2px solid var(--accent);
            font-size: 0.9em; font-family: inherit;
        }
        /* TX cards inside "Latest CSI Data" */
        .tx-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }
        .tx-card {
            border-left: 5px solid var(--accent);
            background: var(--light-bg); padding: 16px;
        }
        .tx-card.tx-1 { border-left-color: rgb(54,162,235); }
        .tx-card.tx-2 { border-left-color: rgb(255,99,132); }
        .tx-card.tx-3 { border-left-color: rgb(75,192,192); }
        .tx-card.tx-4 { border-left-color: rgb(255,205,86); }
        .tx-badge {
            display: inline-block; padding: 3px 10px; color: var(--white);
            font-weight: 700; font-size: 0.85em; margin-bottom: 10px;
        }
        .tx-card.tx-1 .tx-badge { background: rgb(54,162,235); }
        .tx-card.tx-2 .tx-badge { background: rgb(255,99,132); }
        .tx-card.tx-3 .tx-badge { background: rgb(75,192,192); }
        .tx-card.tx-4 .tx-badge { background: rgb(255,205,86); }
        .tx-fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 8px; margin-top: 8px; }
        .data-item {
            background: var(--white); padding: 10px 12px;
            font-size: 0.88em; border-left: 3px solid var(--accent);
        }
        /* diagnostics */
        .diag-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }
        /* plots */
        .plots-container { display: grid; grid-template-columns: 1fr 1fr; gap: 30px; }
        .chart-container {
            height: 380px; background: var(--white); padding: 16px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1); border-left: 4px solid var(--accent);
        }
        .plot-controls {
            display: flex; gap: 16px; align-items: center; flex-wrap: wrap;
            background: var(--light-bg); padding: 14px; margin-bottom: 16px;
            border-left: 4px solid var(--accent);
        }
        .control-group { display: flex; align-items: center; gap: 8px; }
        .subcarrier-browser {
            width: 100%; max-height: 180px; overflow-y: auto;
            background: var(--white); border: 1px solid #d8dee6;
            padding: 12px; display: flex; flex-wrap: wrap; gap: 7px;
        }
        .subcarrier-chip {
            border: 1px solid var(--accent); background: var(--white);
            color: var(--dark-text); padding: 6px 9px; font-size: 0.82em;
            cursor: pointer; user-select: none; font-family: inherit;
        }
        .subcarrier-chip.pending  { background: #f4d58d; border-color: #d4a373; }
        .subcarrier-chip.selected { background: var(--accent); color: var(--white); }
        .subcarrier-status { width: 100%; font-size: 0.88em; color: var(--secondary); }
        #data-log {
            height: 260px; overflow-y: scroll; border: 1px solid #ddd;
            padding: 12px; background: var(--light-bg); font-size: 0.88em;
        }
        .log-entry { margin-bottom: 4px; }
        .log-tx-badge {
            display: inline-block; padding: 1px 6px; color: var(--white);
            font-weight: 700; font-size: 0.8em; margin-right: 5px;
        }
        .log-tx-1 { background: rgb(54,162,235); }
        .log-tx-2 { background: rgb(255,99,132); }
        .log-tx-3 { background: rgb(75,192,192); }
        @media (max-width: 1100px) {
            .plots-container { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
<div class="container">
    <h1>ESP32 Real-Time CSI Monitor</h1>

    <!-- Connection -->
    <div class="card">
        <h3>Connection</h3>
        <div class="status" id="status">
            <div class="status-item disconnected">Disconnected</div>
            <div class="status-item stopped">Not Logging</div>
            <div class="status-item serial-waiting">Serial Waiting</div>
            <div class="status-item csi-waiting">CSI Waiting</div>
        </div>
        <div>
            <input type="text" id="port-input" placeholder="COM9 (slave/RX)" style="width:200px">
            <button class="btn-primary"  onclick="connect()">Connect</button>
            <button class="btn-danger"   onclick="disconnect()">Disconnect</button>
            <button class="btn-success"  onclick="startLogging()">Start Logging</button>
            <button class="btn-warning"  onclick="stopLogging()">Stop Logging</button>
        </div>
    </div>

    <!-- Latest per-TX -->
    <div class="card">
        <h3>Latest CSI Data (per TX)</h3>
        <div class="tx-grid" id="latest-data">
            <div class="data-item">Waiting for CSI data...</div>
        </div>
    </div>

    <!-- Diagnostics -->
    <div class="card">
        <h3>Diagnostics</h3>
        <div class="diag-grid" id="serial-diagnostics">
            <div class="data-item">Waiting...</div>
        </div>
    </div>

    <!-- Plots -->
    <div class="card">
        <h3>Real-time Plots</h3>
        <div class="plot-controls">
            <div class="control-group">
                <label for="tx-filter"><strong>CSI TX:</strong></label>
                <select id="tx-filter" onchange="updateCharts()">
                    <option value="">All TXs</option>
                </select>
            </div>
            <div class="control-group">
                <label><strong>Subcarriers:</strong></label>
                <button class="btn-primary" onclick="addPendingSubcarriers()">Add</button>
                <button class="btn-primary" onclick="refreshSubcarriers()">Refresh</button>
            </div>
            <div id="subcarrier-status" class="subcarrier-status">Click subcarriers to stage them, then press Add.</div>
            <div id="subcarrier-browser" class="subcarrier-browser">
                <div>Waiting for discovered subcarriers...</div>
            </div>
        </div>
        <div class="plots-container">
            <div class="chart-container"><canvas id="rssiChart"></canvas></div>
            <div class="chart-container"><canvas id="csiChart"></canvas></div>
        </div>
    </div>

    <!-- Log -->
    <div class="card">
        <h3>Data Log</h3>
        <div id="data-log"></div>
    </div>
</div>

<script>
// ── TX colour palette (extensible: index = tx_id - 1) ─────────────────────────
const TX_PALETTE = [
    { border: 'rgb(54,162,235)',  bg: 'rgba(54,162,235,0.2)'  },  // TX1 blue
    { border: 'rgb(255,99,132)', bg: 'rgba(255,99,132,0.2)'  },  // TX2 red
    { border: 'rgb(75,192,192)', bg: 'rgba(75,192,192,0.2)'  },  // TX3 teal
    { border: 'rgb(255,205,86)', bg: 'rgba(255,205,86,0.2)'  },  // TX4 yellow
    { border: 'rgb(153,102,255)',bg: 'rgba(153,102,255,0.2)' },
    { border: 'rgb(255,159,64)', bg: 'rgba(255,159,64,0.2)'  },
];
function txColor(txId) {
    return TX_PALETTE[(parseInt(txId, 10) - 1) % TX_PALETTE.length] || TX_PALETTE[0];
}

// CSI subcarrier colour palette (cycles independently of TX)
const SUB_PALETTE = [
    'rgb(54,162,235)', 'rgb(255,99,132)', 'rgb(75,192,192)',
    'rgb(255,205,86)', 'rgb(153,102,255)', 'rgb(255,159,64)',
    'rgb(201,203,207)', 'rgb(100,221,23)', 'rgb(0,188,212)',
];

// ── chart setup (linear x = seconds since session start) ─────────────────────
const CHART_OPTS = (title, yLabel) => ({
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 0 },
    plugins: {
        title: { display: true, text: title },
        legend: { position: 'top' },
    },
    scales: {
        x: { type: 'linear', title: { display: true, text: 'Time (s)' } },
        y: { title: { display: true, text: yLabel } },
    },
});

const rssiChart = new Chart(document.getElementById('rssiChart'), {
    type: 'line',
    data: { datasets: [] },
    options: CHART_OPTS('RSSI over Time (per TX)', 'RSSI (dBm)'),
});

const hiddenCsiSeries = new Set();
const csiChart = new Chart(document.getElementById('csiChart'), {
    type: 'line',
    data: { datasets: [] },
    options: {
        ...CHART_OPTS('CSI Subcarriers', 'CSI Value'),
        plugins: {
            ...CHART_OPTS('CSI Subcarriers', 'CSI Value').plugins,
            legend: {
                onClick: (e, item, legend) => {
                    const ds = legend.chart.data.datasets[item.datasetIndex];
                    if (!ds) return;
                    if (hiddenCsiSeries.has(ds.label)) {
                        hiddenCsiSeries.delete(ds.label);
                        ds.hidden = false;
                    } else {
                        hiddenCsiSeries.add(ds.label);
                        ds.hidden = true;
                    }
                    legend.chart.update();
                },
            },
        },
    },
});

// ── subcarrier browser ────────────────────────────────────────────────────────
const customSubcarriers = new Set();
const pendingSubcarriers = new Set();

function renderSubcarrierStatus() {
    const el = document.getElementById('subcarrier-status');
    if (!el) return;
    el.textContent = pendingSubcarriers.size === 0
        ? 'Click subcarriers to stage them, then press Add.'
        : `Staged: ${[...pendingSubcarriers].sort((a,b)=>a-b).join(', ')}`;
}

function renderSubcarrierBrowser(all = []) {
    const el = document.getElementById('subcarrier-browser');
    if (!el) return;
    if (!all.length) { el.innerHTML = '<div>Waiting for discovered subcarriers...</div>'; return; }
    el.innerHTML = '';
    all.forEach(v => {
        const chip = document.createElement('button');
        chip.type = 'button';
        const isPending  = pendingSubcarriers.has(v);
        const isSelected = customSubcarriers.has(v);
        chip.className = `subcarrier-chip${isPending ? ' pending' : ''}${isSelected ? ' selected' : ''}`;
        chip.textContent = `SC${v}`;
        chip.onclick = () => {
            if (isSelected) {
                customSubcarriers.delete(v);
                hiddenCsiSeries.delete(`subcarrier_${v}`);
                updateCharts();
            } else if (isPending) {
                pendingSubcarriers.delete(v);
            } else {
                pendingSubcarriers.add(v);
            }
            renderSubcarrierStatus();
            renderSubcarrierBrowser(all);
        };
        el.appendChild(chip);
    });
}

function addPendingSubcarriers() {
    if (!pendingSubcarriers.size) return;
    pendingSubcarriers.forEach(v => customSubcarriers.add(v));
    pendingSubcarriers.clear();
    renderSubcarrierStatus();
    refreshSubcarriers();
}

function getSelectedSubcarriers() {
    const sel = [...customSubcarriers].sort((a,b)=>a-b);
    return sel.length ? sel : [1,5,9,13];
}

function refreshSubcarriers() {
    fetch('/api/subcarriers')
        .then(r => r.json())
        .then(data => {
            const merged = [...new Set([...data, ...customSubcarriers, ...pendingSubcarriers])].sort((a,b)=>a-b);
            if (!customSubcarriers.size) {
                [1,5,9,13].forEach(i => { if (merged.includes(i)) customSubcarriers.add(i); });
            }
            renderSubcarrierStatus();
            renderSubcarrierBrowser(merged);
            updateCharts();
        })
        .catch(e => console.error('subcarriers:', e));
}

// ── chart update ──────────────────────────────────────────────────────────────
function updateCharts() {
    const selected = getSelectedSubcarriers();
    const txFilter = document.getElementById('tx-filter').value;
    const params = `subcarriers=${selected.join(',')}&tx=${txFilter}`;

    fetch('/api/plot_data?' + params)
        .then(r => r.json())
        .then(data => {
            // RSSI chart: one dataset per TX, {x,y} points
            rssiChart.data.datasets = Object.entries(data.rssi_by_tx || {}).map(([txId, pts]) => {
                const c = txColor(txId);
                return {
                    label: `TX${txId} RSSI`,
                    data: pts,
                    borderColor: c.border,
                    backgroundColor: c.bg,
                    tension: 0.1,
                    pointRadius: 0,
                    pointHoverRadius: 3,
                    borderWidth: 2,
                };
            });
            rssiChart.update('none');

            // CSI chart: one dataset per "TX{N} sub{idx}" key, {x,y} points
            csiChart.data.datasets = Object.entries(data.subcarriers || {}).map(([key, pts], i) => {
                const color = SUB_PALETTE[i % SUB_PALETTE.length];
                return {
                    label: key,
                    data: pts,
                    borderColor: color,
                    backgroundColor: color.replace('rgb', 'rgba').replace(')', ',0.2)'),
                    tension: 0.1,
                    pointRadius: 0,
                    pointHoverRadius: 3,
                    borderWidth: 2,
                    hidden: hiddenCsiSeries.has(key),
                };
            });
            csiChart.update('none');
        })
        .catch(e => console.error('plot_data:', e));
}

// ── TX filter dropdown ────────────────────────────────────────────────────────
let knownTxIds = new Set();

function syncTxFilter(observedIds) {
    const sel = document.getElementById('tx-filter');
    const current = sel.value;
    const incoming = new Set(observedIds);
    let changed = false;
    incoming.forEach(id => {
        if (!knownTxIds.has(id)) {
            knownTxIds.add(id);
            const opt = document.createElement('option');
            opt.value = id;
            opt.textContent = `TX${id} only`;
            sel.appendChild(opt);
            changed = true;
        }
    });
    if (changed && current !== '') sel.value = current;
}

// ── status ────────────────────────────────────────────────────────────────────
function updateStatus() {
    fetch('/api/status')
        .then(r => r.json())
        .then(data => {
            const connClass   = data.connected        ? 'connected'     : 'disconnected';
            const connText    = data.connected        ? 'Connected'     : 'Disconnected';
            const logClass    = data.logging          ? 'logging'       : 'stopped';
            const logText     = data.logging          ? 'Logging'       : 'Not Logging';
            const serClass    = data.serial_receiving ? 'serial-live'   : 'serial-waiting';
            const serText     = data.serial_receiving ? 'Serial Live'   : 'Serial Waiting';
            const serAge      = data.last_raw_seconds_ago == null ? '—' : `${data.last_raw_seconds_ago}s ago`;
            const csiClass    = data.csi_receiving    ? 'csi-live'      : 'csi-waiting';
            const csiText     = data.csi_receiving    ? 'CSI Live'      : 'CSI Waiting';
            const csiAge      = data.last_csi_seconds_ago == null ? '—' : `${data.last_csi_seconds_ago}s ago`;

            document.getElementById('status').innerHTML = `
                <div class="status-item ${connClass}">${connText} ${data.port ? '('+data.port+')' : ''}</div>
                <div class="status-item ${logClass}">${logText}</div>
                <div class="status-item ${serClass}">${serText}</div>
                <div class="status-item ${csiClass}">${csiText}</div>
                <div>Packets: ${data.packet_count}</div>
                <div>Raw: ${data.raw_line_count||0}</div>
                <div>Last serial: ${serAge}</div>
                <div>CSI: ${data.csi_packet_count||0}</div>
                <div>Last CSI: ${csiAge}</div>
                <div>Session: ${data.session_id||'—'}</div>
            `;

            // populate TX filter dropdown with newly observed TXs
            syncTxFilter(data.observed_tx_ids || []);

            // diagnostics panel
            const diag = data.csi_diag || {};
            const txStats = data.tx_stats || {};
            const txCountsHtml = Object.entries(txStats).map(([id, s]) =>
                `<div class="data-item"><strong>TX${id} frames:</strong> ${s.packet_count} | last RSSI: ${s.last_rssi ?? '—'} dBm</div>`
            ).join('');
            const diagCounts = (diag.tx_counts && Object.keys(diag.tx_counts).length)
                ? Object.entries(diag.tx_counts).map(([id, n]) =>
                    `<div class="data-item"><strong>FW TX${id} count:</strong> ${n}</div>`).join('')
                : '';

            document.getElementById('serial-diagnostics').innerHTML = `
                <div class="data-item"><strong>Raw Lines:</strong> ${data.raw_line_count||0}</div>
                <div class="data-item"><strong>Serial:</strong> ${serText}</div>
                <div class="data-item"><strong>CSI Callbacks:</strong> ${diag.callbacks??0}</div>
                <div class="data-item"><strong>CSI Matches:</strong> ${diag.matches??0}</div>
                <div class="data-item"><strong>Zero Len:</strong> ${diag.zero_len??0}</div>
                <div class="data-item"><strong>Queue Drop:</strong> ${diag.queue_drop??0}</div>
                ${txCountsHtml}
                ${diagCounts}
            `;
        });
}

// ── latest data per TX ────────────────────────────────────────────────────────
function fmtTime(s) {
    if (s == null) return '—';
    if (s < 60) return `${s.toFixed(1)}s`;
    if (s < 3600) return `${Math.floor(s/60)}m ${Math.floor(s%60)}s`;
    return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m`;
}
function fmtSC(v) { return v == null ? '—' : Number(v).toFixed(2); }

function updateLatestData() {
    fetch('/api/latest_by_tx')
        .then(r => r.json())
        .then(byTx => {
            const div = document.getElementById('latest-data');
            const entries = Object.entries(byTx);
            if (!entries.length) {
                div.innerHTML = '<div class="data-item">Waiting for CSI data...</div>';
                return;
            }
            div.innerHTML = entries.sort(([a],[b])=>parseInt(a)-parseInt(b)).map(([txId, d]) => `
                <div class="tx-card tx-${txId}">
                    <span class="tx-badge">TX${txId}</span>
                    group&nbsp;#${d.group_id}&nbsp;&nbsp;pkt&nbsp;#${d.packet_num}
                    <div class="tx-fields">
                        <div class="data-item"><strong>RSSI:</strong> ${d.rssi} dBm</div>
                        <div class="data-item"><strong>Rate:</strong> ${d.rate}</div>
                        <div class="data-item"><strong>Chan:</strong> ${d.channel} / BW:${d.bandwidth}</div>
                        <div class="data-item"><strong>Len:</strong> ${d.data_length}</div>
                        <div class="data-item"><strong>Elapsed:</strong> ${fmtTime(d.time_passed)}</div>
                        <div class="data-item"><strong>SC1:</strong> ${fmtSC(d.subcarrier_1)}</div>
                        <div class="data-item"><strong>SC5:</strong> ${fmtSC(d.subcarrier_5)}</div>
                        <div class="data-item"><strong>SC9:</strong> ${fmtSC(d.subcarrier_9)}</div>
                        <div class="data-item"><strong>SC13:</strong> ${fmtSC(d.subcarrier_13)}</div>
                    </div>
                </div>
            `).join('');
        });
}

// ── data log ──────────────────────────────────────────────────────────────────
function updateDataLog() {
    fetch('/api/recent')
        .then(r => r.json())
        .then(data => {
            const div = document.getElementById('data-log');
            if (!data.length) { div.innerHTML = '<div>Waiting for CSI data...</div>'; return; }
            div.innerHTML = data.slice(-15).reverse().map(p => {
                const txId = p.tx_id || 0;
                const badgeClass = txId ? `log-tx-${txId}` : '';
                const badge = txId ? `<span class="log-tx-badge ${badgeClass}">TX${txId}</span>` : '';
                return `<div class="log-entry">${badge}grp&nbsp;${p.group_id}&nbsp;pkt&nbsp;#${p.packet_num}&nbsp;SC1=${fmtSC(p.subcarrier_1)}&nbsp;RSSI=${p.rssi}&nbsp;t=${fmtTime(p.time_passed)}</div>`;
            }).join('');
            div.scrollTop = 0;
        });
}

// ── control ───────────────────────────────────────────────────────────────────
function connect() {
    const port = document.getElementById('port-input').value || 'COM9';
    fetch('/api/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ port }),
    }).then(r => r.json()).then(() => updateStatus());
}
function disconnect()   { fetch('/api/disconnect', { method: 'POST' }); }
function startLogging() { fetch('/api/start',      { method: 'POST' }); }
function stopLogging()  { fetch('/api/stop',       { method: 'POST' }); }

// ── polling ───────────────────────────────────────────────────────────────────
refreshSubcarriers();
setInterval(() => {
    updateStatus();
    updateLatestData();
    updateDataLog();
    updateCharts();
}, 1000);
updateStatus();
</script>
</body>
</html>
'''

# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route('/api/status')
def api_status():
    if logger:
        return jsonify(logger.get_status())
    return jsonify({'connected': False, 'logging': False, 'packet_count': 0,
                    'port': '', 'session_id': None, 'session_dir': None,
                    'tx_stats': {}, 'observed_tx_ids': []})

@app.route('/api/latest')
def api_latest():
    if logger:
        by_tx = logger.get_latest_by_tx()
        return jsonify(next(iter(by_tx.values()), {}))
    return jsonify({})

@app.route('/api/latest_by_tx')
def api_latest_by_tx():
    if logger:
        return jsonify(logger.get_latest_by_tx())
    return jsonify({})

@app.route('/api/recent')
def api_recent():
    if logger:
        return jsonify(logger.get_recent_data())
    return jsonify([])

@app.route('/api/subcarriers')
def api_subcarriers():
    if logger:
        return jsonify(logger.get_available_subcarriers())
    return jsonify([])

@app.route('/api/plot_data')
def api_plot_data():
    if logger:
        sub_str = request.args.get('subcarriers', '')
        tx_str  = request.args.get('tx', '').strip()
        selected = [int(x) for x in sub_str.split(',') if x.strip().isdigit()]
        tx_filter = int(tx_str) if tx_str.isdigit() else None
        return jsonify(logger.get_plot_data(
            selected_subcarriers=selected or None,
            tx_filter=tx_filter,
        ))
    return jsonify({'rssi_by_tx': {}, 'subcarriers': {}})

@app.route('/api/raw')
def api_raw():
    if logger:
        return jsonify(logger.get_raw_lines())
    return jsonify([])

@app.route('/api/connect', methods=['POST'])
def api_connect():
    global logger
    data = request.get_json()
    port = data.get('port', 'COM9')
    print(f"\n{'='*60}\n[API] connect port={port}\n{'='*60}\n")
    if logger:
        cleanup_logger()
        time.sleep(1)
    logger = ESPNOWDataLogger(port)
    success = logger.connect()
    started = False
    if success:
        try:
            started = logger.start_logging()
        except Exception as e:
            print(f"[API] start_logging error: {e}")
    return jsonify({'success': success, 'port': port, 'logging': started})

@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    global logger
    cleanup_logger()
    return jsonify({'success': True})

@app.route('/api/shutdown', methods=['POST'])
def api_shutdown():
    cleanup_logger()
    shutdown_func = request.environ.get('werkzeug.server.shutdown')
    if shutdown_func is None:
        return jsonify({'success': False, 'error': 'Server shutdown not available'})
    shutdown_func()
    return jsonify({'success': True})

@app.route('/api/start', methods=['POST'])
def api_start():
    if logger:
        return jsonify({'success': logger.start_logging()})
    return jsonify({'success': False, 'error': 'Not connected'})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    if logger:
        logger.stop_logging()
        return jsonify({'success': True})
    return jsonify({'success': False})

if __name__ == '__main__':
    try:
        app.run(debug=True, use_reloader=False, host='0.0.0.0', port=5000)
    finally:
        cleanup_logger()
