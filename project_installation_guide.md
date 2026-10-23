# CSI Data Collection Using ESP32 — Complete Technical Installation & Setup Guide

This document provides a comprehensive, step-by-step technical guide for setting up, building, flashing, and running the **CSI-Data-Collection-Using-ESP32** system from scratch on a **freshly formatted Windows machine** with no prior development tools (no C/C++ compilers, no Python, no Git, and no ESP-IDF installed).

---

## 1. System Architecture & Prerequisites

The system consists of embedded firmware running on ESP32 microcontrollers and host-side Python applications for device role management, real-time visualization, and CSV data logging.

### 1.1. Programming Languages Used
| Language | Role in the Project |
| :--- | :--- |
| **C (ISO C11 / GNU11)** | ESP32 embedded firmware (`main/main.c`), FreeRTOS tasks, ESP-NOW protocol handling, Wi-Fi CSI capture callbacks, UART command parsing, and NVS flash storage. |
| **C++ (GNU++23)** | ESP-IDF internal components and runtime libraries. |
| **Python (3.12)** | Host applications: Central Control GUI (`central_control.py`), Real-Time Web Dashboard backend (`web_app.py`), Standalone Log Viewer (`csi_log_viewer.py`), and ESP-IDF build/flash tools (`idf.py`, `esptool.py`). |
| **HTML5 / CSS3 / JavaScript** | Real-time interactive CSI visualization frontend (`static/dashboard.html`). |
| **CMake & Batch / PowerShell** | Build system configuration (`CMakeLists.txt`) and environment/launcher automation scripts (`.bat`, `.ps1`). |

### 1.2. Frameworks, Compilers & Toolchain
* **Embedded Framework:** **Espressif ESP-IDF v5.5.5** (includes FreeRTOS, ESP-WiFi, ESP-NOW, LWIP TCP/IP stack, NVS Flash, and UART VFS drivers).
* **Installer / Version Manager:** **ESP-IDF Installation Manager CLI (`eim` v0.20.1+)**.
* **C/C++ Cross-Compiler Toolchain:** **Xtensa ESP32 ELF GCC 14.2.0** (`xtensa-esp-elf-gcc` / `xtensa-esp32-elf-g++`, release `esp-14.2.0_20260121`).
* **Language Server / Static Analyzer:** **ESP-Clang** (`esp-19.1.2_20250312` / `clangd`).
* **Build System Tools:**
  * **CMake:** Build generator.
  * **Ninja:** Fast parallel build executor.
  * **ccache:** Compiler cache to accelerate subsequent rebuilds to a few seconds.
* **Flashing & Debugging Tools:**
  * **esptool.py (v4.12+):** Flashes compiled binary images (`bootloader.bin`, `partition-table.bin`, `project-name.bin`) to ESP32 SPI flash over USB-UART.
  * **OpenOCD-ESP32:** On-chip debugging support.

### 1.3. Host Python Libraries (System Python Environment)
While ESP-IDF creates its own isolated Python virtual environment (`C:\Espressif\tools\python\v5.5.5\venv`) for firmware compilation, the host GUI/Web tools (`web_app.py`, `central_control.py`, `csi_log_viewer.py`) run on the **system Python** (`py` / `python`) and require:
* `pyserial`: Bidirectional USB-UART serial communication with ESP32 boards.
* `flask`: HTTP/REST web server powering the real-time dashboard (`http://localhost:5000`).
* `tkinter`: Standard Python GUI toolkit (used by `central_control.py` and `csi_log_viewer.py`).
* `numpy` & `matplotlib`: Numerical processing and plotting for CSI amplitude/phase analysis.

### 1.4. Hardware & Driver Requirements
* **At least 2x ESP32 Development Boards** (e.g., ESP32-WROOM or ESP32-U with external antenna; one configured as `RECEIVER` and one or more configured as `TRANSMITTER`).
* **Data-Capable USB Cables** (Micro-USB or USB-C depending on the board; ensure they are not charge-only cables).
* **USB-to-UART Bridge Drivers:** **Silicon Labs CP210x** or **CH340/CH341** Windows driver, depending on the USB-serial chip on your ESP32 board.

---

## 2. Step-by-Step Installation Instructions (From a Clean Windows PC)

### Step 1: Enable PowerShell Script Execution
On a freshly installed Windows system, PowerShell blocks `.ps1` scripts by default. Because ESP-IDF uses a PowerShell profile script to activate its environment, open **PowerShell** and run:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force
```

---

### Step 2: Install Python 3.12, Git, and Espressif Installation Manager (`eim`)
Use Windows Package Manager (`winget`) to install the core prerequisites.

#### 2.1. Install Python 3.12
```powershell
winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
```
> **Note:** After installation, restart your terminal (or refresh `PATH`) and verify that `python --version` and `py --version` return `Python 3.12.x`.

#### 2.2. Install Git
To install the standard Git for Windows package (will prompt for Administrator permission):
```powershell
winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
```
*(Alternatively, if installing without Administrator privileges, download `MinGit` or `PortableGit` from GitHub, extract it to `%LOCALAPPDATA%\Programs\Git`, and add `%LOCALAPPDATA%\Programs\Git\cmd` to your User `Path` environment variable.)*

#### 2.3. Install Espressif Installation Manager CLI (`eim`)
```powershell
winget install --id Espressif.EIM-CLI -e --source winget --accept-package-agreements --accept-source-agreements
```

#### 2.4. Refresh Environment Variables in the Current PowerShell Session
To continue without restarting PowerShell:
```powershell
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
```

---

### Step 3: Install Required Host Python Packages
Install the Python libraries required by the Web Dashboard, Central Control GUI, and analysis scripts into the **system Python** environment:

```powershell
python -m pip install --upgrade pip flask pyserial numpy matplotlib
```

Verify that all libraries (including `tkinter`) are properly installed (`ALL_IMPORTS_OK` should be printed):
```powershell
python -c "import tkinter, serial, flask, numpy, matplotlib; print('ALL_IMPORTS_OK')"
```

---

### Step 4: Install ESP-IDF v5.5.5 and the C/C++ Toolchain (`xtensa-esp-elf`)
This project is built and tested against **ESP-IDF v5.5.5** targeting the `esp32` architecture. Run the following `eim` command to automatically download and configure ESP-IDF v5.5.5, the Xtensa GCC 14.2.0 C/C++ compiler, CMake, Ninja, ccache, and the isolated ESP-IDF Python virtual environment inside `C:\Espressif`:

```powershell
eim install -n true -p "C:\Espressif" -i v5.5.5 -t esp32 --create-bat-activation-script true -v
```

What this command does:
1. Clones ESP-IDF `v5.5.5` and all required submodules into `C:\Espressif\v5.5.5\esp-idf`.
2. Downloads and extracts `xtensa-esp-elf` (`esp-14.2.0_20260121`), `esp-clang`, `cmake`, `ninja`, `openocd-esp32`, `idf-exe`, `ccache`, and `esp-rom-elfs` into `C:\Espressif\tools`.
3. Creates the ESP-IDF Python virtual environment at `C:\Espressif\tools\python\v5.5.5\venv` and installs all IDF Python dependencies.
4. Generates the PowerShell activation script at `C:\Espressif\tools\Microsoft.v5.5.5.PowerShell_profile.ps1`.

> **Optional Path Compatibility Junction:**  
> If any legacy `.vscode` configuration references `C:\Espressif\frameworks\esp-idf-v5.5.5-2`, you can create a directory junction so both paths resolve identically:
> ```powershell
> New-Item -ItemType Directory -Force -Path "C:\Espressif\frameworks" | Out-Null
> New-Item -ItemType Junction -Path "C:\Espressif\frameworks\esp-idf-v5.5.5-2" -Target "C:\Espressif\v5.5.5\esp-idf"
> ```

---

### Step 5: Install Visual Studio Code and Extensions
Install Visual Studio Code (if not already installed) along with the official ESP-IDF, C/C++, and Python extensions:

```powershell
# Install VS Code (if not already installed):
winget install --id Microsoft.VisualStudioCode -e --source winget --accept-package-agreements --accept-source-agreements

# Install required VS Code extensions:
code --install-extension espressif.esp-idf-extension --force
code --install-extension ms-vscode.cpptools --force
code --install-extension ms-python.python --force
```

---

### Step 6: Clone the GitHub Repository
Create the project directory under `Desktop\apps\projects` and clone the repository:

```powershell
New-Item -ItemType Directory -Force -Path "C:\Users\$env:USERNAME\Desktop\apps\projects" | Out-Null
git clone "https://github.com/NanamiLawliet/CSI-Data-Collection-Using-ESP32.git" "C:\Users\$env:USERNAME\Desktop\apps\projects\CSI-Data-Collection-Using-ESP32"
```

---

### Step 7: Build the ESP32 Firmware (`idf.py build`)
Navigate to the project directory, activate the ESP-IDF v5.5.5 environment, set the target to `esp32`, and compile the firmware:

```powershell
Set-Location "C:\Users\$env:USERNAME\Desktop\apps\projects\CSI-Data-Collection-Using-ESP32"
. "C:\Espressif\tools\Microsoft.v5.5.5.PowerShell_profile.ps1"
idf.py set-target esp32
idf.py build
```

* `sdkconfig.defaults` automatically enables Wi-Fi CSI (`CONFIG_ESP_WIFI_CSI_ENABLED=y`) in the generated `sdkconfig`.
* Once compilation succeeds, the following artifacts are generated inside `build/`:
  * `build/bootloader/bootloader.bin`
  * `build/partition_table/partition-table.bin`
  * `build/project-name.bin`
  * `build/compile_commands.json` (used by VS Code C/C++ IntelliSense)

---

## 3. Flashing ESP32 Boards & Running the System

### Step 8: Flash the Generic Firmware to All ESP32 Boards
This project uses a **single unified (generic) firmware image** for all ESP32 boards. Every board boots into `IDLE` mode by default until its role (`RECEIVER` or `TRANSMITTER`) is dynamically assigned via the Central Control GUI and saved to non-volatile storage (NVS).

1. Plug your ESP32 boards into the computer via USB.
2. List the detected Serial COM ports:
   ```powershell
   py -c "import serial.tools.list_ports; print('\n'.join(f'{p.device}: {p.description}' for p in serial.tools.list_ports.comports()))"
   ```
3. In an activated ESP-IDF PowerShell window (or using `5_Flash_ESP32.bat`), flash each board (e.g., `COM9` and `COM10`):
   ```powershell
   idf.py -p COM9 flash
   idf.py -p COM10 flash
   ```
   > **Important:** Do **NOT** leave `idf.py monitor` running after flashing. Only one program can hold a Windows COM port open at a time, and an active serial monitor will block `central_control.py` and `web_app.py` from connecting.

---

### Step 9: Assign Device Roles Using the Central Control GUI
Once the boards are flashed and connected via USB:

```powershell
cd "C:\Users\$env:USERNAME\Desktop\apps\projects\CSI-Data-Collection-Using-ESP32"
py central_control.py
```

1. The GUI automatically scans all COM ports and displays each discovered ESP32 along with its MAC address, COM port, and current role (`IDLE`, `RECEIVER`, or `TRANSMITTER`).
2. **Configure the Receiver Board:**
   * Select your first ESP32 in the table.
   * Choose **`RECEIVER`** from the **Role** dropdown.
   * Click **Apply Configuration**. The board saves the role to NVS and reboots into Receiver mode.
3. **Configure the Transmitter Board(s):**
   * Select your second ESP32 in the table.
   * Choose **`TRANSMITTER`** from the **Role** dropdown.
   * Select the **Receiver's MAC address** from the **Target Peer MAC** dropdown.
   * Select a **Transmitter ID** (`1`, `2`, etc.).
   * Click **Apply Configuration**. The board saves the settings to NVS and begins transmitting ESP-NOW frames to the Receiver.
4. Close the Central Control GUI before launching the Web Dashboard or Log Viewer so the serial ports are released.

---

### Step 10: Visualize and Record Real-Time CSI Data

#### Option A: Real-Time Web Dashboard (`web_app.py`) — *Recommended*
```powershell
cd "C:\Users\$env:USERNAME\Desktop\apps\projects\CSI-Data-Collection-Using-ESP32"
py web_app.py
```
* Open **`http://localhost:5000`** in your browser (Chrome recommended).
* Select the **RECEIVER** board's COM port from the top toolbar and click **Connect**.
* Observe real-time subcarrier amplitudes, RSSI, packet rate (Hz), and diagnostic counters.
* Click **Record** to log incoming CSI frames to `sessions/session-<id>/csi_data_<timestamp>.csv`.

#### Option B: Standalone Desktop CSI Log Viewer (`csi_log_viewer.py`)
```powershell
cd "C:\Users\$env:USERNAME\Desktop\apps\projects\CSI-Data-Collection-Using-ESP32"
py csi_log_viewer.py
```
* Connects directly to the Receiver's serial port using a native Tkinter interface, writes session CSV files, and displays incoming CSI rows live without requiring a browser.

---

## 4. Quick-Launch Scripts (`Desktop\apps` Folder)

For convenience, ready-to-use batch launchers are provided in the `Desktop\apps` directory:

| Script Name | Description |
| :--- | :--- |
| **`1_Central_Control_GUI.bat`** | Launches `central_control.py` to scan COM ports and assign `RECEIVER` / `TRANSMITTER` roles. |
| **`2_Web_Dashboard.bat`** | Starts `web_app.py` and opens `http://localhost:5000` in the default web browser. |
| **`3_CSI_Log_Viewer.bat`** | Launches the standalone desktop `csi_log_viewer.py` application. |
| **`4_ESP_IDF_Terminal.bat`** | Opens an interactive shell (`eim shell`) inside the project directory with ESP-IDF v5.5.5 activated. |
| **`5_Flash_ESP32.bat`** | Lists available COM ports, prompts for a port name (e.g., `COM9`), and flashes the compiled firmware. |

---

## 5. Troubleshooting

1. **`Missing Python package: flask` or `serial` when running `python web_app.py`:**
   * **Cause:** Running `python` inside an activated ESP-IDF terminal invokes the isolated ESP-IDF virtual environment (`C:\Espressif\tools\python\...`), which does not have Flask installed.
   * **Solution:** Run `py web_app.py` (using the Windows Python Launcher) in a standard terminal, or double-click `2_Web_Dashboard.bat`.
2. **`PermissionError(13, 'Access is denied')` on Serial Port:**
   * **Cause:** Another application (`central_control.py`, `idf.py monitor`, `csi_log_viewer.py`, or another terminal) is currently holding the COM port open.
   * **Solution:** Close any other application using that COM port and click **Connect** again.
3. **No COM Ports Detected When ESP32 Is Plugged In:**
   * **Cause:** The USB cable is power-only (no data lines) or the USB-UART driver (CP210x / CH340) is missing.
   * **Solution:** Use a verified data-capable USB cable and install the Silicon Labs CP210x or CH340 driver (or run `eim install-drivers`).
4. **Include Errors or Squiggly Lines in VS Code:**
   * **Cause:** The `build/compile_commands.json` file has not been generated yet.
   * **Solution:** Run `idf.py build` once so CMake generates `build/compile_commands.json`, then reload the VS Code window.
