#!/usr/bin/env python3
"""
RF Backscatter CSI Data Capture and Analysis
Captures raw CSI data from ESP32-U receiver and performs signal analysis
"""

import serial
import csv
import argparse
import time
from datetime import datetime
from collections import deque
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

class CSIDataCapture:
    def __init__(self, port="COM10", baud_rate=115200):
        self.port = port
        self.baud_rate = baud_rate
        self.ser = None
        self.csv_file = None
        self.csv_writer = None
        self.packet_count = 0
        self.data_buffer = deque(maxlen=1000)
        
    def connect(self):
        """Connect to serial port"""
        try:
            self.ser = serial.Serial(self.port, self.baud_rate, timeout=1)
            print(f"✓ Connected to {self.port} at {self.baud_rate} baud")
            return True
        except serial.SerialException as e:
            print(f"✗ Failed to connect: {e}")
            return False
    
    def start_logging(self, output_file=None):
        """Start logging CSI data to CSV"""
        if output_file is None:
            output_file = f"csi_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        try:
            self.csv_file = open(output_file, 'w', newline='')
            self.csv_writer = csv.writer(self.csv_file)
            
            # Write header
            self.csv_writer.writerow([
                'packet_num', 'timestamp', 'rssi', 'rate', 'channel', 
                'source_mac', 'csi_len', 'magnitude', 'phase', 'csi_data'
            ])
            self.csv_file.flush()
            
            print(f"✓ Logging to {output_file}")
            return output_file
            
        except IOError as e:
            print(f"✗ Failed to create log file: {e}")
            return None
    
    def parse_csi_line(self, line):
        """Parse CSI_DATA line from ESP32"""
        if not line.startswith('CSI_DATA'):
            return None
        
        try:
            parts = line.split(',', 9)
            if len(parts) < 10:
                return None
            
            # Extract fields
            timestamp = int(parts[1])
            rssi = int(parts[2])
            rate = int(parts[3])
            channel = int(parts[4])
            mac = parts[5]
            csi_len = int(parts[6])
            magnitude = int(parts[7])
            phase = int(parts[8])
            csi_string = parts[9]
            
            # Parse CSI array
            csi_array = eval(csi_string)  # Convert string array to list
            
            return {
                'timestamp': timestamp,
                'rssi': rssi,
                'rate': rate,
                'channel': channel,
                'mac': mac,
                'csi_len': csi_len,
                'magnitude': magnitude,
                'phase': phase,
                'csi_data': csi_array
            }
        except (ValueError, IndexError, SyntaxError):
            return None
    
    def capture(self, duration_sec=None, target_packets=None):
        """Capture CSI data"""
        if not self.connect():
            return False
        
        output_file = self.start_logging()
        if not output_file:
            return False
        
        start_time = time.time()
        
        print("\n" + "="*60)
        print(f"Capturing CSI data... (Ctrl+C to stop)")
        print("="*60)
        
        try:
            while True:
                # Check exit conditions
                if duration_sec and (time.time() - start_time) > duration_sec:
                    print(f"\n✓ Duration limit reached ({duration_sec}s)")
                    break
                
                if target_packets and self.packet_count >= target_packets:
                    print(f"\n✓ Target packets reached ({target_packets})")
                    break
                
                # Read line from serial
                if self.ser.in_waiting:
                    try:
                        line = self.ser.readline().decode('utf-8', errors='ignore').strip()
                        
                        if line.startswith('CSI_DATA'):
                            csi_packet = self.parse_csi_line(line)
                            
                            if csi_packet:
                                self.packet_count += 1
                                self.data_buffer.append(csi_packet)
                                
                                # Write to CSV
                                self.csv_writer.writerow([
                                    self.packet_count,
                                    csi_packet['timestamp'],
                                    csi_packet['rssi'],
                                    csi_packet['rate'],
                                    csi_packet['channel'],
                                    csi_packet['mac'],
                                    csi_packet['csi_len'],
                                    csi_packet['magnitude'],
                                    csi_packet['phase'],
                                    str(csi_packet['csi_data'])
                                ])
                                self.csv_file.flush()
                                
                                # Print status
                                if self.packet_count % 50 == 0:
                                    elapsed = time.time() - start_time
                                    pps = self.packet_count / elapsed
                                    print(f"  [{self.packet_count} packets] "
                                          f"RSSI={csi_packet['rssi']:3d}dBm "
                                          f"Rate={pps:.1f}pps "
                                          f"Time={elapsed:.1f}s")
                        
                        elif line and not line.startswith('[') and not line.startswith('I'):
                            # Print non-CSI output from ESP32
                            print(f"  [ESP32] {line}")
                    
                    except Exception as e:
                        pass  # Skip parsing errors
        
        except KeyboardInterrupt:
            print("\n✓ Capture stopped by user")
        
        finally:
            self.close()
        
        print("\n" + "="*60)
        print(f"✓ Capture complete: {self.packet_count} packets")
        print(f"✓ Output file: {output_file}")
        print("="*60 + "\n")
        
        return output_file
    
    def close(self):
        """Clean up resources"""
        if self.csv_file:
            self.csv_file.close()
        if self.ser:
            self.ser.close()


class CSIAnalyzer:
    """Analyze captured CSI data"""
    
    @staticmethod
    def load_csv(filename):
        """Load CSI data from CSV file"""
        data = {
            'timestamp': [],
            'rssi': [],
            'rate': [],
            'channel': [],
            'mac': [],
            'csi_len': [],
            'magnitude': [],
            'phase': [],
            'csi_data': []
        }
        
        with open(filename, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                data['timestamp'].append(int(row['timestamp']))
                data['rssi'].append(int(row['rssi']))
                data['rate'].append(int(row['rate']))
                data['channel'].append(int(row['channel']))
                data['mac'].append(row['source_mac'])
                data['csi_len'].append(int(row['csi_len']))
                data['magnitude'].append(int(row['magnitude']))
                data['phase'].append(int(row['phase']))
                data['csi_data'].append(eval(row['csi_data']))
        
        return data
    
    @staticmethod
    def plot_rssi(data, title="RSSI vs Time"):
        """Plot RSSI variation"""
        plt.figure(figsize=(12, 5))
        plt.plot(data['rssi'], linewidth=0.5, alpha=0.7)
        plt.xlabel('Packet Number')
        plt.ylabel('RSSI (dBm)')
        plt.title(title)
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        return plt.gcf()
    
    @staticmethod
    def plot_phase(data, title="Phase vs Time"):
        """Plot phase variation"""
        plt.figure(figsize=(12, 5))
        plt.plot(data['phase'], linewidth=0.5, alpha=0.7, color='orange')
        plt.xlabel('Packet Number')
        plt.ylabel('Phase (degrees)')
        plt.title(title)
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        return plt.gcf()
    
    @staticmethod
    def plot_histogram(data, title="RSSI Distribution"):
        """Plot RSSI histogram"""
        plt.figure(figsize=(10, 5))
        plt.hist(data['rssi'], bins=30, alpha=0.7, edgecolor='black')
        plt.xlabel('RSSI (dBm)')
        plt.ylabel('Count')
        plt.title(title)
        plt.grid(True, alpha=0.3, axis='y')
        plt.tight_layout()
        return plt.gcf()
    
    @staticmethod
    def compare_datasets(baseline, test, title="CSI Comparison"):
        """Compare baseline and test measurements"""
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        
        # RSSI comparison
        axes[0, 0].plot(baseline['rssi'], label='Baseline', alpha=0.7, linewidth=0.5)
        axes[0, 0].plot(test['rssi'], label='Test', alpha=0.7, linewidth=0.5)
        axes[0, 0].set_xlabel('Packet Number')
        axes[0, 0].set_ylabel('RSSI (dBm)')
        axes[0, 0].set_title('RSSI Comparison')
        axes[0, 0].legend()
        axes[0, 0].grid(True, alpha=0.3)
        
        # Phase comparison
        axes[0, 1].plot(baseline['phase'], label='Baseline', alpha=0.7, linewidth=0.5)
        axes[0, 1].plot(test['phase'], label='Test', alpha=0.7, linewidth=0.5)
        axes[0, 1].set_xlabel('Packet Number')
        axes[0, 1].set_ylabel('Phase (degrees)')
        axes[0, 1].set_title('Phase Comparison')
        axes[0, 1].legend()
        axes[0, 1].grid(True, alpha=0.3)
        
        # RSSI histograms
        axes[1, 0].hist(baseline['rssi'], bins=30, alpha=0.5, label='Baseline', edgecolor='black')
        axes[1, 0].hist(test['rssi'], bins=30, alpha=0.5, label='Test', edgecolor='black')
        axes[1, 0].set_xlabel('RSSI (dBm)')
        axes[1, 0].set_ylabel('Count')
        axes[1, 0].set_title('RSSI Distribution')
        axes[1, 0].legend()
        axes[1, 0].grid(True, alpha=0.3, axis='y')
        
        # Statistics
        axes[1, 1].axis('off')
        stats_text = f"""
STATISTICS COMPARISON

Baseline:
  Mean RSSI: {np.mean(baseline['rssi']):.2f} dBm
  Std Dev:   {np.std(baseline['rssi']):.2f} dBm
  Min/Max:   {np.min(baseline['rssi'])}/{np.max(baseline['rssi'])} dBm
  
Test:
  Mean RSSI: {np.mean(test['rssi']):.2f} dBm
  Std Dev:   {np.std(test['rssi']):.2f} dBm
  Min/Max:   {np.min(test['rssi'])}/{np.max(test['rssi'])} dBm
  
Difference:
  RSSI Δ: {(np.mean(test['rssi']) - np.mean(baseline['rssi'])):.2f} dBm
  Phase Δ: {(np.mean(test['phase']) - np.mean(baseline['phase'])):.2f}°
        """
        axes[1, 1].text(0.1, 0.5, stats_text, fontfamily='monospace', fontsize=10, 
                       verticalalignment='center', transform=axes[1, 1].transAxes)
        
        fig.suptitle(title, fontsize=14, fontweight='bold')
        plt.tight_layout()
        return fig


def main():
    parser = argparse.ArgumentParser(description='RF Backscatter CSI Data Capture')
    parser.add_argument('--port', default='COM10', help='Serial port (default: COM10)')
    parser.add_argument('--baud', type=int, default=115200, help='Baud rate (default: 115200)')
    parser.add_argument('--duration', type=int, help='Capture duration in seconds')
    parser.add_argument('--packets', type=int, help='Target number of packets')
    parser.add_argument('--output', help='Output CSV filename')
    parser.add_argument('--analyze', action='store_true', help='Analyze after capture')
    parser.add_argument('--compare', nargs=2, help='Compare two CSV files')
    
    args = parser.parse_args()
    
    if args.compare:
        # Compare mode
        print("Loading baseline file...")
        baseline = CSIAnalyzer.load_csv(args.compare[0])
        print(f"Loaded {len(baseline['rssi'])} packets")
        
        print("Loading test file...")
        test = CSIAnalyzer.load_csv(args.compare[1])
        print(f"Loaded {len(test['rssi'])} packets")
        
        # Generate comparison plot
        fig = CSIAnalyzer.compare_datasets(baseline, test, 
                                          title=f"Comparison: {args.compare[0]} vs {args.compare[1]}")
        plt.savefig('csi_comparison.png', dpi=150)
        print("✓ Saved comparison plot to csi_comparison.png")
        plt.show()
    
    else:
        # Capture mode
        capturer = CSIDataCapture(port=args.port, baud_rate=args.baud)
        output_file = capturer.capture(
            duration_sec=args.duration,
            target_packets=args.packets
        )
        
        if args.analyze and output_file:
            print("\nAnalyzing captured data...")
            data = CSIAnalyzer.load_csv(output_file)
            
            # Generate plots
            print("Generating plots...")
            fig1 = CSIAnalyzer.plot_rssi(data)
            fig1.savefig('rssi_timeline.png', dpi=150)
            
            fig2 = CSIAnalyzer.plot_histogram(data)
            fig2.savefig('rssi_histogram.png', dpi=150)
            
            fig3 = CSIAnalyzer.plot_phase(data)
            fig3.savefig('phase_timeline.png', dpi=150)
            
            print("✓ Plots saved:")
            print("  - rssi_timeline.png")
            print("  - rssi_histogram.png")
            print("  - phase_timeline.png")
            
            plt.show()


if __name__ == '__main__':
    main()
