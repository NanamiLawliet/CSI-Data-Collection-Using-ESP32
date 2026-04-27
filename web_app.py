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

app = Flask(__name__)

class CSIDataLogger:
    def __init__(self, port, baud_rate=115200):
        # Basic serial connection settings
        self.port = port
        self.baud_rate = baud_rate
        self.serial_conn = None
        self.csv_writer = None
        self.csv_file = None
        self.is_running = False
        self.packet_count = 0
        self.session_start_time = None
        
        # Each logging session gets its own directory with a unique ID
        # This helps keep data organized when doing multiple experiments
        self.session_id = str(uuid.uuid4())[:8]
        self.session_dir = f"sessions/session-{self.session_id}"
        os.makedirs(self.session_dir, exist_ok=True)
        
        # Keep track of recent packets for the web display
        # Using a deque with maxlen=100 means we only keep the last 100 packets
        # This prevents memory from growing too large during long sessions
        self.recent_data = deque(maxlen=100)
        self.latest_packet = {}
        
        # Store data points for plotting
        # We keep 200 points for smooth scrolling plots
        self.plot_data = deque(maxlen=200)
        
        # Track which subcarriers we've seen data for
        # This helps populate the dropdown menu in the web UI
        self.available_subcarriers = set()
        
        # Save raw serial lines for debugging
        self.raw_lines = deque(maxlen=50)
        
    def connect(self):
        """Try to connect to the ESP32 over serial port"""
        try:
            self.serial_conn = serial.Serial(self.port, self.baud_rate, timeout=1)
            print(f"Connected to ESP32 on {self.port}")
            return True
        except serial.SerialException as e:
            print(f"Failed to connect: {e}")
            return False

    def setup_csv_file(self):
        """Create a new CSV file for this logging session"""
        try:
            # Create filename with timestamp so we know when the data was collected
            timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"csi_data_{timestamp}.csv"
            filepath = os.path.join(self.session_dir, filename)
            
            # Ensure directory exists
            os.makedirs(self.session_dir, exist_ok=True)
            print(f"[CSV] Session directory: {os.path.abspath(self.session_dir)}")
            
            self.csv_file = open(filepath, 'w', newline='')
            print(f"[CSV] File opened: {os.path.abspath(filepath)}")
            
            # Define what data we'll store in each row
            fieldnames = [
                'timestamp', 'rssi', 'rate', 'channel', 'bandwidth', 
                'data_length', 'esp_timestamp', 'csi_data'
            ]
            
            self.csv_writer = csv.DictWriter(self.csv_file, fieldnames=fieldnames)
            self.csv_writer.writeheader()
            self.csv_file.flush()
            
            print(f"[CSV] CSV writer initialized. File: {filepath}")
            print(f"[CSV] Headers written: {fieldnames}")
            return filepath
        except Exception as e:
            print(f"[ERROR] Failed to setup CSV file: {e}")
            import traceback
            traceback.print_exc()
            raise
    
    def parse_csi_line(self, line):
        """Extract CSI data from the ESP32's CSV output format
        
        The ESP32 now sends data in CSV format:
        CSI_DATA,timestamp,rssi,rate,channel,mac,len,magnitude,phase,csi_string
        
        We parse this CSV line and convert it to the same dict format expected by the rest of the code
        """
        if not line.startswith('CSI_DATA'):
            return None
        
        try:
            # Split by comma but handle the last field (CSI array in quotes)
            parts = line.split(',', 9)  # Max 9 splits = 10 fields
            
            if len(parts) < 10:
                print(f"[WARNING] CSV line has fewer than 10 fields: {line[:100]}")
                return None
            
            # Parse each field
            timestamp = int(parts[1])
            rssi = int(parts[2])
            rate = int(parts[3])
            channel = int(parts[4])
            mac = parts[5]
            csi_len = int(parts[6])
            magnitude = int(parts[7])
            phase = int(parts[8])
            csi_string = parts[9]
            
            # Parse CSI array from string representation
            # Remove quotes and square brackets, then convert to list
            csi_string = csi_string.strip().strip('"').strip('[]')
            csi_array = [int(x.strip()) for x in csi_string.split(',') if x.strip()]
            
            # Convert to the same format the rest of the code expects
            data = {
                'rssi': rssi,
                'rate': rate,
                'channel': channel,
                'bandwidth': 0,  # Not provided in CSV format
                'len': csi_len,
                'timestamp': timestamp,
                'magnitude': magnitude,
                'phase': phase,
                'csi_data': csi_array,
                'mac': mac
            }
            
            print(f"[SUCCESS] Parsed CSI packet: RSSI={rssi}dBm, CH={channel}, MAC={mac}, LEN={len(csi_array)} subcarriers")
            return data
            
        except (ValueError, IndexError) as e:
            print(f"[ERROR] CSV parse failed: {e}")
            print(f"[ERROR] Attempted to parse: {line[:150]}...")
            return None
    
    def analyze_csi_structure(self, csi_data):
        """Figure out what CSI data we're getting from the ESP32
        
        The CSI data is an array of values, where each value represents
        a subcarrier. We keep track of which subcarriers we've seen
        so we can show them in the web UI's dropdown menu.
        """
        if not csi_data or not isinstance(csi_data, list):
            return {}
        
        # Add each subcarrier index to our set of available ones
        for i in range(len(csi_data)):
            self.available_subcarriers.add(i)
        
        return {'total_subcarriers': len(csi_data)}
    
    def extract_subcarrier_data(self, csi_data, subcarrier_indices):
        """Get the raw values for specific subcarriers
        
        The ESP32 sends CSI data as an array of integers, where each
        integer represents the signal strength for that subcarrier.
        This function extracts just the values we want to plot.
        """
        if not csi_data:
            print("No CSI data provided")
            return {}
        
        result = {}
        
        for idx in subcarrier_indices:
            try:
                if idx < len(csi_data):
                    # Get the raw value for this subcarrier
                    value = csi_data[idx]
                    result[f'subcarrier_{idx}'] = value
                else:
                    print(f"Subcarrier {idx} index out of range (len={len(csi_data)})")
                    result[f'subcarrier_{idx}'] = 0
                    
            except (TypeError, ValueError, IndexError) as e:
                print(f"Error processing subcarrier {idx}: {e}")
                result[f'subcarrier_{idx}'] = 0
        
        return result
    
    def start_logging(self):
        """Start collecting CSI data in a background thread
        
        This function:
        1. Checks if we're already connected and not already logging
        2. Creates a new CSV file for this session
        3. Starts a background thread to read data from the ESP32
        """
        if not self.serial_conn:
            print("Not connected to ESP32")
            return False
        
        if self.is_running:
            print("Already logging")
            return False
            
        self.is_running = True
        self.session_start_time = time.time()
        self.csv_filename = self.setup_csv_file()
        
        # Start the logging loop in a separate thread
        # This keeps the web UI responsive while we collect data
        self.logging_thread = threading.Thread(target=self._log_loop)
        self.logging_thread.daemon = True  # Thread will exit when main program exits
        self.logging_thread.start()
        
        return True
    
    def _log_loop(self):
        """Main loop that reads data from the ESP32
        
        This function runs in a background thread and:
        1. Reads data from the serial port
        2. Parses the CSI data
        3. Saves it to CSV
        4. Updates the data structures used by the web UI
        """
        try:
            print("Starting CSI data collection...")
            print(f"CSV file: {self.csv_filename}")
            print(f"CSV writer initialized: {self.csv_writer is not None}")
            
            while self.is_running:
                if self.serial_conn and self.serial_conn.in_waiting > 0:
                    try:
                        # Read a line from the ESP32
                        line = self.serial_conn.readline().decode('utf-8', errors='ignore').strip()
                        # store raw line for debugging
                        if line:
                            self.raw_lines.append(line)
                        
                        if line:
                            # Try to parse the CSI data
                            csi_data = self.parse_csi_line(line)
                            
                            if csi_data:
                                try:
                                    python_timestamp = datetime.datetime.now().isoformat()
                                    current_time = time.time()
                                    
                                    # Get the CSI array and analyze its structure
                                    csi_array = csi_data.get('csi_data', [])
                                    self.analyze_csi_structure(csi_array)
                                    
                                    # Prepare the row for the CSV file
                                    row = {
                                        'timestamp': python_timestamp,
                                        'rssi': csi_data.get('rssi', ''),
                                        'rate': csi_data.get('rate', ''),
                                        'channel': csi_data.get('channel', ''),
                                        'bandwidth': csi_data.get('bandwidth', ''),
                                        'data_length': csi_data.get('data_length', ''),  # Changed from 'len' to 'data_length'
                                        'esp_timestamp': csi_data.get('esp_timestamp', ''),  # Changed from 'timestamp' to 'esp_timestamp'
                                        'csi_data': json.dumps(csi_array)
                                    }
                                    
                                    # Save to CSV - with extra error checking
                                    if self.csv_writer and self.csv_file:
                                        try:
                                            self.csv_writer.writerow(row)
                                            self.csv_file.flush()  # Make sure data is written to disk
                                            print(f"[CSV] Wrote packet #{self.packet_count + 1} to {self.csv_filename}")
                                        except Exception as csv_error:
                                            print(f"[ERROR] Failed to write CSV row: {csv_error}")
                                    else:
                                        print(f"[ERROR] CSV writer or file not initialized! Writer={self.csv_writer}, File={self.csv_file}")
                                    
                                    # Update the data structures used by the web UI
                                    self.packet_count += 1
                                    display_data = {
                                        'packet_num': self.packet_count,
                                        'timestamp': python_timestamp,
                                        'rssi': csi_data.get('rssi', 0),
                                        'rate': csi_data.get('rate', 0),
                                        'channel': csi_data.get('channel', 0),
                                        'bandwidth': csi_data.get('bandwidth', 0),
                                        'data_length': csi_data.get('len', 0),
                                        'esp_timestamp': csi_data.get('esp_timestamp', 0),
                                        'time_passed': current_time - self.session_start_time if self.session_start_time else 0
                                    }
                                    
                                    # Add subcarrier data to display
                                    for i in range(len(csi_array)):
                                        display_data[f'subcarrier_{i}'] = csi_array[i]
                                    
                                    # Update the data structures for the web UI
                                    self.recent_data.append(display_data)
                                    self.latest_packet = display_data
                                    
                                    # Store data for plotting
                                    plot_point = {
                                        'time': current_time,
                                        'rssi': csi_data.get('rssi', 0)
                                    }
                                    
                                    # Add all CSI values to the plot data
                                    for i in range(len(csi_array)):
                                        plot_point[f'subcarrier_{i}'] = csi_array[i]
                                    
                                    self.plot_data.append(plot_point)
                                    
                                except Exception as e:
                                    print(f"[ERROR] Error processing parsed CSI data: {e}")
                                    import traceback
                                    traceback.print_exc()
                            
                            else:
                                # Print non-CSI output from the ESP32 (connection messages, etc.)
                                print(f"ESP32: {line}")
                    except Exception as e:
                        print(f"Error processing serial line: {e}")
                        import traceback
                        traceback.print_exc()
                
                # Small delay to prevent using too much CPU
                time.sleep(0.01)
                
        except Exception as e:
            print(f"Logging error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.is_running = False
    
    def stop_logging(self):
        """Stop collecting CSI data and clean up"""
        self.is_running = False
        if hasattr(self, 'logging_thread'):
            self.logging_thread.join(timeout=1)  # Wait up to 1 second for thread to finish
    
    def get_status(self):
        """Get the current status of the logger
        
        Returns a dictionary with:
        - Whether we're connected to the ESP32
        - Whether we're currently logging
        - How many packets we've collected
        - The serial port we're using
        - The current session ID and directory
        """
        return {
            'connected': self.serial_conn and self.serial_conn.is_open,
            'logging': self.is_running,
            'packet_count': self.packet_count,
            'port': self.port,
            'session_id': self.session_id,
            'session_dir': self.session_dir
        }
    
    def get_recent_data(self):
        """Get the last 100 packets for the web UI's data log"""
        return list(self.recent_data)
    
    def get_latest_packet(self):
        """Get the most recent packet for the web UI's latest data display"""
        return self.latest_packet
    
    def get_available_subcarriers(self):
        """Get a list of subcarriers we've seen data for
        
        This is used to populate the dropdown menu in the web UI
        where users can select which subcarriers to plot.
        """
        return sorted(list(self.available_subcarriers))
    
    def get_raw_lines(self):
        """Return recent raw lines received from the serial port for debugging"""
        return list(self.raw_lines)
    
    def get_plot_data(self, selected_subcarriers=None):
        """Return data formatted for plotting with configurable subcarriers"""
        if not self.plot_data:
            return {'time': [], 'rssi': [], 'subcarriers': {}}
        
        if selected_subcarriers is None:
            selected_subcarriers = [1, 5, 9, 13]  # Default
        
        print(f"Getting plot data for subcarriers: {selected_subcarriers}")  # Debug log
        
        # Get current time to calculate relative timestamps
        current_time = time.time()
        
        # Convert to relative time (seconds ago) for easier plotting
        plot_formatted = {
            'time': [],
            'rssi': [],
            'subcarriers': {}
        }
        
        # Initialize subcarrier data
        for sc in selected_subcarriers:
            key = f'subcarrier_{sc}'
            plot_formatted['subcarriers'][key] = []
        
        # Only use the last 100 points
        recent_points = list(self.plot_data)[-100:]
        print(f"Number of recent points: {len(recent_points)}")  # Debug log
        
        for point in recent_points:
            relative_time = point['time'] - current_time  # This will be negative (seconds ago)
            plot_formatted['time'].append(relative_time)
            plot_formatted['rssi'].append(point.get('rssi', 0))
            
            # Add subcarrier data
            for sc in selected_subcarriers:
                key = f'subcarrier_{sc}'
                value = point.get(key, 0)
                plot_formatted['subcarriers'][key].append(value)
                if len(plot_formatted['subcarriers'][key]) == 1:  # Debug log first value
                    print(f"First value for {key}: {value}")
        
        return plot_formatted
    
    def close(self):
        self.stop_logging()
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
        if self.csv_file:
            self.csv_file.close()

# Global logger instances - one for each device
logger_tx = None  # Transmitter (COM9)
logger_rx = None  # Receiver (COM10)

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
            <div class="card">
                <h3>Connection Status</h3>
                
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 30px;">
                    <!-- TRANSMITTER (COM9) STATUS -->
                    <div>
                        <h4 style="color: var(--accent-color); margin-bottom: 1em;">📡 TRANSMITTER (COM9)</h4>
                        <div class="status" id="status-tx">
                            <div class="status-item disconnected">Disconnected</div>
                            <div class="status-item stopped">Not Logging</div>
                            <div>Packets: <span id="packet-count-tx">0</span></div>
                        </div>
                        <div style="margin-top: 15px;">
                            <input type="text" id="port-input-tx" placeholder="COM9" value="COM9" style="padding: 8px; width: 150px;">
                            <button class="btn-primary" onclick="connectTx()">Connect</button>
                            <button class="btn-danger" onclick="disconnectTx()">Disconnect</button>
                        </div>
                        <div style="margin-top: 10px;">
                            <button class="btn-success" onclick="startLoggingTx()" style="width: 140px;">Start TX Logging</button>
                            <button class="btn-warning" onclick="stopLoggingTx()" style="width: 140px;">Stop TX Logging</button>
                        </div>
                    </div>
                    
                    <!-- RECEIVER (COM10) STATUS -->
                    <div>
                        <h4 style="color: #7D8CA3; margin-bottom: 1em;">📡 RECEIVER (COM10)</h4>
                        <div class="status" id="status-rx">
                            <div class="status-item disconnected">Disconnected</div>
                            <div class="status-item stopped">Not Logging</div>
                            <div>Packets: <span id="packet-count-rx">0</span></div>
                        </div>
                        <div style="margin-top: 15px;">
                            <input type="text" id="port-input-rx" placeholder="COM10" value="COM10" style="padding: 8px; width: 150px;">
                            <button class="btn-primary" onclick="connectRx()">Connect</button>
                            <button class="btn-danger" onclick="disconnectRx()">Disconnect</button>
                        </div>
                        <div style="margin-top: 10px;">
                            <button class="btn-success" onclick="startLoggingRx()" style="width: 140px;">Start RX Logging</button>
                            <button class="btn-warning" onclick="stopLoggingRx()" style="width: 140px;">Stop RX Logging</button>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="card">
                <h3>Latest CSI Data - Comparison</h3>
                
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 30px;">
                    <!-- TRANSMITTER DATA -->
                    <div>
                        <h4 style="color: var(--accent-color); margin-bottom: 1em;">📤 Transmitter Signal</h4>
                        <div class="latest-data" id="latest-data-tx">
                            <div class="data-item">No data yet...</div>
                        </div>
                    </div>
                    
                    <!-- RECEIVER DATA -->
                    <div>
                        <h4 style="color: #7D8CA3; margin-bottom: 1em;">📥 Receiver Signal</h4>
                        <div class="latest-data" id="latest-data-rx">
                            <div class="data-item">No data yet...</div>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="card">
                <h3>Real-time RSSI Plots</h3>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 30px;">
                    <div class="chart-container">
                        <h4 style="text-align: center; color: var(--accent-color);">📤 Transmitter RSSI</h4>
                        <canvas id="rssiChartTx"></canvas>
                    </div>
                    <div class="chart-container">
                        <h4 style="text-align: center; color: #7D8CA3;">📥 Receiver RSSI</h4>
                        <canvas id="rssiChartRx"></canvas>
                    </div>
                </div>
            </div>
            
            <div class="card">
                <h3>Real-time Subcarrier Plots</h3>
                <div class="plot-controls" style="margin-bottom: 20px;">
                    <div class="control-group">
                        <label>TX Subcarriers:</label>
                        <select id="tx-subcarrier1" class="subcarrier-select">
                            <option value="1" selected>Subcarrier 1</option>
                        </select>
                        <select id="tx-subcarrier2" class="subcarrier-select">
                            <option value="5" selected>Subcarrier 5</option>
                        </select>
                        <select id="tx-subcarrier3" class="subcarrier-select">
                            <option value="9" selected>Subcarrier 9</option>
                        </select>
                        <select id="tx-subcarrier4" class="subcarrier-select">
                            <option value="13" selected>Subcarrier 13</option>
                        </select>
                        <button class="btn-primary" onclick="updatePlotConfigTx()">Update TX Plots</button>
                    </div>
                </div>
                <div class="plot-controls">
                    <div class="control-group">
                        <label>RX Subcarriers:</label>
                        <select id="rx-subcarrier1" class="subcarrier-select">
                            <option value="1" selected>Subcarrier 1</option>
                        </select>
                        <select id="rx-subcarrier2" class="subcarrier-select">
                            <option value="5" selected>Subcarrier 5</option>
                        </select>
                        <select id="rx-subcarrier3" class="subcarrier-select">
                            <option value="9" selected>Subcarrier 9</option>
                        </select>
                        <select id="rx-subcarrier4" class="subcarrier-select">
                            <option value="13" selected>Subcarrier 13</option>
                        </select>
                        <button class="btn-primary" onclick="updatePlotConfigRx()">Update RX Plots</button>
                    </div>
                </div>
                
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 30px;">
                    <div class="chart-container">
                        <h4 style="text-align: center; color: var(--accent-color);">📤 Transmitter Subcarriers</h4>
                        <canvas id="subcarrierChartTx"></canvas>
                    </div>
                    <div class="chart-container">
                        <h4 style="text-align: center; color: #7D8CA3;">📥 Receiver Subcarriers</h4>
                        <canvas id="subcarrierChartRx"></canvas>
                    </div>
                </div>
            </div>
            
            <div class="card">
                <h3>Data Logs</h3>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 30px;">
                    <div>
                        <h4 style="color: var(--accent-color); margin-bottom: 1em;">📤 Transmitter Log</h4>
                        <div id="data-log-tx" style="height: 300px; overflow-y: scroll; border: 1px solid #ddd; padding: 15px; background: var(--light-bg); border-radius: 0; font-family: 'IBM Plex Mono', monospace; font-size: 0.9em;"></div>
                    </div>
                    <div>
                        <h4 style="color: #7D8CA3; margin-bottom: 1em;">📥 Receiver Log</h4>
                        <div id="data-log-rx" style="height: 300px; overflow-y: scroll; border: 1px solid #ddd; padding: 15px; background: var(--light-bg); border-radius: 0; font-family: 'IBM Plex Mono', monospace; font-size: 0.9em;"></div>
                    </div>
                </div>
            </div>
        </div>
        
        <script>
            let selectedSubcarriersTx = [1, 5, 9, 13];
            let selectedSubcarriersRx = [1, 5, 9, 13];
            
            // Chart configurations for TRANSMITTER
            const rssiChartTx = new Chart(document.getElementById('rssiChartTx'), {
                type: 'line',
                data: {
                    labels: [],
                    datasets: [{
                        label: 'TX RSSI (dBm)',
                        data: [],
                        borderColor: 'rgb(121, 169, 209)',
                        backgroundColor: 'rgba(121, 169, 209, 0.2)',
                        tension: 0.1
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        title: { display: false }
                    },
                    scales: {
                        y: {
                            min: -100,
                            max: 0
                        }
                    },
                    animation: { duration: 0 }
                }
            });
            
            // Chart configurations for RECEIVER
            const rssiChartRx = new Chart(document.getElementById('rssiChartRx'), {
                type: 'line',
                data: {
                    labels: [],
                    datasets: [{
                        label: 'RX RSSI (dBm)',
                        data: [],
                        borderColor: 'rgb(125, 140, 163)',
                        backgroundColor: 'rgba(125, 140, 163, 0.2)',
                        tension: 0.1
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        title: { display: false }
                    },
                    scales: {
                        y: {
                            min: -100,
                            max: 0
                        }
                    },
                    animation: { duration: 0 }
                }
            });
            
            // Subcarrier Charts
            const subcarrierChartTx = new Chart(document.getElementById('subcarrierChartTx'), {
                type: 'line',
                data: { labels: [], datasets: [] },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: { duration: 0 }
                }
            });
            
            const subcarrierChartRx = new Chart(document.getElementById('subcarrierChartRx'), {
                type: 'line',
                data: { labels: [], datasets: [] },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: { duration: 0 }
                }
            });
            
            function initializeSubcarrierDropdowns() {
                ['tx-subcarrier1', 'tx-subcarrier2', 'tx-subcarrier3', 'tx-subcarrier4', 
                 'rx-subcarrier1', 'rx-subcarrier2', 'rx-subcarrier3', 'rx-subcarrier4'].forEach(id => {
                    const select = document.getElementById(id);
                    const options = select.innerHTML;
                    if (!options.includes('Subcarrier 10')) {
                        select.innerHTML = '';
                        for (let i = 0; i < 128; i++) {
                            const option = document.createElement('option');
                            option.value = i;
                            option.textContent = `Subcarrier ${i}`;
                            select.appendChild(option);
                        }
                    }
                });
            }
            
            function updatePlotConfigTx() {
                selectedSubcarriersTx = [
                    parseInt(document.getElementById('tx-subcarrier1').value),
                    parseInt(document.getElementById('tx-subcarrier2').value),
                    parseInt(document.getElementById('tx-subcarrier3').value),
                    parseInt(document.getElementById('tx-subcarrier4').value)
                ];
                subcarrierChartTx.data.datasets = [];
                const colors = ['rgb(54, 162, 235)', 'rgb(255, 205, 86)', 'rgb(75, 192, 192)', 'rgb(153, 102, 255)'];
                selectedSubcarriersTx.forEach((sc, index) => {
                    const color = colors[index % colors.length];
                    subcarrierChartTx.data.datasets.push({
                        label: `SC ${sc}`,
                        data: [],
                        borderColor: color,
                        backgroundColor: color.replace('rgb', 'rgba').replace(')', ', 0.2)'),
                        tension: 0.1
                    });
                });
                subcarrierChartTx.update();
            }
            
            function updatePlotConfigRx() {
                selectedSubcarriersRx = [
                    parseInt(document.getElementById('rx-subcarrier1').value),
                    parseInt(document.getElementById('rx-subcarrier2').value),
                    parseInt(document.getElementById('rx-subcarrier3').value),
                    parseInt(document.getElementById('rx-subcarrier4').value)
                ];
                subcarrierChartRx.data.datasets = [];
                const colors = ['rgb(54, 162, 235)', 'rgb(255, 205, 86)', 'rgb(75, 192, 192)', 'rgb(153, 102, 255)'];
                selectedSubcarriersRx.forEach((sc, index) => {
                    const color = colors[index % colors.length];
                    subcarrierChartRx.data.datasets.push({
                        label: `SC ${sc}`,
                        data: [],
                        borderColor: color,
                        backgroundColor: color.replace('rgb', 'rgba').replace(')', ', 0.2)'),
                        tension: 0.1
                    });
                });
                subcarrierChartRx.update();
            }
            
            function updateCharts() {
                // Update TX Charts
                const params_tx = new URLSearchParams({
                    subcarriers: selectedSubcarriersTx.join(','),
                    role: 'tx'
                });
                
                fetch('/api/plot_data?' + params_tx)
                    .then(response => response.json())
                    .then(data => {
                        if (data.time && data.time.length > 0) {
                            rssiChartTx.data.labels = data.time;
                            rssiChartTx.data.datasets[0].data = data.rssi;
                            rssiChartTx.update('none');
                            
                            subcarrierChartTx.data.labels = data.time;
                            selectedSubcarriersTx.forEach((sc, index) => {
                                const key = `subcarrier_${sc}`;
                                if (subcarrierChartTx.data.datasets[index] && data.subcarriers[key]) {
                                    subcarrierChartTx.data.datasets[index].data = data.subcarriers[key];
                                }
                            });
                            subcarrierChartTx.update('none');
                        }
                    })
                    .catch(error => console.error('Error updating TX charts:', error));
                
                // Update RX Charts
                const params_rx = new URLSearchParams({
                    subcarriers: selectedSubcarriersRx.join(','),
                    role: 'rx'
                });
                
                fetch('/api/plot_data?' + params_rx)
                    .then(response => response.json())
                    .then(data => {
                        if (data.time && data.time.length > 0) {
                            rssiChartRx.data.labels = data.time;
                            rssiChartRx.data.datasets[0].data = data.rssi;
                            rssiChartRx.update('none');
                            
                            subcarrierChartRx.data.labels = data.time;
                            selectedSubcarriersRx.forEach((sc, index) => {
                                const key = `subcarrier_${sc}`;
                                if (subcarrierChartRx.data.datasets[index] && data.subcarriers[key]) {
                                    subcarrierChartRx.data.datasets[index].data = data.subcarriers[key];
                                }
                            });
                            subcarrierChartRx.update('none');
                        }
                    })
                    .catch(error => console.error('Error updating RX charts:', error));
            }
            
            function updateStatusTx() {
                fetch('/api/status?role=tx')
                    .then(response => response.json())
                    .then(data => {
                        const statusDiv = document.getElementById('status-tx');
                        const connClass = data.connected ? 'connected' : 'disconnected';
                        const connText = data.connected ? 'Connected' : 'Disconnected';
                        const logClass = data.logging ? 'logging' : 'stopped';
                        const logText = data.logging ? 'Logging' : 'Not Logging';
                        
                        statusDiv.innerHTML = `
                            <div class="status-item ${connClass}">${connText} ${data.port ? '(' + data.port + ')' : ''}</div>
                            <div class="status-item ${logClass}">${logText}</div>
                            <div>Packets: <span id="packet-count-tx">${data.packet_count}</span></div>
                        `;
                    });
            }
            
            function updateStatusRx() {
                fetch('/api/status?role=rx')
                    .then(response => response.json())
                    .then(data => {
                        const statusDiv = document.getElementById('status-rx');
                        const connClass = data.connected ? 'connected' : 'disconnected';
                        const connText = data.connected ? 'Connected' : 'Disconnected';
                        const logClass = data.logging ? 'logging' : 'stopped';
                        const logText = data.logging ? 'Logging' : 'Not Logging';
                        
                        statusDiv.innerHTML = `
                            <div class="status-item ${connClass}">${connText} ${data.port ? '(' + data.port + ')' : ''}</div>
                            <div class="status-item ${logClass}">${logText}</div>
                            <div>Packets: <span id="packet-count-rx">${data.packet_count}</span></div>
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
            
            function updateLatestDataTx() {
                fetch('/api/latest?role=tx')
                    .then(response => response.json())
                    .then(data => {
                        if (Object.keys(data).length > 0) {
                            const latestDiv = document.getElementById('latest-data-tx');
                            latestDiv.innerHTML = `
                                <div class="data-item"><strong>Packet #:</strong> ${data.packet_num}</div>
                                <div class="data-item"><strong>RSSI:</strong> ${data.rssi} dBm</div>
                                <div class="data-item"><strong>Rate:</strong> ${data.rate}</div>
                                <div class="data-item"><strong>Channel:</strong> ${data.channel}</div>
                                <div class="data-item"><strong>SC1:</strong> ${data.subcarrier_1 ? data.subcarrier_1.toFixed(2) : 'N/A'}</div>
                                <div class="data-item"><strong>SC5:</strong> ${data.subcarrier_5 ? data.subcarrier_5.toFixed(2) : 'N/A'}</div>
                                <div class="data-item"><strong>Time:</strong> ${data.time_passed ? formatTimePassed(data.time_passed) : 'N/A'}</div>
                            `;
                        }
                    });
            }
            
            function updateLatestDataRx() {
                fetch('/api/latest?role=rx')
                    .then(response => response.json())
                    .then(data => {
                        if (Object.keys(data).length > 0) {
                            const latestDiv = document.getElementById('latest-data-rx');
                            latestDiv.innerHTML = `
                                <div class="data-item"><strong>Packet #:</strong> ${data.packet_num}</div>
                                <div class="data-item"><strong>RSSI:</strong> ${data.rssi} dBm</div>
                                <div class="data-item"><strong>Rate:</strong> ${data.rate}</div>
                                <div class="data-item"><strong>Channel:</strong> ${data.channel}</div>
                                <div class="data-item"><strong>SC1:</strong> ${data.subcarrier_1 ? data.subcarrier_1.toFixed(2) : 'N/A'}</div>
                                <div class="data-item"><strong>SC5:</strong> ${data.subcarrier_5 ? data.subcarrier_5.toFixed(2) : 'N/A'}</div>
                                <div class="data-item"><strong>Time:</strong> ${data.time_passed ? formatTimePassed(data.time_passed) : 'N/A'}</div>
                            `;
                        }
                    });
            }
            
            function updateDataLogTx() {
                fetch('/api/recent?role=tx')
                    .then(response => response.json())
                    .then(data => {
                        const logDiv = document.getElementById('data-log-tx');
                        if (data.length > 0) {
                            logDiv.innerHTML = data.slice(-10).reverse().map(packet => 
                                `<div>P#${packet.packet_num}: RSSI=${packet.rssi}dBm, SC1=${packet.subcarrier_1 ? packet.subcarrier_1.toFixed(1) : 'N/A'}</div>`
                            ).join('');
                            logDiv.scrollTop = 0;
                        }
                    });
            }
            
            function updateDataLogRx() {
                fetch('/api/recent?role=rx')
                    .then(response => response.json())
                    .then(data => {
                        const logDiv = document.getElementById('data-log-rx');
                        if (data.length > 0) {
                            logDiv.innerHTML = data.slice(-10).reverse().map(packet => 
                                `<div>P#${packet.packet_num}: RSSI=${packet.rssi}dBm, SC1=${packet.subcarrier_1 ? packet.subcarrier_1.toFixed(1) : 'N/A'}</div>`
                            ).join('');
                            logDiv.scrollTop = 0;
                        }
                    });
            }
            
            function connectTx() {
                const port = document.getElementById('port-input-tx').value || 'COM9';
                fetch('/api/connect', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({port: port, role: 'tx'})
                }).then(() => {
                    setTimeout(initializeSubcarrierDropdowns, 1000);
                    updateStatusTx();
                });
            }
            
            function connectRx() {
                const port = document.getElementById('port-input-rx').value || 'COM10';
                fetch('/api/connect', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({port: port, role: 'rx'})
                }).then(() => {
                    setTimeout(initializeSubcarrierDropdowns, 1000);
                    updateStatusRx();
                });
            }
            
            function disconnectTx() {
                fetch('/api/disconnect', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({role: 'tx'})
                });
            }
            
            function disconnectRx() {
                fetch('/api/disconnect', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({role: 'rx'})
                });
            }
            
            function startLoggingTx() {
                fetch('/api/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({role: 'tx'})
                });
            }
            
            function startLoggingRx() {
                fetch('/api/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({role: 'rx'})
                });
            }
            
            function stopLoggingTx() {
                fetch('/api/stop', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({role: 'tx'})
                });
            }
            
            function stopLoggingRx() {
                fetch('/api/stop', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({role: 'rx'})
                });
            }
            
            // Initialize
            initializeSubcarrierDropdowns();
            updatePlotConfigTx();
            updatePlotConfigRx();
            
            // Update every second
            setInterval(() => {
                updateStatusTx();
                updateStatusRx();
                updateLatestDataTx();
                updateLatestDataRx();
                updateDataLogTx();
                updateDataLogRx();
                updateCharts();
            }, 1000);
            
            // Initial update
            updateStatusTx();
            updateStatusRx();
        </script>
    </body>
    </html>
    '''

# flask stuff

@app.route('/api/status')
def api_status():
    role = request.args.get('role', 'tx')
    logger = logger_tx if role == 'tx' else logger_rx
    if logger:
        return jsonify(logger.get_status())
    return jsonify({'connected': False, 'logging': False, 'packet_count': 0, 'port': '', 'session_id': None, 'session_dir': None})

@app.route('/api/latest')
def api_latest():
    role = request.args.get('role', 'tx')
    logger = logger_tx if role == 'tx' else logger_rx
    if logger:
        return jsonify(logger.get_latest_packet())
    return jsonify({})

@app.route('/api/recent')
def api_recent():
    role = request.args.get('role', 'tx')
    logger = logger_tx if role == 'tx' else logger_rx
    if logger:
        return jsonify(logger.get_recent_data())
    return jsonify([])

@app.route('/api/subcarriers')
def api_subcarriers():
    role = request.args.get('role', 'tx')
    logger = logger_tx if role == 'tx' else logger_rx
    if logger:
        return jsonify(logger.get_available_subcarriers())
    return jsonify([])

@app.route('/api/plot_data')
def api_plot_data():
    role = request.args.get('role', 'tx')
    logger = logger_tx if role == 'tx' else logger_rx
    
    if logger:
        subcarriers_param = request.args.get('subcarriers', '1,5,9,13')
        
        try:
            selected_subcarriers = [int(x.strip()) for x in subcarriers_param.split(',') if x.strip()]
            selected_subcarriers = [sc for sc in selected_subcarriers if 0 <= sc < 128]
            if not selected_subcarriers:
                selected_subcarriers = [1, 5, 9, 13]
        except ValueError:
            selected_subcarriers = [1, 5, 9, 13]
        
        plot_data = logger.get_plot_data(selected_subcarriers)
        return jsonify(plot_data)
    return jsonify({'time': [], 'rssi': [], 'subcarriers': {}})

@app.route('/api/raw')
def api_raw():
    role = request.args.get('role', 'tx')
    logger = logger_tx if role == 'tx' else logger_rx
    if logger:
        return jsonify(logger.get_raw_lines())
    return jsonify([])

@app.route('/api/connect', methods=['POST'])
def api_connect():
    global logger_tx, logger_rx
    data = request.get_json()
    port = data.get('port', 'COM9')
    role = data.get('role', 'tx')
    
    print(f"\n{'='*60}")
    print(f"[API] Connect request: port={port}, role={role}")
    print(f"{'='*60}\n")
    
    # Select the appropriate logger based on role
    if role == 'tx':
        if logger_tx:
            logger_tx.close()
        logger_tx = CSIDataLogger(port)
        success = logger_tx.connect()
        if success:
            try:
                logger_tx.start_logging()
            except Exception as e:
                print(f"[API] Error starting TX logging: {e}")
    else:
        if logger_rx:
            logger_rx.close()
        logger_rx = CSIDataLogger(port)
        success = logger_rx.connect()
        if success:
            try:
                logger_rx.start_logging()
            except Exception as e:
                print(f"[API] Error starting RX logging: {e}")
    
    return jsonify({'success': success, 'port': port, 'role': role})

@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    global logger_tx, logger_rx
    data = request.get_json()
    role = data.get('role', 'tx')
    
    if role == 'tx':
        if logger_tx:
            logger_tx.close()
            logger_tx = None
    else:
        if logger_rx:
            logger_rx.close()
            logger_rx = None
    
    return jsonify({'success': True})

@app.route('/api/start', methods=['POST'])
def api_start():
    data = request.get_json()
    role = data.get('role', 'tx')
    logger = logger_tx if role == 'tx' else logger_rx
    
    if logger:
        success = logger.start_logging()
        return jsonify({'success': success})
    return jsonify({'success': False, 'error': 'Not connected'})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    data = request.get_json()
    role = data.get('role', 'tx')
    logger = logger_tx if role == 'tx' else logger_rx
    
    if logger:
        logger.stop_logging()
        return jsonify({'success': True})
    return jsonify({'success': False})

if __name__ == '__main__':
    try:
        app.run(debug=True, host='0.0.0.0', port=5000)
    finally:
        if logger_tx:
            logger_tx.close()
        if logger_rx:
            logger_rx.close()