# Implementation Plan: Single-File "Next -> Next -> Install" Setup Wizard (`Easy_Setup_Wizard.bat`)

> **Purpose of this file:** This is the finalized, step-by-step implementation specification to be executed by **Argon Medium**.
>
> ### Confirmed User Requirements
> 1. **Language:** **English only** across all wizard screens, step labels, logs, and dialogs.
> 2. **Single Self-Contained File (`Easy_Setup_Wizard.bat`):** Must be **1 single file** (a polyglot `.bat` + embedded PowerShell WinForms script) so the user can copy/download just `Easy_Setup_Wizard.bat` onto any freshly formatted Windows PC, double-click it, click `Next >`, and have it install everything, clone the repo (if not already inside it), compile the firmware, and **verify the build output (`build\project-name.bin`)** so the user only needs to plug in the ESP32 boards and start collecting CSI data.
> 3. **Confirmation Dialog with Warning Sign (`⚠️`) Before Flashing (Step 9):** Before flashing any connected ESP32 board, the wizard must display a prominent **Warning (`⚠️`) & Confirmation Dialog** listing the detected COM ports (`COMx`), warning that `erase-flash` will wipe existing firmware/NVS calibration on those ports, and asking the user to confirm before proceeding.

---

## 1. Single-File Architecture (Polyglot `.bat` + Embedded PowerShell GUI)

A freshly formatted Windows 10/11 PC does **not** have Python, Git, or Bash pre-installed, but **always** has `cmd.exe`, `powershell.exe`, and `.NET Windows Forms` (`System.Windows.Forms` & `System.Drawing`).

To keep everything in **one single file (`Easy_Setup_Wizard.bat`)**:
* The top header of `Easy_Setup_Wizard.bat` (lines 1–25) is a valid Batch script placed inside a PowerShell block comment (`<# : ... #>`).
* When the user double-clicks `Easy_Setup_Wizard.bat`:
  1. It checks for Administrator privileges; if not elevated, it automatically triggers the Windows UAC prompt (`RunAs`) so driver and `winget` installations never fail on permissions.
  2. It extracts/invokes `powershell.exe -NoProfile -ExecutionPolicy Bypass -STA` on itself (`%~f0`), jumping directly into the embedded PowerShell `.NET Windows Forms` GUI wizard (`-STA` is required for WinForms UI controls and dialogs).

### Polyglot Header Pattern for `Easy_Setup_Wizard.bat`
```bat
<# :
@echo off
title ESP32 CSI System - Easy Setup Wizard
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting Administrator privileges for driver and toolchain installation...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -STA -Command "$script = Get-Content -LiteralPath '%~f0' -Raw; Invoke-Expression $script"
exit /b
#>
# --- Embedded PowerShell WinForms Setup Wizard starts here ---
```

---

## 2. Wizard Screens & User Experience ("Like a Standard Windows Installer")

### Screen 1: Welcome & Pre-Flight System Check (`[Next >]`)
* **Header:** `ESP32 CSI Data Collection System — Setup Wizard`
* **Pre-Flight Scanner:** Automatically checks the PC on startup and shows which of the 10 steps are already satisfied (`✅ Installed`) vs. need installation (`⏳ To Be Installed`).
* **Target Paths (Editable or Default):**
  * Project Directory: `C:\Users\<USERNAME>\Desktop\apps\projects\CSI-Data-Collection-Using-ESP32`
  * ESP-IDF Directory: `C:\Espressif`
* **Options Checkboxes (Checked by default):**
  * `[x] Install USB-to-UART Drivers (Silicon Labs CP210x)`
  * `[x] Install ESP-IDF v5.5.5, C/C++ Toolchain & Build Firmware`
  * `[x] Install Visual Studio Code & Extensions (ESP-IDF, C/C++, Python)`
  * `[x] Prompt to Flash Connected ESP32 Boards After Build`
  * `[x] Create Desktop Quick-Launch Shortcuts (Desktop\apps)`
* **Buttons:** `[Cancel]` | **`[Next >]`**

---

### Screen 2: Live Installation Progress Screen (`[Install]`)
Displays 4 synchronized areas while commands run asynchronously:

#### A. Full Step Checklist (Shows All Steps, Completed Steps & Current Step)
Always visible on the main panel with live status icons and per-step elapsed time:
```text
✅ Step 1/10: System & PowerShell Preparation                      [Done - 00:01]
✅ Step 2/10: Python 3.12 Runtime                                  [Done - 00:42]
✅ Step 3/10: Git & Project Repository                             [Done - 00:18]
🔄 Step 4/10: Espressif Installer (eim) & CP210x USB Drivers       [Running...]
⏳ Step 5/10: Host Python Packages (Flask, PySerial, NumPy...)     [Pending]
⏳ Step 6/10: ESP-IDF v5.5.5 & Xtensa C/C++ Toolchain              [Pending]
⏳ Step 7/10: Visual Studio Code & Extensions                      [Pending]
⏳ Step 8/10: Build & Verify ESP32 CSI Firmware (idf.py build)     [Pending]
⏳ Step 9/10: Flash Connected ESP32 Boards (With Confirmation)     [Pending]
⏳ Step 10/10: Create Desktop Quick-Launch Shortcuts               [Pending]
```

#### B. Active Step Banner & Dual Progress Bars
* **Current Step Title:** e.g. `Currently in Step 8 of 10: Building & Verifying ESP32 CSI Firmware...`
* **Live Sub-Status Text:** Parses command stdout (e.g., `[84/112] Building C object...` or `Downloading xtensa-esp-elf...`)
* **Current Step Progress Bar (`0% – 100%`)**
* **Overall Progress Bar (`0% – 100%`)**

#### C. Elapsed Time & Dynamic Remaining Time (ETA)
* **`Elapsed Time: 02:15`** | **`Estimated Time Remaining: ~04:10`** | **`Overall Progress: 45% (4/10 Steps Done)`**
* **Dynamic ETA Calculation:**
  * Uses baseline duration weights for each step on a clean PC:
    * Step 1 (Prep): `3s`
    * Step 2 (Python 3.12): `45s`
    * Step 3 (Git & Repo Clone): `30s`
    * Step 4 (`eim` & CP210x Drivers): `35s`
    * Step 5 (Host Python Packages): `30s`
    * Step 6 (ESP-IDF v5.5.5 & Toolchain): `300s`
    * Step 7 (VS Code & Extensions): `60s`
    * Step 8 (Firmware Build & Binary Verification): `75s`
    * Step 9 (ESP32 Flashing): `40s`
    * Step 10 (Desktop Shortcuts): `5s`
  * Any step detected as already installed during pre-check has its baseline reduced to `2s–5s` (verification only).
  * A `1000 ms` UI timer updates `Elapsed Time` and `Remaining Time` every second.

#### D. Live Log Console & Error Recovery Controls
* Scrollable dark monospace log viewer streaming real-time stdout/stderr and saving to `C:\Users\<USERNAME>\Desktop\apps\install_log.txt`.
* If any step fails, the wizard pauses on that step, marks it `❌ Failed`, and displays **`[Retry Step]`** and **`[Skip Step]`** buttons.

---

### Screen 3: Completion & Ready-to-Run Screen (`[Finish]`)
* **Summary Banner:**
  * `✅ Installation & Firmware Build Verified!`
  * Shows verified paths:
    * `Firmware Binary: build\project-name.bin (Verified OK)`
    * `Bootloader: build\bootloader\bootloader.bin (Verified OK)`
    * `Partition Table: build\partition_table\partition-table.bin (Verified OK)`
* **Post-Install Launch Checkboxes:**
  * `[x] Launch Central Control GUI (Assign RECEIVER / TRANSMITTER roles)`
  * `[x] Launch Real-Time Web Dashboard (http://localhost:5000)`
* **Button:** **`[Finish]`**

---

## 3. Detailed Logic for Each of the 10 Steps

### Step 1: System & PowerShell Preparation
* Sets `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force`.
* Creates `C:\Users\$env:USERNAME\Desktop\apps\projects` and `C:\Users\$env:USERNAME\Desktop\apps\drivers`.

### Step 2: Python 3.12 Installation
* Checks if `py -3.12 --version` or `python --version` (`3.12.x`) is available.
* If missing, installs via:
  ```powershell
  winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
  ```
* Reloads `PATH` (`Machine` + `User`) into the current process immediately.

### Step 3: Git & Project Repository Clone/Verify
* Checks `git --version`; if missing, installs `Git.Git` via `winget` and refreshes `PATH`.
* Sets default Git `user.name` (`NanamiLawliet`) and `user.email` (`burakmentesss@gmail.com`) if not already configured.
* Checks if `C:\Users\$env:USERNAME\Desktop\apps\projects\CSI-Data-Collection-Using-ESP32\CMakeLists.txt` exists:
  * If missing (e.g., user only downloaded `Easy_Setup_Wizard.bat` onto a new PC), clones `https://github.com/NanamiLawliet/CSI-Data-Collection-Using-ESP32.git` into that folder.
  * If `Easy_Setup_Wizard.bat` was run from inside an existing clone, uses that directory and verifies `main/main.c`, `web_app.py`, and `central_control.py` exist.

### Step 4: Espressif Installer (`eim`) & CP210x USB-UART Drivers
* Checks `eim --version`; if missing, runs:
  ```powershell
  winget install --id Espressif.EIM-CLI -e --source winget --accept-package-agreements --accept-source-agreements
  ```
* Downloads `https://dl.espressif.com/dl/idf-installer/CP210x_Universal_Windows_Driver.zip` to `Desktop\apps\drivers\CP210x.zip`, extracts it, and installs `silabser.inf` via `pnputil /add-driver ... /install`.

### Step 5: Host Python Libraries (with `serial` vs `pyserial` Safeguard)
* Uninstalls the broken PyPI `serial` package if present (`py -m pip uninstall -y serial`).
* Installs/upgrades required packages:
  ```powershell
  py -m pip install --upgrade pip flask pyserial numpy matplotlib
  ```
* Verifies imports with:
  ```powershell
  py -c "import tkinter, serial, flask, numpy, matplotlib; assert hasattr(serial, 'Serial'); print('ALL_IMPORTS_OK')"
  ```

### Step 6: ESP-IDF v5.5.5 & Xtensa C/C++ Toolchain
* Checks if `C:\Espressif\tools\Microsoft.v5.5.5.PowerShell_profile.ps1` and `C:\Espressif\v5.5.5\esp-idf` exist.
* If missing, executes:
  ```powershell
  eim install -n true -p "C:\Espressif" -i v5.5.5 -t esp32 --create-bat-activation-script true -v
  ```
* Creates the compatibility directory junction `C:\Espressif\frameworks\esp-idf-v5.5.5-2 -> C:\Espressif\v5.5.5\esp-idf` if missing.

### Step 7: Visual Studio Code & Extensions
* Checks `code --version`; if missing, installs `Microsoft.VisualStudioCode` via `winget` and refreshes `PATH`.
* Installs `espressif.esp-idf-extension`, `ms-vscode.cpptools`, and `ms-python.python`.

### Step 8: Build & Verify ESP32 CSI Firmware (`idf.py build`)
* Activates `C:\Espressif\tools\Microsoft.v5.5.5.PowerShell_profile.ps1` inside the project directory.
* Runs `idf.py set-target esp32` (if `sdkconfig` is not yet generated) and `idf.py build`.
* Parses Ninja build lines (`[X/Y]`) to update the current step progress bar in real time.
* **Mandatory Build Verification Check:**
  * Asserts that `build\bootloader\bootloader.bin`, `build\partition_table\partition-table.bin`, and `build\project-name.bin` all exist and have file size `> 0`.
  * Asserts that `CONFIG_ESP_WIFI_CSI_ENABLED=y` is present in `sdkconfig`.
  * If any artifact is missing, marks Step 8 as `❌ Build Verification Failed` and stops before Step 9.

### Step 9: Flash Connected ESP32 Boards (With `⚠️` Warning & Confirmation Dialog)
* Scans connected USB-UART COM ports via `py -c "import serial.tools.list_ports..."`.
* **If no USB COM ports are detected:**
  * Shows a dialog with a **`⚠️ No ESP32 Boards Detected`** message allowing the user to either:
    * Plug in their ESP32 boards and click **`[Rescan Ports]`**, or
    * Click **`[Skip Flashing (Flash Later via 5_Flash_ESP32.bat)]`**.
* **If one or more USB COM ports are detected:**
  * Opens a dedicated **Confirmation Dialog with a prominent Warning Sign (`⚠️`)**:
    ```text
    ⚠️ WARNING: Firmware & NVS Flash Erase Confirmation

    The following USB Serial COM ports were detected:
      [x] COM9  (Silicon Labs CP210x USB to UART Bridge)
      [x] COM11 (Silicon Labs CP210x USB to UART Bridge)

    ⚠️ IMPORTANT WARNINGS:
    1. Flashing will run 'idf.py erase-flash flash', which completely erases
       existing firmware and NVS RF calibration data on the selected boards.
    2. Make sure no non-ESP32 USB serial devices are selected above.
    3. Do NOT unplug the USB cables while flashing is in progress.
    4. After flashing finishes, boards boot in IDLE mode — assign RECEIVER
       and TRANSMITTER roles once in Central Control GUI.

    Do you want to erase and flash the selected ESP32 board(s) now?
                [Yes, Erase & Flash Selected]     [Skip Flashing]
    ```
  * Only flashes the COM ports explicitly confirmed by the user.

### Step 10: Create Desktop Quick-Launch Shortcuts (`Desktop\apps`)
* Generates/verifies the 5 batch launchers in `C:\Users\$env:USERNAME\Desktop\apps`:
  * `1_Central_Control_GUI.bat`
  * `2_Web_Dashboard.bat`
  * `3_CSI_Log_Viewer.bat`
  * `4_ESP_IDF_Terminal.bat`
  * `5_Flash_ESP32.bat`

---

## 4. Prompt to Give Argon Medium

When you switch to **Argon Medium**, simply send:
> *"Please implement `Easy_Setup_Wizard.bat` following `EASY_INSTALLER_PLAN.md`."*
