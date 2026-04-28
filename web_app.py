from flask import Flask, render_template, jsonify, request
import serial
import json
import re
import datetime
import csv
import threading
import time
import os
from collections import deque
import math
import uuid

CSI_REGEX = re.compile(r'CSI_START(\{[^{}]*\})CSI_END')
ESPNOW_REGEX = re.compile(r'RX,SEQ=(\d+),RAW=(-?\d+),FILT=(-?\d+),RSSI=(-?\d+),TS=(\d+)')

app = Flask(__name__)

class ESPNOWDataLogger:
    def __init__(self, port, baud_rate=115200):
        self.port = port
        self.baud_rate = baud_rate
        self.serial_conn = None
        self.csv_writer = None
        self.csv_file = None
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
            timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"csi_data_{timestamp}.csv"
            filepath = os.path.join(self.session_dir, filename)
            os.makedirs(self.session_dir, exist_ok=True)
            self.csv_file = open(filepath, 'w', newline='', encoding='utf-8')

            fieldnames = [
                'timestamp', 'rssi', 'rate', 'channel', 'bandwidth', 'data_length',
                'esp_timestamp', 'csi_data', 'seq', 'raw_adc', 'filtered_adc'
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
        if not match:
            return None

        try:
            return {
                'seq': int(match.group(1)),
                'raw_adc': int(match.group(2)),
                'filtered_adc': int(match.group(3)),
                'rssi': int(match.group(4)),
                'esp_timestamp': int(match.group(5))
            }
        except ValueError as e:
            print(f"[ERROR] ESP-NOW parse failed: {e} | line={line}")
            return None

    def _process_packet(self, packet):
        python_timestamp = datetime.datetime.now().isoformat()
        csi_data = packet.get('csi_data', [])
        if isinstance(csi_data, str):
            try:
                csi_data = json.loads(csi_data)
            except Exception:
                csi_data = []

        data_length = packet.get('data_length', len(csi_data) if isinstance(csi_data, list) else 0)
        row = {
            'timestamp': python_timestamp,
            'rssi': packet.get('rssi', 0),
            'rate': packet.get('rate', 0),
            'channel': packet.get('channel', 0),
            'bandwidth': packet.get('bandwidth', 0),
            'data_length': data_length,
            'esp_timestamp': packet.get('esp_timestamp', 0),
            'csi_data': json.dumps(csi_data) if csi_data else '[]',
            'seq': packet.get('seq', ''),
            'raw_adc': packet.get('raw_adc', ''),
            'filtered_adc': packet.get('filtered_adc', ''),
        }

        if self.csv_writer:
            try:
                self.csv_writer.writerow(row)
                self.csv_file.flush()
            except Exception as exc:
                print(f"[ERROR] Failed to write CSV row: {exc}")

        self.packet_count += 1
        display_data = {
            'packet_num': self.packet_count,
            'timestamp': python_timestamp,
            'rssi': packet.get('rssi', 0),
            'rate': packet.get('rate', 0),
            'channel': packet.get('channel', 0),
            'bandwidth': packet.get('bandwidth', 0),
            'data_length': data_length,
            'esp_timestamp': packet.get('esp_timestamp', 0),
            'seq': packet.get('seq', 0),
            'raw_adc': packet.get('raw_adc', 0),
            'filtered_adc': packet.get('filtered_adc', 0),
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
            'raw_adc': packet.get('raw_adc', 0),
            'filtered_adc': packet.get('filtered_adc', 0),
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
                            packet = self.parse_csi_line(line)
                            if packet is None:
                                packet = self.parse_espnow_line(line)

                            if packet is not None:
                                self._process_packet(packet)
                            else:
                                print(f"ESP32: {line}")
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
        if hasattr(self, 'logging_thread'):
            self.logging_thread.join(timeout=1)

    def get_status(self):
        return {
            'connected': self.serial_conn is not None and self.serial_conn.is_open,
            'logging': self.is_running,
            'packet_count': self.packet_count,
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
            'raw_adc': [],
            'filtered_adc': [],
            'subcarriers': {}
        }

        for idx in selected_subcarriers:
            plot_data['subcarriers'][f'subcarrier_{idx}'] = []

        for point in list(self.plot_data)[-200:]:
            plot_data['time'].append(round(point['time'], 3))
            plot_data['rssi'].append(point.get('rssi', 0))
            plot_data['raw_adc'].append(point.get('raw_adc', 0))
            plot_data['filtered_adc'].append(point.get('filtered_adc', 0))
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
        if self.csv_file:
            self.csv_file.close()
    
# Global logger instance
logger = None

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
                    <div>Packets: <span id="packet-count">0</span></div>
                    <div>Session: <span id="session-id">None</span></div>
                </div>
                
                <div style="margin-top: 15px;">
                    <input type="text" id="port-input" placeholder="COM10 or /dev/ttyUSB0" style="padding: 8px; width: 200px;">
                    <button class="btn-primary" onclick="connect()">Connect</button>
                    <button class="btn-danger" onclick="disconnect()">Disconnect</button>
                    <button class="btn-success" onclick="startLogging()">Start Logging</button>
                    <button class="btn-warning" onclick="stopLogging()">Stop Logging</button>
                </div>
                <div class="plot-controls">
                    <div class="control-group">
                        <label for="subcarrier-select">Subcarriers</label>
                        <select id="subcarrier-select" multiple size="4" class="multi-select"></select>
                    </div>
                    <div class="control-group">
                        <button class="btn-primary" onclick="refreshSubcarriers()">Refresh Subcarriers</button>
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
                <h3>Real-time Plots</h3>
                <div class="plots-container">
                    <div class="chart-container">
                        <canvas id="rssiChart"></canvas>
                    </div>
                    <div class="chart-container">
                        <canvas id="adcChart"></canvas>
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
                        tension: 0.1
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
            
            const adcChart = new Chart(document.getElementById('adcChart'), {
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
                            text: 'ADC Values over Time'
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
                                text: 'ADC Value'
                            }
                        }
                    },
                    animation: {
                        duration: 0
                    }
                }
            });
            
            function updatePlotConfig() {
                // Update chart title
                adcChart.options.plugins.title.text = 'ADC Values over Time';
                adcChart.options.scales.y.title.text = 'ADC Value';
                
                // Clear existing datasets
                adcChart.data.datasets = [];
                
                // Create ADC datasets
                const adcDatasets = [
                    { label: 'Raw ADC', key: 'raw_adc', color: 'rgb(54, 162, 235)' },
                    { label: 'Filtered ADC', key: 'filtered_adc', color: 'rgb(255, 99, 132)' }
                ];
                
                adcDatasets.forEach(dataset => {
                    adcChart.data.datasets.push({
                        label: dataset.label,
                        data: [],
                        borderColor: dataset.color,
                        backgroundColor: dataset.color.replace('rgb', 'rgba').replace(')', ', 0.2)'),
                        tension: 0.1
                    });
                });
                
                adcChart.update();
            }
            
            function getSelectedSubcarriers() {
                const select = document.getElementById('subcarrier-select');
                if (!select) {
                    return [1, 5, 9, 13];
                }
                const selected = Array.from(select.selectedOptions).map(option => parseInt(option.value, 10));
                return selected.length ? selected : [1, 5, 9, 13];
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

                            adcChart.data.labels = data.time;
                            adcChart.data.datasets = [];

                            adcChart.data.datasets.push({
                                label: 'RSSI',
                                data: data.rssi,
                                borderColor: 'rgb(255, 99, 132)',
                                backgroundColor: 'rgba(255, 99, 132, 0.2)',
                                tension: 0.1
                            });

                            if (data.subcarriers) {
                                Object.keys(data.subcarriers).forEach((key, index) => {
                                    const color = ['rgb(54, 162, 235)', 'rgb(75, 192, 192)', 'rgb(255, 206, 86)', 'rgb(153, 102, 255)'][index % 4];
                                    adcChart.data.datasets.push({
                                        label: key,
                                        data: data.subcarriers[key],
                                        borderColor: color,
                                        backgroundColor: color.replace('rgb', 'rgba').replace(')', ', 0.2)'),
                                        tension: 0.1
                                    });
                                });
                            }

                            adcChart.update('none');
                        }
                    })
                    .catch(error => console.error('Error updating charts:', error));
            }

            function refreshSubcarriers() {
                fetch('/api/subcarriers')
                    .then(response => response.json())
                    .then(data => {
                        const select = document.getElementById('subcarrier-select');
                        if (!select) return;
                        select.innerHTML = '';
                        data.slice(0, 16).forEach(index => {
                            const option = document.createElement('option');
                            option.value = index;
                            option.textContent = `Subcarrier ${index}`;
                            if ([1, 5, 9, 13].includes(index)) {
                                option.selected = true;
                            }
                            select.appendChild(option);
                        });
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
                        
                        statusDiv.innerHTML = `
                            <div class="status-item ${connClass}">${connText} ${data.port ? '(' + data.port + ')' : ''}</div>
                            <div class="status-item ${logClass}">${logText}</div>
                            <div>Packets: <span id="packet-count">${data.packet_count}</span></div>
                            <div>Session: <span id="session-id">${data.session_id || 'None'}</span></div>
                        `;
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
                        if (Object.keys(data).length > 0) {
                            const latestDiv = document.getElementById('latest-data');
                            latestDiv.innerHTML = `
                                <div class="data-item"><strong>Packet #:</strong> ${data.packet_num}</div>
                                <div class="data-item"><strong>RSSI:</strong> ${data.rssi} dBm</div>
                                <div class="data-item"><strong>Rate:</strong> ${data.rate}</div>
                                <div class="data-item"><strong>Channel:</strong> ${data.channel}</div>
                                <div class="data-item"><strong>Bandwidth:</strong> ${data.bandwidth}</div>
                                <div class="data-item"><strong>Data Length:</strong> ${data.data_length}</div>
                                <div class="data-item"><strong>Timestamp:</strong> ${data.timestamp ? data.timestamp.split('T')[1].split('.')[0] : 'N/A'}</div>
                                <div class="data-item"><strong>Time Passed:</strong> ${data.time_passed ? formatTimePassed(data.time_passed) : 'N/A'}</div>
                                <div class="data-item"><strong>SC1 Value:</strong> ${data.subcarrier_1 ? data.subcarrier_1.toFixed(2) : 'N/A'}</div>
                                <div class="data-item"><strong>SC5 Value:</strong> ${data.subcarrier_5 ? data.subcarrier_5.toFixed(2) : 'N/A'}</div>
                                <div class="data-item"><strong>SC9 Value:</strong> ${data.subcarrier_9 ? data.subcarrier_9.toFixed(2) : 'N/A'}</div>
                                <div class="data-item"><strong>SC13 Value:</strong> ${data.subcarrier_13 ? data.subcarrier_13.toFixed(2) : 'N/A'}</div>
                            `;
                        }
                    });
            }
            
            function updateDataLog() {
                fetch('/api/recent')
                    .then(response => response.json())
                    .then(data => {
                        const logDiv = document.getElementById('data-log');
                        if (data.length > 0) {
                            logDiv.innerHTML = data.slice(-15).reverse().map(packet => 
                                `<div>Packet #${packet.packet_num}: Value=${packet.subcarrier_1 ? packet.subcarrier_1.toFixed(2) : 'N/A'}, Time=${packet.time_passed ? formatTimePassed(packet.time_passed) : 'N/A'} [${packet.timestamp ? packet.timestamp.split('T')[1].split('.')[0] : 'N/A'}]</div>`
                            ).join('');
                            logDiv.scrollTop = 0;
                        }
                    });
            
            // also fetch raw serial lines for debugging
            fetch('/api/raw')
                .then(response => response.json())
                .then(lines => {
                    if (lines && lines.length) {
                        console.log('raw lines:', lines.slice(-10));
                    }
                });
            }
            
            function connect() {
                const port = document.getElementById('port-input').value || 'COM10';
                fetch('/api/connect', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({port: port})
                }).then(response => response.json())
                  .then(data => {
                      console.log('connect response', data);
                      // refresh status right away
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
        raw_data = logger.get_raw_lines()
        # Show detailed info about raw lines
        print(f"[DEBUG] Raw lines count: {len(raw_data)}")
        if raw_data:
            print(f"[DEBUG] Last raw line: {raw_data[-1][:200]}")
        return jsonify(raw_data)
    return jsonify([])

@app.route('/api/connect', methods=['POST'])
def api_connect():
    global logger
    data = request.get_json()
    port = data.get('port', 'COM10')
    print(f"\n{'='*60}")
    print(f"[API] api_connect called with port={port}")
    print(f"{'='*60}\n")
    
    # Close existing connection
    if logger:
        logger.close()
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
    if logger:
        logger.close()
        logger = None
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
        app.run(debug=True, host='0.0.0.0', port=5000)
    finally:
        if logger:
            logger.close()