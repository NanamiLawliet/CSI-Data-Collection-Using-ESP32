# RF Backscatter Sensing with ESP32-U - CSI Capture

## Overview

This project implements **connectionless RF sensing** using two ESP32-U boards with external U.FL antennas to measure backscatter effects and material interactions with WiFi signals.

**Key Improvements:**
- ✅ No WiFi connection required - raw 802.11 frame transmission
- ✅ Promiscuous mode CSI capture on receiver
- ✅ High-rate packet transmission (configurable 10-100ms interval)
- ✅ Disabled WiFi power saving for signal stability
- ✅ Maximum TX power configuration for ESP32-U
- ✅ Raw I/Q CSI data output in CSV format
- ✅ Optimized for RF sensing experiments

---

## Hardware Setup

### Equipment
- 2x **ESP32-U** boards (with U.FL connector)
- 2x External WiFi antennas (2.4 GHz)
- USB cables for programming
- Test material (metal, dielectric, etc.)

### Physical Layout
```
┌─────────────────────────────────────────────┐
│  TX (COM9)                                  │
│  68:fe:71:0b:a4:00                         │
│  Antenna ↑                                  │
└────────────────────────────────────────────┘
           ↕ 802.11 frames
         [TEST MATERIAL]
           ↕ Backscatter/Multipath
┌────────────────────────────────────────────┐
│  (RX) COM10                                 │
│  08:d1:f9:f6:7c:ec                         │
│  Antenna ↑                                  │
└─────────────────────────────────────────────┘
```

---

## Configuration Parameters

Edit `main/main.c` to adjust:

```c
// Role selection (line 17)
#define DEVICE_ROLE_TRANSMITTER 1  // 0=RX, 1=TX

// WiFi Channel (fixed for both devices)
#define WIFI_CHANNEL 1             // 1-13 for 2.4GHz

// TX Broadcast interval
#define TX_BROADCAST_INTERVAL_MS 50  // 10ms=100pps, 50ms=20pps, 100ms=10pps

// Maximum TX Power
#define MAX_TX_POWER 20            // dBm (0-20 for ESP32)
```

---

## Build & Flash Instructions

### 1. Configure MenuConfig for CSI Support

```bash
idf.py menuconfig
```

Navigate to:
```
Component config → Wi-Fi → Enable CSI
```

Then enable:
```
- [x] Enable CSI
```

### 2. For TRANSMITTER (COM9)

Set line 17 to:
```c
#define DEVICE_ROLE_TRANSMITTER 1
```

Build and flash:
```bash
idf.py set-target esp32
idf.py build
idf.py -p COM9 flash
```

Monitor output:
```bash
idf.py -p COM9 monitor
```

**Expected Output:**
```
DEVICE ROLE: TRANSMITTER
TX broadcast task created
TX packet 1 sent
TX packet 2 sent
...
```

### 3. For RECEIVER (COM10)

Set line 17 to:
```c
#define DEVICE_ROLE_TRANSMITTER 0
```

Build and flash:
```bash
idf.py set-target esp32
idf.py build
idf.py -p COM10 flash
```

Monitor output:
```bash
idf.py -p COM10 monitor
```

**Expected Output:**
```
DEVICE ROLE: RECEIVER
Promiscuous mode enabled
CSI capture enabled
CSI_DATA,1234,−45,6,1,68:fe:71:0b:a4:00,256,45,−30,"[1,2,3,...,128]"
CSI_DATA,1235,−44,6,1,68:fe:71:0b:a4:00,256,44,−29,"[2,3,4,...,129]"
```

---

## Data Output Format

### CSV Format (Receiver Output)
```
CSI_DATA,timestamp,rssi,rate,channel,source_mac,csi_len,magnitude,phase,"[I/Q_values]"
```

**Fields:**
- `timestamp`: Microsecond timestamp from ESP32 timer
- `rssi`: Received Signal Strength Indicator (dBm)
- `rate`: Data rate (6=6Mbps, 11=11Mbps, etc.)
- `channel`: WiFi channel (1-13)
- `source_mac`: Source MAC address (hex)
- `csi_len`: Number of CSI subcarriers
- `magnitude`: Magnitude estimate (dBm)
- `phase`: Phase estimate (degrees)
- `[I/Q_values]`: Raw CSI I/Q components for all subcarriers

### Example Output
```
CSI_DATA,12453000,-45,6,1,68:fe:71:0b:a4:00,256,42,-25,"[12,-8,15,-6,8,-10,...]"
CSI_DATA,12504000,-44,6,1,68:fe:71:0b:a4:00,256,41,-24,"[11,-7,14,-5,7,-9,...]"
CSI_DATA,12555000,-43,6,1,68:fe:71:0b:a4:00,256,40,-23,"[10,-6,13,-4,6,-8,...]"
```

---

## Python Data Capture Script

```python
import serial
import csv
from datetime import datetime

# Configuration
RX_PORT = "COM10"
BAUD_RATE = 115200
OUTPUT_FILE = f"csi_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

def capture_csi_data(port, output_file, duration_sec=60):
    """Capture CSI data from receiver"""
    ser = serial.Serial(port, BAUD_RATE, timeout=1)
    
    with open(output_file, 'w', newline='') as f:
        writer = csv.writer(f)
        # Write header
        writer.writerow(['timestamp', 'rssi', 'rate', 'channel', 'mac', 
                        'csi_len', 'magnitude', 'phase', 'csi_data'])
        
        start_time = time.time()
        packet_count = 0
        
        while time.time() - start_time < duration_sec:
            try:
                line = ser.readline().decode().strip()
                
                if line.startswith('CSI_DATA'):
                    parts = line.split(',', 8)
                    if len(parts) >= 9:
                        # Extract fields
                        timestamp = parts[1]
                        rssi = parts[2]
                        rate = parts[3]
                        channel = parts[4]
                        mac = parts[5]
                        csi_len = parts[6]
                        magnitude = parts[7]
                        phase = parts[8]
                        csi_data = parts[9] if len(parts) > 9 else ""
                        
                        # Write row
                        writer.writerow([timestamp, rssi, rate, channel, mac, 
                                       csi_len, magnitude, phase, csi_data])
                        f.flush()
                        
                        packet_count += 1
                        if packet_count % 10 == 0:
                            print(f"Captured {packet_count} packets...")
                            
            except Exception as e:
                print(f"Error: {e}")
                continue
    
    ser.close()
    print(f"Saved {packet_count} packets to {output_file}")

if __name__ == "__main__":
    import time
    capture_csi_data(RX_PORT, OUTPUT_FILE, duration_sec=300)  # 5 minutes
```

---

## Troubleshooting

### TX Not Sending Packets
**Problem:** "TX packet X sent" not appearing in logs
- Check WIFI_CHANNEL matches on both devices
- Verify TX power setting (should be 1-20)
- Ensure CSI is enabled in menuconfig

**Solution:**
```bash
# Rebuild with CSI enabled
idf.py menuconfig  # Enable CSI
idf.py clean
idf.py build
idf.py -p COM9 flash
```

### RX Not Receiving CSI Data
**Problem:** No "CSI_DATA" lines in receiver output
- Check both devices on same channel (line 18)
- Verify promiscuous mode is enabled
- Confirm CSI callback is registered

**Check logs:**
```
idf.py -p COM10 monitor | grep -E "CSI|promiscuous|callback"
```

### Low RSSI Values
**Problem:** RSSI around -80 to -90 dBm (too weak)
- Reduce distance between antennas
- Check external antenna connections (U.FL connectors)
- Verify antenna orientation (perpendicular typically works best)
- Increase TX power: `#define MAX_TX_POWER 20`

---

## Experiment Workflow

### Baseline Measurement (No Object)
1. Start RX (COM10) logging to CSV
2. Start TX (COM9)
3. Capture 30-60 seconds of clean channel data
4. Save baseline file: `baseline_ch1.csv`

### Material Test
1. Place test material (metal sheet, cardboard, water, etc.) between antennas
2. Keep all equipment positions fixed
3. Capture another 30-60 seconds
4. Save test file: `test_metal_ch1.csv`

### Analysis
```python
import pandas as pd
import numpy as np

baseline = pd.read_csv('baseline_ch1.csv')
test = pd.read_csv('test_metal_ch1.csv')

# Compare RSSI
print(f"Baseline RSSI avg: {baseline['rssi'].mean():.2f} dBm")
print(f"Test RSSI avg: {test['rssi'].mean():.2f} dBm")
print(f"Difference: {(test['rssi'].mean() - baseline['rssi'].mean()):.2f} dBm")

# Plot CSI magnitude variation
import matplotlib.pyplot as plt

plt.figure(figsize=(12, 6))
plt.subplot(1, 2, 1)
plt.plot(baseline['magnitude'], label='Baseline', alpha=0.7)
plt.plot(test['magnitude'], label='With Material', alpha=0.7)
plt.xlabel('Packet Number')
plt.ylabel('CSI Magnitude (dBm)')
plt.legend()

plt.subplot(1, 2, 2)
plt.plot(baseline['phase'], label='Baseline', alpha=0.7)
plt.plot(test['phase'], label='With Material', alpha=0.7)
plt.xlabel('Packet Number')
plt.ylabel('Phase (degrees)')
plt.legend()

plt.tight_layout()
plt.savefig('csi_comparison.png')
```

---

## Performance Specifications

| Parameter | Value | Notes |
|-----------|-------|-------|
| WiFi Standard | 802.11 b/g/n | 2.4 GHz only |
| TX Rate | 10-100 pps | Configurable interval |
| RX Sensitivity | -90 dBm typical | With external antenna |
| CSI Subcarriers | 128 | For 20MHz BW |
| Channel Bandwidth | 20 MHz | Fixed |
| CSI Capture Rate | ~100 Hz | Depends on TX rate |
| Output Format | CSV | Easy Python parsing |

---

## Advanced Configuration

### Change WiFi Channel
Edit `main/main.c` line 18:
```c
#define WIFI_CHANNEL 6  // Options: 1-13
```

### Increase TX Rate
Edit `main/main.c` line 19:
```c
#define TX_BROADCAST_INTERVAL_MS 10  // 10ms = 100 packets/sec
```

### Enable Detailed Logging
```bash
idf.py -p COM10 monitor -v
```

### Capture Raw Serial Data
```bash
# Redirect to file
idf.py -p COM10 monitor > receiver_output.log &
idf.py -p COM9 monitor > transmitter_output.log &
```

---

## CSI Data Interpretation

The CSI output contains raw I/Q components:
- **I (In-phase):** Even-indexed values in array
- **Q (Quadrature):** Odd-indexed values in array

**Magnitude Calculation:**
```
magnitude = sqrt(I^2 + Q^2)
```

**Phase Calculation:**
```
phase = atan2(Q, I) * 180/pi
```

**Multipath Detection:**
- Large phase variations → Strong multipath
- Sudden RSSI drops → Object blocking
- Phase discontinuities → Channel state changes

---

## References

- [ESP-IDF WiFi Documentation](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/network/esp_wifi.html)
- [CSI API Guide](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-guides/wifi.html#channel-state-information)
- [ESP32 Hardware Design Guide](https://www.espressif.com/sites/default/files/documentation/esp32_hardware_design_guidelines_en.pdf)

---

## License

Academic/Research use. Cite if published.
