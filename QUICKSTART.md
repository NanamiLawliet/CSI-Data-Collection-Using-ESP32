# Quick Start Guide - RF Backscatter CSI Capture

## 5-Minute Setup

### Prerequisites
- 2x ESP32-U boards with external antennas
- USB cables for serial connection
- ESP-IDF installed and working
- Python 3.7+ with `pyserial`, `numpy`, `matplotlib`

### Step 1: Enable CSI in MenuConfig (One-time)

```bash
cd ~/esp32/CSI-Data-Collection-Using-ESP32
idf.py menuconfig
```

Navigate: `Component config` → `Wi-Fi` → ✅ `Enable CSI` → Save & Exit

---

### Step 2: Build for TRANSMITTER (COM9)

**1a. Edit main/main.c line 17:**
```c
#define DEVICE_ROLE_TRANSMITTER 1   // TX mode
```

**1b. Build & Flash:**
```bash
idf.py set-target esp32
idf.py build
idf.py -p COM9 erase_flash
idf.py -p COM9 flash
```

**1c. Verify TX is Running:**
```bash
idf.py -p COM9 monitor
# Look for: "TX packet X sent" messages
```

---

### Step 3: Build for RECEIVER (COM10)

**2a. Edit main/main.c line 17:**
```c
#define DEVICE_ROLE_TRANSMITTER 0   // RX mode
```

**2b. Build & Flash:**
```bash
idf.py build
idf.py -p COM10 erase_flash
idf.py -p COM10 flash
```

**2c. Verify RX is Receiving:**
```bash
idf.py -p COM10 monitor
# Look for: "CSI_DATA,..." lines with RSSI values
```

---

### Step 4: Capture CSI Data

**Option A: Using Python Script (Recommended)**
```bash
# Install dependencies
pip install pyserial numpy matplotlib

# Capture for 60 seconds
python csi_capture.py --port COM10 --duration 60 --analyze

# Compare two measurements
python csi_capture.py --compare baseline.csv test_with_metal.csv
```

**Option B: Using Serial Monitor**
```bash
# Terminal 1: Monitor transmitter
idf.py -p COM9 monitor > tx.log &

# Terminal 2: Monitor receiver
idf.py -p COM10 monitor > rx.log

# Wait 60+ seconds, then Ctrl+C to stop
# rx.log now contains all CSI data
```

---

## Typical Workflow

### 1️⃣ Baseline Measurement (No Object)
```bash
# Both boards running
python csi_capture.py --port COM10 --duration 60 --output baseline_ch1.csv --analyze

# Output: baseline_ch1.csv with RSSI/Phase data
```

### 2️⃣ Test Measurement (With Material)
```bash
# Place metal/dielectric between antennas
python csi_capture.py --port COM10 --duration 60 --output test_metal_ch1.csv --analyze

# Output: test_metal_ch1.csv
```

### 3️⃣ Compare Results
```bash
python csi_capture.py --compare baseline_ch1.csv test_metal_ch1.csv

# Generates: csi_comparison.png with statistics
```

---

## Adjusting Experiment Parameters

### Increase TX Rate (More Packets/sec)
Edit `main/main.c` line 19:
```c
#define TX_BROADCAST_INTERVAL_MS 10  // 10ms = 100 pps
#define TX_BROADCAST_INTERVAL_MS 50  // 50ms = 20 pps (default)
#define TX_BROADCAST_INTERVAL_MS 100 // 100ms = 10 pps
```

### Change WiFi Channel
Edit `main/main.c` line 18:
```c
#define WIFI_CHANNEL 1   // Must be same on TX & RX
// Options: 1-13 for 2.4GHz, choose less congested channel
```

### Increase TX Power
Edit `main/main.c` line 20:
```c
#define MAX_TX_POWER 20  // 0-20 dBm (higher = stronger signal)
```

Then rebuild both TX and RX.

---

## Output Format

### CSV Structure
```
packet_num, timestamp, rssi, rate, channel, source_mac, csi_len, magnitude, phase, csi_data
```

**Example Row:**
```
1, 12453000, -45, 6, 1, 68:fe:71:0b:a4:00, 256, 42, -25, "[12,-8,15,-6,...]"
```

### Real-time Output
```
CSI_DATA,12453000,−45,6,1,68:fe:71:0b:a4:00,256,42,−25,"[12,−8,15,−6,8,−10,...]"
CSI_DATA,12504000,−44,6,1,68:fe:71:0b:a4:00,256,41,−24,"[11,−7,14,−5,7,−9,...]"
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "TX packet not sending" | Check WIFI_CHANNEL same on both, rebuild TX |
| "No CSI_DATA output on RX" | Check CSI enabled in menuconfig, rebuild RX |
| "RSSI too weak (-85 dBm)" | Move antennas closer, check U.FL connections |
| "Serial port not found" | Check COM port with `mode` (Windows) or `ls /dev/tty*` (Linux) |
| "Baud rate error" | Ensure 115200 matches TX/RX and terminal |

---

## Key Files

```
main/
  └── main.c                    # Source code (edit line 17 for role)
  
RF_BACKSCATTER_SETUP.md         # Full technical documentation
TRANSMITTER_RECEIVER_SETUP.md   # Original connected WiFi setup (deprecated)
csi_capture.py                  # Python data capture & analysis tool
```

---

## Performance Tips

1. **Stable Baseline:** Capture 60+ seconds of baseline data in absence of moving objects
2. **Fixed Position:** Keep antenna positions constant between measurements
3. **Clean Channel:** Run at least 3 meters from WiFi routers
4. **Perpendicular Antennas:** Position antennas perpendicular to each other for best CSI
5. **CSI Verification:** Always check for RSSI values in -40 to -70 dBm range (too strong/weak = poor CSI quality)

---

## Testing Material Detection

1. **Metal (Reflective):**
   - Expect -10 to -20 dBm RSSI drop
   - Significant phase shift
   - Multipath signature in CSI

2. **Dielectric (Absorbing):**
   - Expect -5 to -10 dBm RSSI drop
   - Moderate phase shift
   - Reduced CSI magnitude

3. **Human (Complex):**
   - Variable -10 to -30 dBm depending on body part
   - Random phase variations
   - Non-stationary CSI data

---

## Next Steps

- ✅ Read `RF_BACKSCATTER_SETUP.md` for full technical details
- ✅ Experiment with different WiFi channels (line 18)
- ✅ Try different TX rates (line 19)
- ✅ Analyze CSI subcarrier patterns
- ✅ Implement machine learning for material classification

---

**Questions?** Check the detailed documentation in `RF_BACKSCATTER_SETUP.md`
