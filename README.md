# ESP32 Wi-Fi CSI Data Collection & Real-Time Visualization System

A complete, connectionless **Wi-Fi Channel State Information (CSI)** data collection, multi-node orchestration, and real-time visualization platform built on **ESP32**, **ESP-IDF v5.5.5 (FreeRTOS & ESP-NOW)**, and **Python**.

---

## Key Features

- **Single Unified Firmware Image:** One firmware binary (`main/main.c`) flashes onto every ESP32 board. Each board can be dynamically configured at runtime over USB Serial (`2,000,000 baud`) as an `IDLE` node, `TRANSMITTER` (`TX`), or `RECEIVER` (`RX`) with persistent NVS storage.
- **Connectionless ESP-NOW CSI Acquisition:** Operates directly at the 802.11 MAC layer without requiring a router or Wi-Fi Access Point. Supports high-frequency packet transmission (up to `1000 Hz`) and multi-transmitter time-division multiplexing (TDM).
- **One-Click Windows Setup Wizard (`Easy_Setup_Wizard.bat`):** Self-contained Windows GUI installer that automatically installs Python 3.12, Git, Espressif Installation Manager (`eim`), Silicon Labs CP210x USB-UART drivers, ESP-IDF v5.5.5, Python packages, builds & verifies the firmware (`build/project-name.bin`), and optionally flashes connected ESP32 boards.
- **Central Control GUI (`central_control.py`):** Auto-discovers all connected ESP32 boards over USB, assigns `TRANSMITTER`/`RECEIVER` roles, configures Wi-Fi channel (`1–13`), bandwidth (`20/40 MHz`), transmit rate (`10–1000 Hz`), TX power (`2–20 dBm`), and launches the Web Dashboard in one click.
- **Real-Time Web Dashboard (`web_app.py` + `static/dashboard.html`):** Browser-based live visualization of RSSI, per-subcarrier amplitude (`64 subcarriers`), EMA signal smoothing, fixed/auto Y-axis scaling, fullscreen mode, and automatic CSV session recording.
- **Offline CSI Log Viewer (`csi_log_viewer.py`):** Desktop analyzer for inspecting recorded `.csv` CSI sessions with amplitude/phase heatmaps and subcarrier time-series plots.

---

## Repository Structure

```text
CSI-Data-Collection-Using-ESP32/
├── main/
│   ├── main.c                        # Unified ESP32 FreeRTOS + ESP-NOW CSI firmware
│   ├── CMakeLists.txt                # Firmware component registration
│   └── Kconfig.projbuild             # ESP-IDF menuconfig definitions
├── static/
│   └── dashboard.html                # Real-time Web Dashboard frontend
├── CMakeLists.txt                    # Root ESP-IDF CMake project file
├── sdkconfig.defaults                # Default SDK config (enables CONFIG_ESP_WIFI_CSI_ENABLED=y)
├── Easy_Setup_Wizard.bat             # One-click Windows GUI setup & build wizard
├── central_control.py                # Multi-node ESP32 role & traffic controller GUI
├── web_app.py                        # Flask server & high-speed serial CSI parser
├── csi_log_viewer.py                 # Offline CSV session analyzer & plotter
├── config.json                       # Node configuration template
├── project_installation_guide.md     # Detailed manual installation & troubleshooting guide
└── README.md                         # Project overview and quick-start guide
```

> **Note on `build/`, `sdkconfig`, `.venv/`, and `sessions/`:**  
> Machine-specific build artifacts (`build/`, `sdkconfig`), Python virtual environments (`.venv/`), and recorded datasets (`sessions/`) are intentionally excluded via `.gitignore`. Each machine generates its own `sdkconfig` (from `sdkconfig.defaults`) and `build/` directory during compilation.

---

## Hardware Requirements

- **2× (or more) ESP32 Development Boards** (e.g., ESP32-WROOM-32D / ESP32-DevKitC with Silicon Labs CP210x USB-to-UART bridge)
- **Micro-USB / USB-C Data Cables** (must support data transfer, not charge-only)
- **Windows 10 / 11 PC**

---

## Quick Start (Recommended: Automated Setup Wizard)

On any Windows 10/11 PC (including a freshly formatted machine):

1. Download or clone this repository (or simply download **`Easy_Setup_Wizard.bat`**).
2. Double-click **`Easy_Setup_Wizard.bat`**.
3. Choose your target installation folder using the **Browse...** button and click **Next >**.
4. The wizard will automatically check and install all prerequisites (Python 3.12, Git, CP210x USB drivers, ESP-IDF v5.5.5, Python libraries), compile the firmware (`idf.py build`), and verify `build/project-name.bin`.
5. Plug in your ESP32 boards via USB when prompted to erase and flash the firmware (a confirmation dialog with a warning sign is displayed before flashing).
6. Click **Finish** to launch **Central Control (`central_control.py`)**.

---

## Manual Setup & Usage

For step-by-step manual installation commands and troubleshooting, see **[project_installation_guide.md](project_installation_guide.md)**.

### 1. Build & Flash Firmware (ESP-IDF v5.5.5)

```powershell
# Activate ESP-IDF v5.5.5 environment
. "C:\Espressif\tools\Microsoft.v5.5.5.PowerShell_profile.ps1"

# Build the firmware (generates sdkconfig from sdkconfig.defaults and compiles binaries)
idf.py set-target esp32
idf.py build

# Erase flash (clears stale NVS RF calibration) and flash each ESP32 board
idf.py -p COM9 erase-flash flash
idf.py -p COM11 erase-flash flash
```

### 2. Install Python Dependencies

```powershell
python -m pip install --upgrade pip flask pyserial numpy matplotlib
```

### 3. Configure ESP32 Roles & Start Collecting CSI Data

1. **Assign Node Roles (Central Control):**
   ```powershell
   python central_control.py
   ```
   - Click **Scan & Discover** to detect your ESP32 boards.
   - Set one board to **`RECEIVER`** and the other board(s) to **`TRANSMITTER`**.
   - Click **Apply Configuration (Save to NVS)**.
2. **Launch Real-Time Web Dashboard:**
   - Click **Open Web Dashboard** directly inside Central Control (this automatically releases the Receiver COM port and opens `http://localhost:5000`), **or** close Central Control and run:
     ```powershell
     python web_app.py --port COM11
     ```
3. **Offline Session Analysis:**
   ```powershell
   python csi_log_viewer.py
   ```

---

## Troubleshooting

- **COM Port Busy / Access Denied:** Only one application can hold a serial port open at a time. Close `idf.py monitor` or use the **Open Web Dashboard** button in `central_control.py` so the Receiver port is released before `web_app.py` connects.
- **No CSI Packets (`callbacks=0` / `ESP-NOW status = FAIL`):** Stale RF calibration data in the ESP32 NVS partition can prevent 2.4 GHz transmission after SDK upgrades. Run `idf.py -p COMx erase-flash flash` on both boards and re-apply roles in `central_control.py`.
- **`AttributeError: module 'serial' has no attribute 'Serial'`:** Run `python -m pip uninstall -y serial` followed by `python -m pip install --force-reinstall pyserial`.
