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
import math
import uuid

CSI_REGEX = re.compile(r'CSI_START(\{.+\})CSI_END')
ESPNOW_REGEX = re.compile(r'RX,SEQ=(\d+),RSSI=(-?\d+),TS=(\d+)')
ESPNOW_LOG_REGEX = re.compile(r'seq=(\d+)\s+rssi=(-?\d+)', re.IGNORECASE)
CSI_DIAG_REGEX = re.compile(r'callbacks=(\d+)\s+matches=(\d+)\s+zero_len=(\d+)\s+queue_drop=(\d+)', re.IGNORECASE)

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
        self.latest_packet = {}
        self.plot_data = deque(maxlen=200)
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
        self.pending_seq = None
        self.pending_espnow_timestamp = None

    def connect(self):
        try:
            self.serial_conn = serial.Serial(self.port, self.baud_rate, timeout=1)
            print(f"Connected to ESP32 on {self.port}")
            return True
        except serial.SerialException as e:
            print(f"Failed to connect: {e}")
            return False

    def setup_csv_file(self):
        try:
            self._close_csv_file()
            timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"csi_data_{timestamp}.csv"
            filepath = os.path.join(self.session_dir, filename)
            os.makedirs(self.session_dir, exist_ok=True)
            self.csv_file = open(filepath, 'w', newline='', encoding='utf-8')

            fieldnames = [
                'timestamp', 'packet_type', 'rssi', 'rate', 'channel', 'bandwidth',
                'data_length', 'esp_timestamp', 'csi_data', 'seq'
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

    def parse_csi_line(self, line):
        match = CSI_REGEX.search(line)
        if not match:
            return None

        try:
            payload = json.loads(match.group(1))
            return payload
        except json.JSONDecodeError as exc:
            print(f"[ERROR] CSI JSON parse failed: {exc} | line={line}")
            return None

    def parse_espnow_line(self, line):
        match = ESPNOW_REGEX.search(line)
        if match:
            try:
                return {
                    'seq': int(match.group(1)),
                    'rssi': int(match.group(2)),
                    'esp_timestamp': int(match.group(3))
                }
            except ValueError as e:
                print(f"[ERROR] ESP-NOW parse failed: {e} | line={line}")
                return None

        match = ESPNOW_LOG_REGEX.search(line)
        if match:
            try:
                return {
                    'seq': int(match.group(1)),
                    'rssi': int(match.group(2)),
                    'esp_timestamp': 0
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
            return {
                'callbacks': int(match.group(1)),
                'matches': int(match.group(2)),
                'zero_len': int(match.group(3)),
                'queue_drop': int(match.group(4)),
            }
        except ValueError as e:
            print(f"[ERROR] CSI diag parse failed: {e} | line={line}")
            return None

    def _process_packet(self, packet, packet_type='unknown'):
        python_timestamp = datetime.datetime.now().isoformat()
        csi_data = packet.get('csi_data', [])
        if isinstance(csi_data, str):
            try:
                csi_data = json.loads(csi_data)
            except Exception:
                csi_data = []

        data_length = packet.get('data_length', len(csi_data) if isinstance(csi_data, list) else 0)
        seq = packet.get('seq', '')
        if packet_type == 'espnow':
            self.pending_seq = packet.get('seq')
            self.pending_espnow_timestamp = time.time()
        elif packet_type == 'csi' and (seq == '' or seq is None or seq == 0):
            if self.pending_seq is not None and self.pending_espnow_timestamp is not None:
                if time.time() - self.pending_espnow_timestamp <= 1.0:
                    seq = self.pending_seq

        row = {
            'timestamp': python_timestamp,
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
        if packet_type == 'csi':
            self.csi_packet_count += 1
            self.last_csi_time = self.last_packet_time
        elif packet_type == 'espnow':
            self.espnow_packet_count += 1

        display_data = {
            'packet_num': self.packet_count,
            'timestamp': python_timestamp,
            'rssi': packet.get('rssi', 0),
            'rate': packet.get('rate', 0),
            'channel': packet.get('channel', 0),
            'bandwidth': packet.get('bandwidth', 0),
            'data_length': data_length,
            'esp_timestamp': packet.get('esp_timestamp', 0),
            'seq': seq,
            'packet_type': packet_type,
            'time_passed': time.time() - self.session_start_time if self.session_start_time else 0,
        }

        if isinstance(csi_data, list):
            for idx, value in enumerate(csi_data):
                display_data[f'subcarrier_{idx}'] = value
                self.available_subcarriers.add(idx)

        self.recent_data.append(display_data)
        self.latest_packet = display_data
        self.plot_data.append({
            'time': time.time() - (self.session_start_time or time.time()),
            'rssi': packet.get('rssi', 0),
            'subcarriers': csi_data if isinstance(csi_data, list) else []
        })

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

    def get_status(self):
        now = time.time()
        csi_age = None
        raw_age = None
        if self.last_csi_time is not None:
            csi_age = round(now - self.last_csi_time, 2)
        if self.last_raw_line_time is not None:
            raw_age = round(now - self.last_raw_line_time, 2)

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
            'port': self.port,
            'session_id': self.session_id,
            'session_dir': self.session_dir
        }

    def get_recent_data(self):
        return list(self.recent_data)

    def get_latest_packet(self):
        return self.latest_packet

    def get_available_subcarriers(self):
        return sorted(self.available_subcarriers)

    def get_raw_lines(self):
        return list(self.raw_lines)

    def get_plot_data(self, selected_subcarriers=None):
        if selected_subcarriers is None:
            selected_subcarriers = [1, 5, 9, 13]

        selected_subcarriers = [int(x) for x in selected_subcarriers if isinstance(x, int) or str(x).isdigit()]
        selected_subcarriers = sorted(set(selected_subcarriers))

        plot_data = {
            'time': [],
            'rssi': [],
            'subcarriers': {}
        }

        for idx in selected_subcarriers:
            plot_data['subcarriers'][f'subcarrier_{idx}'] = []

        for point in list(self.plot_data)[-200:]:
            plot_data['time'].append(round(point['time'], 3))
            plot_data['rssi'].append(point.get('rssi', 0))
            for idx in selected_subcarriers:
                values = point.get('subcarriers', [])
                if idx < len(values):
                    plot_data['subcarriers'][f'subcarrier_{idx}'].append(values[idx])
                else:
                    plot_data['subcarriers'][f'subcarrier_{idx}'].append(None)

        return plot_data

    def close(self):
        self.stop_logging()
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
        self.serial_conn = None
    
# Global logger instance
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

@app.route('/')
def home():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>ESP32 CSI Data Monitor with Configurable Plots</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.9.1/chart.min.js"></script>
        <style>
            :root {
                --primary-color: #36311F;  /* Dark brown */
                --secondary-color: #59544B;  /* Medium brown */
                --accent-color: #79A9D1;  /* Light blue */
                --success-color: #7D8CA3;  /* Blue-gray */
                --warning-color: #59544B;  /* Medium brown */
                --danger-color: #36311F;  /* Dark brown */
                --light-bg: #F5F6F8;  /* Very light gray */
                --dark-text: #36311F;  /* Dark brown */
                --light-text: #ffffff;
            }

            body { 
                font-family: 'Space Grotesk', 'IBM Plex Mono', monospace;
                margin: 0;
                padding: 20px;
                background: var(--light-bg);
                color: var(--dark-text);
                line-height: 1.6;
            }

            .container { 
                max-width: 1400px; 
                margin: 0 auto;
                padding: 20px;
            }

            h1 {
                color: var(--primary-color);
                font-size: 2.2em;
                margin-bottom: 1.5em;
                text-align: center;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 2px;
                font-family: 'Space Grotesk', sans-serif;
            }

            h3 {
                color: var(--primary-color);
                font-size: 1.4em;
                margin-bottom: 1em;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 1px;
                font-family: 'Space Grotesk', sans-serif;
            }

            .card { 
                background: white; 
                padding: 25px; 
                margin: 20px 0; 
                border-radius: 0; 
                box-shadow: 0 4px 6px rgba(0,0,0,0.1);
                border-left: 4px solid var(--accent-color);
            }

            .status { 
                display: flex; 
                gap: 20px; 
                align-items: center; 
                flex-wrap: wrap;
                margin-bottom: 20px;
            }

            .status-item { 
                padding: 12px 20px; 
                border-radius: 0; 
                font-weight: 600;
                font-size: 0.95em;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                text-transform: uppercase;
                letter-spacing: 1px;
                font-family: 'Space Grotesk', sans-serif;
            }

            .connected { 
                background: var(--accent-color); 
                color: var(--light-text);
            }

            .disconnected { 
                background: var(--danger-color); 
                color: var(--light-text);
            }

            .logging { 
                background: var(--success-color); 
                color: var(--light-text);
            }

            .stopped { 
                background: var(--warning-color); 
                color: var(--light-text);
            }

            .csi-live {
                background: #2d6a4f;
                color: var(--light-text);
            }

            .csi-waiting {
                background: #b08968;
                color: var(--light-text);
            }

            .serial-live {
                background: #457b9d;
                color: var(--light-text);
            }

            .serial-waiting {
                background: #8d99ae;
                color: var(--light-text);
            }

            button { 
                padding: 12px 24px; 
                margin: 5px; 
                border: none; 
                border-radius: 0; 
                cursor: pointer; 
                font-size: 0.95em;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 1px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.2);
                font-family: 'Space Grotesk', sans-serif;
            }

            .btn-primary { 
                background: var(--accent-color); 
                color: var(--light-text);
            }

            .btn-success { 
                background: var(--success-color); 
                color: var(--light-text);
            }

            .btn-danger { 
                background: var(--danger-color); 
                color: var(--light-text);
            }

            .btn-warning { 
                background: var(--warning-color); 
                color: var(--light-text);
            }

            .data-display { 
                font-family: 'IBM Plex Mono', monospace; 
                font-size: 0.9em;
            }

            .latest-data { 
                display: grid; 
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); 
                gap: 15px;
            }

            .data-item { 
                background: var(--light-bg); 
                padding: 15px; 
                border-radius: 0;
                font-size: 0.95em;
                border-left: 3px solid var(--accent-color);
                font-family: 'IBM Plex Mono', monospace;
            }

            #data-log { 
                height: 300px; 
                overflow-y: scroll; 
                border: 1px solid #ddd; 
                padding: 15px; 
                background: var(--light-bg);
                border-radius: 0;
                font-family: 'IBM Plex Mono', monospace;
                font-size: 0.9em;
            }

            .chart-container { 
                height: 400px; 
                margin: 20px 0;
                background: white;
                padding: 20px;
                border-radius: 0;
                box-shadow: 0 4px 6px rgba(0,0,0,0.1);
                border-left: 4px solid var(--accent-color);
            }

            .plots-container { 
                display: grid; 
                grid-template-columns: 1fr 1fr; 
                gap: 30px;
            }

            .plot-controls { 
                display: flex; 
                gap: 20px; 
                align-items: center; 
                margin-bottom: 20px; 
                flex-wrap: wrap;
                background: var(--light-bg);
                padding: 15px;
                border-radius: 0;
                border-left: 4px solid var(--accent-color);
            }

            .control-group { 
                display: flex; 
                align-items: center; 
                gap: 10px;
            }

            .subcarrier-browser {
                width: 100%;
                max-height: 220px;
                overflow-y: auto;
                background: white;
                border: 1px solid #d8dee6;
                padding: 14px;
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
            }

            .subcarrier-status {
                width: 100%;
                font-family: 'IBM Plex Mono', monospace;
                font-size: 0.9em;
                color: var(--secondary-color);
            }

            .subcarrier-chip {
                border: 1px solid var(--accent-color);
                background: #ffffff;
                color: var(--dark-text);
                padding: 7px 10px;
                font-size: 0.85em;
                cursor: pointer;
                user-select: none;
                font-family: 'IBM Plex Mono', monospace;
            }

            .subcarrier-chip.pending {
                background: #f4d58d;
                border-color: #d4a373;
            }

            .subcarrier-chip.selected {
                background: var(--accent-color);
                color: var(--light-text);
            }

            .subcarrier-empty {
                font-family: 'IBM Plex Mono', monospace;
                font-size: 0.9em;
                color: var(--secondary-color);
            }

            select, input[type="text"] { 
                padding: 10px 15px; 
                border: 2px solid var(--accent-color); 
                border-radius: 0;
                font-size: 0.95em;
                font-family: 'IBM Plex Mono', monospace;
            }

            select:focus, input[type="text"]:focus {
                border-color: var(--primary-color);
                outline: none;
            }

            .multi-select { 
                min-width: 200px;
            }

            @media (max-width: 1200px) {
                .plots-container { 
                    grid-template-columns: 1fr; 
                }
                
                .container {
                    padding: 10px;
                }
                
                .card {
                    padding: 15px;
                }
            }

            /* Custom scrollbar */
            ::-webkit-scrollbar {
                width: 8px;
            }

            ::-webkit-scrollbar-track {
                background: var(--light-bg);
            }

            ::-webkit-scrollbar-thumb {
                background: var(--accent-color);
            }

            ::-webkit-scrollbar-thumb:hover {
                background: var(--primary-color);
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>ESP32 Real-Time CSI Data Monitor</h1>
            
            <div class="card">
                <h3>Connection Status</h3>
                <div class="status" id="status">
                    <div class="status-item disconnected">Disconnected</div>
                    <div class="status-item stopped">Not Logging</div>
                    <div class="status-item serial-waiting">Serial Waiting</div>
                    <div class="status-item csi-waiting">CSI Waiting</div>
                    <div>Packets: <span id="packet-count">0</span></div>
                    <div>Session: <span id="session-id">None</span></div>
                </div>
                
                <div style="margin-top: 15px;">
                    <input type="text" id="port-input" placeholder="COM9 (slave/RX) or /dev/ttyUSB0" style="padding: 8px; width: 200px;">
                    <button class="btn-primary" onclick="connect()">Connect</button>
                    <button class="btn-danger" onclick="disconnect()">Disconnect</button>
                    <button class="btn-success" onclick="startLogging()">Start Logging</button>
                    <button class="btn-warning" onclick="stopLogging()">Stop Logging</button>
                </div>
                <div class="plot-controls">
                    <div class="control-group">
                        <label>Subcarriers</label>
                    </div>
                    <div class="control-group">
                        <button class="btn-primary" onclick="addPendingSubcarriers()">Add</button>
                    </div>
                    <div class="control-group">
                        <button class="btn-primary" onclick="refreshSubcarriers()">Refresh Subcarriers</button>
                    </div>
                    <div id="subcarrier-status" class="subcarrier-status">Click subcarriers to stage them, then press Add.</div>
                    <div id="subcarrier-browser" class="subcarrier-browser">
                        <div class="subcarrier-empty">Waiting for discovered subcarriers...</div>
                    </div>
                </div>
            </div>
            
            <div class="card">
                <h3>Latest CSI Data</h3>
                <div class="latest-data" id="latest-data">
                    <div class="data-item">No data yet...</div>
                </div>
            </div>

            <div class="card">
                <h3>Serial Diagnostics</h3>
                <div class="latest-data" id="serial-diagnostics">
                    <div class="data-item">Waiting for serial data...</div>
                </div>
            </div>
            
            <div class="card">
                <h3>Real-time Plots</h3>
                <div class="plots-container">
                    <div class="chart-container">
                        <canvas id="rssiChart"></canvas>
                    </div>
                    <div class="chart-container">
                        <canvas id="csiChart"></canvas>
                    </div>
                </div>
            </div>
            
            <div class="card">
                <h3>Data Log</h3>
                <div id="data-log"></div>
            </div>
        </div>
        
        <script>
            // Chart configurations
            const rssiChart = new Chart(document.getElementById('rssiChart'), {
                type: 'line',
                data: {
                    labels: [],
                    datasets: [{
                        label: 'RSSI (dBm)',
                        data: [],
                        borderColor: 'rgb(255, 99, 132)',
                        backgroundColor: 'rgba(255, 99, 132, 0.2)',
                        tension: 0.1,
                        pointRadius: 0,
                        pointHoverRadius: 3,
                        spanGaps: true,
                        borderWidth: 2
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        title: {
                            display: true,
                            text: 'RSSI over Time'
                        }
                    },
                    scales: {
                        x: {
                            title: {
                                display: true,
                                text: 'Time (seconds ago)'
                            }
                        },
                        y: {
                            title: {
                                display: true,
                                text: 'RSSI (dBm)'
                            }
                        }
                    },
                    animation: {
                        duration: 0
                    }
                }
            });
            
            const hiddenCsiSeries = new Set();
            const customSubcarriers = new Set();
            const pendingSubcarriers = new Set();

            const csiChart = new Chart(document.getElementById('csiChart'), {
                type: 'line',
                data: {
                    labels: [],
                    datasets: []
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        title: {
                            display: true,
                            text: 'CSI Values over Time'
                        },
                        legend: {
                            onClick: (event, legendItem, legend) => {
                                const chart = legend.chart;
                                const dataset = chart.data.datasets[legendItem.datasetIndex];
                                if (!dataset) {
                                    return;
                                }

                                const label = dataset.label;
                                if (hiddenCsiSeries.has(label)) {
                                    hiddenCsiSeries.delete(label);
                                    dataset.hidden = false;
                                } else {
                                    hiddenCsiSeries.add(label);
                                    dataset.hidden = true;
                                }
                                chart.update();
                            }
                        }
                    },
                    scales: {
                        x: {
                            title: {
                                display: true,
                                text: 'Time (seconds ago)'
                            }
                        },
                        y: {
                            title: {
                                display: true,
                                text: 'CSI Value'
                            }
                        }
                    },
                    animation: {
                        duration: 0
                    }
                }
            });
            
            function updatePlotConfig() {
                csiChart.options.plugins.title.text = 'CSI Values over Time';
                csiChart.options.scales.y.title.text = 'CSI Value';
                csiChart.data.datasets = [];
                csiChart.update();
            }
            
            function getSelectedSubcarriers() {
                const selected = Array.from(customSubcarriers).sort((a, b) => a - b);
                return selected.length ? selected : [1, 5, 9, 13];
            }

            function renderSubcarrierStatus() {
                const status = document.getElementById('subcarrier-status');
                if (!status) {
                    return;
                }

                if (pendingSubcarriers.size === 0) {
                    status.textContent = 'Click subcarriers to stage them, then press Add.';
                    return;
                }

                status.textContent = `Staged: ${Array.from(pendingSubcarriers).sort((a, b) => a - b).join(', ')}`;
            }

            function renderSubcarrierBrowser(allSubcarriers = []) {
                const browser = document.getElementById('subcarrier-browser');
                if (!browser) {
                    return;
                }

                if (!allSubcarriers.length) {
                    browser.innerHTML = '<div class="subcarrier-empty">Waiting for discovered subcarriers...</div>';
                    return;
                }

                browser.innerHTML = '';
                allSubcarriers.forEach(value => {
                    const chip = document.createElement('button');
                    chip.type = 'button';
                    const isPending = pendingSubcarriers.has(value);
                    const isSelected = customSubcarriers.has(value);
                    chip.className = `subcarrier-chip${isPending ? ' pending' : ''}${isSelected ? ' selected' : ''}`;
                    chip.textContent = `Subcarrier ${value}`;
                    chip.onclick = () => {
                        if (isSelected) {
                            customSubcarriers.delete(value);
                            hiddenCsiSeries.delete(`subcarrier_${value}`);
                            updateCharts();
                        } else if (pendingSubcarriers.has(value)) {
                            pendingSubcarriers.delete(value);
                        } else {
                            pendingSubcarriers.add(value);
                        }
                        renderSubcarrierStatus();
                        renderSubcarrierBrowser(allSubcarriers);
                    };
                    browser.appendChild(chip);
                });
            }

            function addPendingSubcarriers() {
                if (pendingSubcarriers.size === 0) {
                    return;
                }

                pendingSubcarriers.forEach(value => customSubcarriers.add(value));
                pendingSubcarriers.clear();
                renderSubcarrierStatus();
                refreshSubcarriers();
            }

            function updateCharts() {
                const selected = getSelectedSubcarriers();
                fetch('/api/plot_data?subcarriers=' + selected.join(','))
                    .then(response => response.json())
                    .then(data => {
                        if (data.time && data.time.length > 0) {
                            rssiChart.data.labels = data.time;
                            rssiChart.data.datasets[0].data = data.rssi;
                            rssiChart.update('none');

                            csiChart.data.labels = data.time;
                            csiChart.data.datasets = [];

                            if (data.subcarriers) {
                                Object.keys(data.subcarriers).forEach((key, index) => {
                                    const color = [
                                        'rgb(54, 162, 235)',
                                        'rgb(75, 192, 192)',
                                        'rgb(255, 206, 86)',
                                        'rgb(153, 102, 255)',
                                        'rgb(255, 99, 132)',
                                        'rgb(201, 203, 207)'
                                    ][index % 6];
                                    csiChart.data.datasets.push({
                                        label: key,
                                        data: data.subcarriers[key],
                                        borderColor: color,
                                        backgroundColor: color.replace('rgb', 'rgba').replace(')', ', 0.2)'),
                                        tension: 0.1,
                                        pointRadius: 0,
                                        pointHoverRadius: 3,
                                        spanGaps: true,
                                        borderWidth: 2,
                                        hidden: hiddenCsiSeries.has(key)
                                    });
                                });
                            }

                            csiChart.update('none');
                        }
                    })
                    .catch(error => console.error('Error updating charts:', error));
            }

            function refreshSubcarriers() {
                fetch('/api/subcarriers')
                    .then(response => response.json())
                    .then(data => {
                        const merged = Array.from(new Set([
                            ...data,
                            ...Array.from(customSubcarriers),
                            ...Array.from(pendingSubcarriers)
                        ])).sort((a, b) => a - b);

                        if (customSubcarriers.size === 0) {
                            [1, 5, 9, 13].forEach(index => {
                                if (merged.includes(index)) {
                                    customSubcarriers.add(index);
                                }
                            });
                        }

                        renderSubcarrierStatus();
                        renderSubcarrierBrowser(merged);
                        updateCharts();
                    })
                    .catch(error => console.error('Error loading subcarriers:', error));
            }

            function updateStatus() {
                fetch('/api/status')
                    .then(response => response.json())
                    .then(data => {
                        const statusDiv = document.getElementById('status');
                        const connClass = data.connected ? 'connected' : 'disconnected';
                        const connText = data.connected ? 'Connected' : 'Disconnected';
                        const logClass = data.logging ? 'logging' : 'stopped';
                        const logText = data.logging ? 'Logging' : 'Not Logging';
                        const serialClass = data.serial_receiving ? 'serial-live' : 'serial-waiting';
                        const serialText = data.serial_receiving ? 'Serial Live' : 'Serial Waiting';
                        const serialAgeText = data.last_raw_seconds_ago == null ? 'No serial yet' : `${data.last_raw_seconds_ago}s ago`;
                        const csiClass = data.csi_receiving ? 'csi-live' : 'csi-waiting';
                        const csiText = data.csi_receiving ? 'CSI Live' : 'CSI Waiting';
                        const csiAgeText = data.last_csi_seconds_ago == null ? 'No CSI yet' : `${data.last_csi_seconds_ago}s ago`;
                        const csiDiag = data.csi_diag || {};
                        
                        statusDiv.innerHTML = `
                            <div class="status-item ${connClass}">${connText} ${data.port ? '(' + data.port + ')' : ''}</div>
                            <div class="status-item ${logClass}">${logText}</div>
                            <div class="status-item ${serialClass}">${serialText}</div>
                            <div class="status-item ${csiClass}">${csiText}</div>
                            <div>Packets: <span id="packet-count">${data.packet_count}</span></div>
                            <div>Raw Lines: <span>${data.raw_line_count || 0}</span></div>
                            <div>Last Serial: <span>${serialAgeText}</span></div>
                            <div>CSI Packets: <span>${data.csi_packet_count || 0}</span></div>
                            <div>Last CSI: <span>${csiAgeText}</span></div>
                            <div>Session: <span id="session-id">${data.session_id || 'None'}</span></div>
                        `;

                        const serialDiv = document.getElementById('serial-diagnostics');
                        if (serialDiv) {
                            serialDiv.innerHTML = `
                                <div class="data-item"><strong>Raw Line Count:</strong> ${data.raw_line_count || 0}</div>
                                <div class="data-item"><strong>Serial Status:</strong> ${serialText}</div>
                                <div class="data-item"><strong>Last Serial:</strong> ${serialAgeText}</div>
                                <div class="data-item"><strong>CSI Callbacks:</strong> ${csiDiag.callbacks ?? 0}</div>
                                <div class="data-item"><strong>CSI Matches:</strong> ${csiDiag.matches ?? 0}</div>
                                <div class="data-item"><strong>CSI Zero Len:</strong> ${csiDiag.zero_len ?? 0}</div>
                                <div class="data-item"><strong>CSI Queue Drop:</strong> ${csiDiag.queue_drop ?? 0}</div>
                            `;
                        }
                    });
            }
            
            function formatTimePassed(seconds) {
                if (seconds < 60) {
                    return `${seconds.toFixed(1)}s`;
                } else if (seconds < 3600) {
                    const minutes = Math.floor(seconds / 60);
                    const secs = Math.floor(seconds % 60);
                    return `${minutes}m ${secs}s`;
                } else {
                    const hours = Math.floor(seconds / 3600);
                    const minutes = Math.floor((seconds % 3600) / 60);
                    return `${hours}h ${minutes}m`;
                }
            }
            
            function updateLatestData() {
                fetch('/api/latest')
                    .then(response => response.json())
                    .then(data => {
                        const formatSubcarrier = value => value == null ? 'N/A' : Number(value).toFixed(2);
                        if (Object.keys(data).length > 0) {
                            const latestDiv = document.getElementById('latest-data');
                            latestDiv.innerHTML = `
                                <div class="data-item"><strong>Packet #:</strong> ${data.packet_num}</div>
                                <div class="data-item"><strong>RSSI:</strong> ${data.rssi} dBm</div>
                                <div class="data-item"><strong>Rate:</strong> ${data.rate}</div>
                                <div class="data-item"><strong>Channel:</strong> ${data.channel}</div>
                                <div class="data-item"><strong>Bandwidth:</strong> ${data.bandwidth}</div>
                                <div class="data-item"><strong>Data Length:</strong> ${data.data_length}</div>
                                <div class="data-item"><strong>Packet Type:</strong> ${data.packet_type || 'N/A'}</div>
                                <div class="data-item"><strong>Timestamp:</strong> ${data.timestamp ? data.timestamp.split('T')[1].split('.')[0] : 'N/A'}</div>
                                <div class="data-item"><strong>Time Passed:</strong> ${data.time_passed ? formatTimePassed(data.time_passed) : 'N/A'}</div>
                                <div class="data-item"><strong>SC1 Value:</strong> ${formatSubcarrier(data.subcarrier_1)}</div>
                                <div class="data-item"><strong>SC5 Value:</strong> ${formatSubcarrier(data.subcarrier_5)}</div>
                                <div class="data-item"><strong>SC9 Value:</strong> ${formatSubcarrier(data.subcarrier_9)}</div>
                                <div class="data-item"><strong>SC13 Value:</strong> ${formatSubcarrier(data.subcarrier_13)}</div>
                            `;
                        } else {
                            document.getElementById('latest-data').innerHTML = '<div class="data-item">Waiting for CSI data...</div>';
                        }
                    });
            }
            
            function updateDataLog() {
                fetch('/api/recent')
                    .then(response => response.json())
                    .then(data => {
                        const formatSubcarrier = value => value == null ? 'N/A' : Number(value).toFixed(2);
                        const logDiv = document.getElementById('data-log');
                        if (data.length > 0) {
                            logDiv.innerHTML = data.slice(-15).reverse().map(packet => 
                                `<div>Packet #${packet.packet_num} (${packet.packet_type || 'unknown'}): Value=${formatSubcarrier(packet.subcarrier_1)}, Time=${packet.time_passed ? formatTimePassed(packet.time_passed) : 'N/A'} [${packet.timestamp ? packet.timestamp.split('T')[1].split('.')[0] : 'N/A'}]</div>`
                            ).join('');
                            logDiv.scrollTop = 0;
                        } else {
                            logDiv.innerHTML = '<div>Waiting for CSI data...</div>';
                        }
                    });
            }
            
            function connect() {
                const port = document.getElementById('port-input').value || 'COM9';
                fetch('/api/connect', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({port: port})
                }).then(response => response.json())
                  .then(data => {
                      updateStatus();
                  });
            }
            
            function disconnect() {
                fetch('/api/disconnect', {method: 'POST'});
            }
            
            function startLogging() {
                fetch('/api/start', {method: 'POST'});
            }
            
            function stopLogging() {
                fetch('/api/stop', {method: 'POST'});
            }
            
            // Initialize plot configuration
            updatePlotConfig();
            refreshSubcarriers();
            
            // Update every second
            setInterval(() => {
                updateStatus();
                updateLatestData();
                updateDataLog();
                updateCharts();
            }, 1000);
            
            // Initial update
            updateStatus();
        </script>
    </body>
    </html>
    '''

# flask stuff

@app.route('/api/status')
def api_status():
    if logger:
        return jsonify(logger.get_status())
    return jsonify({'connected': False, 'logging': False, 'packet_count': 0, 'port': '', 'session_id': None, 'session_dir': None})

@app.route('/api/latest')
def api_latest():
    if logger:
        return jsonify(logger.get_latest_packet())
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
        subcarriers = request.args.get('subcarriers', '')
        selected = [int(x) for x in subcarriers.split(',') if x.strip().isdigit()]
        plot_data = logger.get_plot_data(selected_subcarriers=selected if selected else None)
        return jsonify(plot_data)
    return jsonify({'time': [], 'rssi': [], 'raw_adc': [], 'filtered_adc': [], 'subcarriers': {}})

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
    print(f"\n{'='*60}")
    print(f"[API] api_connect called with port={port}")
    print(f"{'='*60}\n")
    
    # Close existing connection
    if logger:
        cleanup_logger()
        time.sleep(1)  # Give extra time for cleanup
    
    logger = ESPNOWDataLogger(port)
    success = logger.connect()
    print(f"[API] Connected={success}, port={port}")
    
    # If connected successfully, start logging immediately
    started = False
    if success:
        try:
            started = logger.start_logging()
            print(f"[API] Logging started={started}")
        except Exception as e:
            print(f"[API] Error starting logging: {e}")
            import traceback
            traceback.print_exc()
    
    print(f"[API] Final status: connected={success}, logging={started}\n")
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
        return jsonify({'success': False, 'error': 'Server shutdown is not available'})

    shutdown_func()
    return jsonify({'success': True})

@app.route('/api/start', methods=['POST'])
def api_start():
    if logger:
        success = logger.start_logging()
        return jsonify({'success': success})
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
