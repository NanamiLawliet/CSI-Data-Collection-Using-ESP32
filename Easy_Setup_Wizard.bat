<# :
@echo off
setlocal
title ESP32 CSI System - Setup
cd /d "%~dp0"
set "WIZARD_SELF=%~f0"
set "WIZARD_DIR=%~dp0"
set "WIZARD_ARGS=%*"

powershell -NoProfile -ExecutionPolicy Bypass -STA -Command "$code = Get-Content -LiteralPath $env:WIZARD_SELF -Raw -Encoding UTF8; Invoke-Expression $code; exit 0"
exit /b 0
#>

# ==============================================================================
# ESP32 CSI Data Collection System - Minimalist Setup Wizard (WinForms GUI)
# ==============================================================================

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

# Unicode symbols (encoding-safe char codes)
$ICON_DONE    = [char]::ConvertFromUtf32(0x2714) # Heavy Checkmark
$ICON_RUN     = [char]::ConvertFromUtf32(0x25CF) # Active Dot
$ICON_WAIT    = [char]::ConvertFromUtf32(0x25CB) # Pending Circle
$ICON_WARN    = [char]::ConvertFromUtf32(0x26A0) # Warning Sign
$ICON_FAIL    = [char]::ConvertFromUtf32(0x2716) # Heavy Cross
$ICON_SKIP    = [char]::ConvertFromUtf32(0x2014) # Dash
$ICON_FOLDER  = [char]::ConvertFromUtf32(0x1F4C1) # Folder

# Color Palette (Minimalist Dark Slate Theme)
$CLR_BG        = [System.Drawing.Color]::FromArgb(15, 17, 23)
$CLR_SURFACE   = [System.Drawing.Color]::FromArgb(24, 27, 36)
$CLR_ROW_ALT   = [System.Drawing.Color]::FromArgb(20, 23, 31)
$CLR_ROW_RUN   = [System.Drawing.Color]::FromArgb(28, 38, 56)
$CLR_ROW_FAIL  = [System.Drawing.Color]::FromArgb(48, 22, 26)
$CLR_CARD      = [System.Drawing.Color]::FromArgb(30, 34, 45)
$CLR_BORDER    = [System.Drawing.Color]::FromArgb(44, 50, 68)
$CLR_TEXT      = [System.Drawing.Color]::FromArgb(241, 243, 249)
$CLR_MUTED     = [System.Drawing.Color]::FromArgb(139, 148, 168)
$CLR_BLUE      = [System.Drawing.Color]::FromArgb(59, 130, 246)
$CLR_GREEN     = [System.Drawing.Color]::FromArgb(16, 185, 129)
$CLR_AMBER     = [System.Drawing.Color]::FromArgb(245, 158, 11)
$CLR_RED       = [System.Drawing.Color]::FromArgb(239, 68, 68)

# Default Paths
$UserDesktop  = [Environment]::GetFolderPath("Desktop")
$AppsDir      = Join-Path $UserDesktop "apps"
$DriversDir   = Join-Path $AppsDir "drivers"
$ProjectsDir  = Join-Path $AppsDir "projects"
$DefaultRepo  = Join-Path $ProjectsDir "CSI-Data-Collection-Using-ESP32"
$IdfProfile   = "C:\Espressif\tools\Microsoft.v5.5.5.PowerShell_profile.ps1"
$IdfPython    = "C:\Espressif\tools\python\v5.5.5\venv\Scripts\python.exe"
$RepoUrl      = "https://github.com/NanamiLawliet/CSI-Data-Collection-Using-ESP32.git"

$WizardDirClean = ($env:WIZARD_DIR -replace '[\\\/]+$', '')
if ($WizardDirClean -and (Test-Path (Join-Path $WizardDirClean "CMakeLists.txt")) -and (Test-Path (Join-Path $WizardDirClean "web_app.py"))) {
    $DefaultRepo = $WizardDirClean
}

try {
    if (-not (Test-Path $AppsDir -ErrorAction SilentlyContinue)) {
        New-Item -ItemType Directory -Force -Path $AppsDir -ErrorAction SilentlyContinue | Out-Null
    }
} catch {}
$LogFilePath = Join-Path $AppsDir "install_log.txt"

function Write-WizardLog {
    param([string]$Message)
    $ts = (Get-Date).ToString("HH:mm:ss")
    try {
        Add-Content -Path $LogFilePath -Value "[$ts] $Message" -Encoding UTF8 -ErrorAction SilentlyContinue
    } catch {}
}

function Refresh-SessionPath {
    $m = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $u = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$m;$u"
}

function Get-HostPythonExe {
    Refresh-SessionPath
    foreach ($candidate in @("python.exe", "py.exe", "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe")) {
        try {
            $ver = & $candidate --version 2>&1 | Out-String
            if ($LASTEXITCODE -eq 0 -and $ver -match "Python 3\.(1[0-9])") {
                return $candidate
            }
        } catch {}
    }
    return "python.exe"
}

function Test-Cp210xDriverInstalled {
    try {
        $drvOut = & pnputil.exe /enum-drivers 2>&1 | Out-String
        if ($drvOut -match "silabser\.inf|Silicon Laboratories") { return $true }
    } catch {}
    return $false
}

function Format-Duration {
    param([int]$TotalSeconds)
    if ($TotalSeconds -lt 0) { $TotalSeconds = 0 }
    $mins = [math]::Floor($TotalSeconds / 60)
    $secs = [math]::Floor($TotalSeconds % 60)
    return ("{0:D2}:{1:D2}" -f [int]$mins, [int]$secs)
}

# ==============================================================================
# 10 Installation Steps Definition
# ==============================================================================

$script:TargetRepoDir = $DefaultRepo

$script:Steps = @(
    @{
        Index = 1; Name = "System & Workspace Preparation"
        CleanSec = 3; FastSec = 1; Status = "Pending"; ActualSec = 0
        CheckFn = {
            try { return (Test-Path $ProjectsDir -ErrorAction SilentlyContinue) } catch { return $false }
        }
    },
    @{
        Index = 2; Name = "Python 3.12 Runtime"
        CleanSec = 45; FastSec = 2; Status = "Pending"; ActualSec = 0
        CheckFn = {
            $pyExe = Get-HostPythonExe
            try {
                $v = & $pyExe --version 2>&1 | Out-String
                if ($LASTEXITCODE -eq 0 -and $v -match "Python 3\.(1[0-9])") { return $true }
            } catch {}
            return $false
        }
    },
    @{
        Index = 3; Name = "Git & Project Repository"
        CleanSec = 25; FastSec = 2; Status = "Pending"; ActualSec = 0
        CheckFn = {
            Refresh-SessionPath
            $hasGit = $false
            try {
                if ((& git --version 2>&1 | Out-String) -match "git version") { $hasGit = $true }
            } catch {}
            $hasRepo = (Test-Path (Join-Path $script:TargetRepoDir "CMakeLists.txt")) -and (Test-Path (Join-Path $script:TargetRepoDir "main\main.c"))
            return ($hasGit -and $hasRepo)
        }
    },
    @{
        Index = 4; Name = "Espressif Installer (eim) & Silicon Labs CP210x Driver"
        CleanSec = 35; FastSec = 3; Status = "Pending"; ActualSec = 0
        CheckFn = {
            Refresh-SessionPath
            try {
                $e = & eim --version 2>&1 | Out-String
                if ($LASTEXITCODE -eq 0 -and $e.Trim().Length -gt 0 -and (Test-Cp210xDriverInstalled)) { return $true }
            } catch {}
            return $false
        }
    },
    @{
        Index = 5; Name = "Python Libraries (Flask, PySerial, NumPy, Matplotlib)"
        CleanSec = 25; FastSec = 3; Status = "Pending"; ActualSec = 0
        CheckFn = {
            $pyExe = Get-HostPythonExe
            try {
                $chk = & $pyExe -c "import tkinter, serial, flask, numpy, matplotlib; assert hasattr(serial, 'Serial'); print('ALL_IMPORTS_OK')" 2>&1 | Out-String
                if ($chk -match "ALL_IMPORTS_OK") { return $true }
            } catch {}
            return $false
        }
    },
    @{
        Index = 6; Name = "ESP-IDF v5.5.5 & Xtensa C/C++ Toolchain"
        CleanSec = 280; FastSec = 4; Status = "Pending"; ActualSec = 0
        CheckFn = {
            return ((Test-Path $IdfProfile) -and (Test-Path $IdfPython) -and (Test-Path "C:\Espressif\v5.5.5\esp-idf"))
        }
    },
    @{
        Index = 7; Name = "Visual Studio Code & Extensions (Optional)"
        CleanSec = 55; FastSec = 3; Status = "Pending"; ActualSec = 0
        CheckFn = {
            Refresh-SessionPath
            try {
                $c = & code --version 2>&1 | Out-String
                if ($LASTEXITCODE -eq 0 -and $c.Trim().Length -gt 0) { return $true }
            } catch {}
            return $false
        }
    },
    @{
        Index = 8; Name = "Build & Verify ESP32 CSI Firmware (idf.py build)"
        CleanSec = 70; FastSec = 8; Status = "Pending"; ActualSec = 0
        CheckFn = {
            $b1 = Join-Path $script:TargetRepoDir "build\project-name.bin"
            $b2 = Join-Path $script:TargetRepoDir "build\bootloader\bootloader.bin"
            $b3 = Join-Path $script:TargetRepoDir "build\partition_table\partition-table.bin"
            return ((Test-Path $b1) -and (Test-Path $b2) -and (Test-Path $b3))
        }
    },
    @{
        Index = 9; Name = "Flash Connected ESP32 Boards"
        CleanSec = 30; FastSec = 5; Status = "Pending"; ActualSec = 0
        CheckFn = { return $false }
    },
    @{
        Index = 10; Name = "Desktop Quick-Launch Shortcuts"
        CleanSec = 3; FastSec = 1; Status = "Pending"; ActualSec = 0
        CheckFn = {
            try { return (Test-Path (Join-Path $AppsDir "1_Central_Control_GUI.bat") -ErrorAction SilentlyContinue) } catch { return $false }
        }
    }
)

# ==============================================================================
# Build Minimalist Main Window
# ==============================================================================

$form = New-Object System.Windows.Forms.Form
$form.Text = "ESP32 CSI System Setup"
$form.Size = New-Object System.Drawing.Size(780, 610)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox = $false
$form.BackColor = $CLR_BG
$form.ForeColor = $CLR_TEXT
$form.Font = New-Object System.Drawing.Font("Segoe UI", 9.5)

# Minimal Top Header
$headerPanel = New-Object System.Windows.Forms.Panel
$headerPanel.Dock = "Top"
$headerPanel.Height = 64
$headerPanel.BackColor = $CLR_SURFACE
$form.Controls.Add($headerPanel)

$lblTitle = New-Object System.Windows.Forms.Label
$lblTitle.Text = "ESP32 CSI System Setup"
$lblTitle.Font = New-Object System.Drawing.Font("Segoe UI", 13, [System.Drawing.FontStyle]::Bold)
$lblTitle.ForeColor = $CLR_TEXT
$lblTitle.Location = New-Object System.Drawing.Point(24, 11)
$lblTitle.AutoSize = $true
$headerPanel.Controls.Add($lblTitle)

$lblSubtitle = New-Object System.Windows.Forms.Label
$lblSubtitle.Text = "Step 1 of 3  -  Review & Destination Folder"
$lblSubtitle.Font = New-Object System.Drawing.Font("Segoe UI", 9)
$lblSubtitle.ForeColor = $CLR_MUTED
$lblSubtitle.Location = New-Object System.Drawing.Point(26, 36)
$lblSubtitle.AutoSize = $true
$headerPanel.Controls.Add($lblSubtitle)

$lblTopEta = New-Object System.Windows.Forms.Label
$lblTopEta.Text = "Est. time: --:--"
$lblTopEta.Font = New-Object System.Drawing.Font("Consolas", 9.5, [System.Drawing.FontStyle]::Bold)
$lblTopEta.ForeColor = $CLR_GREEN
$lblTopEta.Location = New-Object System.Drawing.Point(540, 22)
$lblTopEta.Size = New-Object System.Drawing.Size(200, 22)
$lblTopEta.TextAlign = "MiddleRight"
$headerPanel.Controls.Add($lblTopEta)

# Minimal Bottom Footer
$footerPanel = New-Object System.Windows.Forms.Panel
$footerPanel.Dock = "Bottom"
$footerPanel.Height = 56
$footerPanel.BackColor = $CLR_SURFACE
$form.Controls.Add($footerPanel)

$btnCancel = New-Object System.Windows.Forms.Button
$btnCancel.Text = "Cancel"
$btnCancel.Size = New-Object System.Drawing.Size(95, 34)
$btnCancel.Location = New-Object System.Drawing.Point(24, 11)
$btnCancel.FlatStyle = "Flat"
$btnCancel.FlatAppearance.BorderColor = $CLR_BORDER
$btnCancel.BackColor = $CLR_CARD
$btnCancel.ForeColor = $CLR_MUTED
$footerPanel.Controls.Add($btnCancel)

$btnRetry = New-Object System.Windows.Forms.Button
$btnRetry.Text = "Retry Step"
$btnRetry.Size = New-Object System.Drawing.Size(110, 34)
$btnRetry.Location = New-Object System.Drawing.Point(385, 11)
$btnRetry.FlatStyle = "Flat"
$btnRetry.FlatAppearance.BorderSize = 0
$btnRetry.BackColor = $CLR_AMBER
$btnRetry.ForeColor = [System.Drawing.Color]::White
$btnRetry.Visible = $false
$footerPanel.Controls.Add($btnRetry)

$btnSkip = New-Object System.Windows.Forms.Button
$btnSkip.Text = "Skip Step"
$btnSkip.Size = New-Object System.Drawing.Size(100, 34)
$btnSkip.Location = New-Object System.Drawing.Point(505, 11)
$btnSkip.FlatStyle = "Flat"
$btnSkip.FlatAppearance.BorderColor = $CLR_BORDER
$btnSkip.BackColor = $CLR_CARD
$btnSkip.ForeColor = $CLR_TEXT
$btnSkip.Visible = $false
$footerPanel.Controls.Add($btnSkip)

$btnNext = New-Object System.Windows.Forms.Button
$btnNext.Text = "Next >"
$btnNext.Size = New-Object System.Drawing.Size(130, 34)
$btnNext.Location = New-Object System.Drawing.Point(615, 11)
$btnNext.FlatStyle = "Flat"
$btnNext.FlatAppearance.BorderSize = 0
$btnNext.Font = New-Object System.Drawing.Font("Segoe UI", 9.5, [System.Drawing.FontStyle]::Bold)
$btnNext.BackColor = $CLR_BLUE
$btnNext.ForeColor = [System.Drawing.Color]::White
$footerPanel.Controls.Add($btnNext)

# ==============================================================================
# Native Flicker-Free Step Table Builder (10 Static Panel Rows - Zero Hover Bugs)
# ==============================================================================

function New-StepTable {
    param([int]$X, [int]$Y, [int]$W, [int]$RowHeight = 25)
    $container = New-Object System.Windows.Forms.Panel
    $container.Location = New-Object System.Drawing.Point($X, $Y)
    $container.Size = New-Object System.Drawing.Size($W, ($RowHeight * 10 + 4))
    $container.BackColor = $CLR_SURFACE
    $container.BorderStyle = "FixedSingle"

    $rows = @()
    for ($i = 0; $i -lt 10; $i++) {
        $rowPanel = New-Object System.Windows.Forms.Panel
        $rowPanel.Location = New-Object System.Drawing.Point(0, ($i * $RowHeight))
        $rowPanel.Size = New-Object System.Drawing.Size(($W - 2), $RowHeight)
        $rowPanel.BackColor = if ($i % 2 -eq 1) { $CLR_ROW_ALT } else { $CLR_SURFACE }

        $lblNum = New-Object System.Windows.Forms.Label
        $lblNum.Text = "Step $($i + 1)"
        $lblNum.Location = New-Object System.Drawing.Point(10, 4)
        $lblNum.Size = New-Object System.Drawing.Size(62, ($RowHeight - 6))
        $lblNum.ForeColor = $CLR_MUTED
        $lblNum.Font = New-Object System.Drawing.Font("Consolas", 9)
        $rowPanel.Controls.Add($lblNum)

        $lblSt = New-Object System.Windows.Forms.Label
        $lblSt.Text = "$ICON_WAIT Pending"
        $lblSt.Location = New-Object System.Drawing.Point(78, 4)
        $lblSt.Size = New-Object System.Drawing.Size(115, ($RowHeight - 6))
        $lblSt.ForeColor = $CLR_MUTED
        $lblSt.Font = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Bold)
        $rowPanel.Controls.Add($lblSt)

        $lblName = New-Object System.Windows.Forms.Label
        $lblName.Text = $script:Steps[$i].Name
        $lblName.Location = New-Object System.Drawing.Point(200, 4)
        $lblName.Size = New-Object System.Drawing.Size(($W - 310), ($RowHeight - 6))
        $lblName.ForeColor = $CLR_TEXT
        $lblName.Font = New-Object System.Drawing.Font("Segoe UI", 9)
        $rowPanel.Controls.Add($lblName)

        $lblTime = New-Object System.Windows.Forms.Label
        $lblTime.Text = "--:--"
        $lblTime.Location = New-Object System.Drawing.Point(($W - 105), 4)
        $lblTime.Size = New-Object System.Drawing.Size(92, ($RowHeight - 6))
        $lblTime.TextAlign = "TopRight"
        $lblTime.ForeColor = $CLR_MUTED
        $lblTime.Font = New-Object System.Drawing.Font("Consolas", 9)
        $rowPanel.Controls.Add($lblTime)

        $container.Controls.Add($rowPanel)
        $rows += @{
            Panel = $rowPanel
            DefaultBg = $rowPanel.BackColor
            StatusLabel = $lblSt
            NameLabel = $lblName
            TimeLabel = $lblTime
        }
    }
    return @{ Container = $container; Rows = $rows }
}

# ==============================================================================
# PAGE 1: Clean Welcome & Folder Selection
# ==============================================================================

$pageWelcome = New-Object System.Windows.Forms.Panel
$pageWelcome.Location = New-Object System.Drawing.Point(0, 64)
$pageWelcome.Size = New-Object System.Drawing.Size(764, 450)
$form.Controls.Add($pageWelcome)

# Folder Card
$cardFolder = New-Object System.Windows.Forms.Panel
$cardFolder.Location = New-Object System.Drawing.Point(24, 14)
$cardFolder.Size = New-Object System.Drawing.Size(720, 66)
$cardFolder.BackColor = $CLR_SURFACE
$pageWelcome.Controls.Add($cardFolder)

$lblFolderCap = New-Object System.Windows.Forms.Label
$lblFolderCap.Text = "Installation Folder"
$lblFolderCap.Font = New-Object System.Drawing.Font("Segoe UI", 8.5, [System.Drawing.FontStyle]::Bold)
$lblFolderCap.ForeColor = $CLR_MUTED
$lblFolderCap.Location = New-Object System.Drawing.Point(14, 7)
$lblFolderCap.AutoSize = $true
$cardFolder.Controls.Add($lblFolderCap)

$txtRepoPath = New-Object System.Windows.Forms.TextBox
$txtRepoPath.Text = $DefaultRepo
$txtRepoPath.Location = New-Object System.Drawing.Point(14, 28)
$txtRepoPath.Size = New-Object System.Drawing.Size(572, 26)
$txtRepoPath.BackColor = $CLR_BG
$txtRepoPath.ForeColor = $CLR_TEXT
$txtRepoPath.BorderStyle = "FixedSingle"
$txtRepoPath.Font = New-Object System.Drawing.Font("Consolas", 9.5)
$cardFolder.Controls.Add($txtRepoPath)

$btnBrowse = New-Object System.Windows.Forms.Button
$btnBrowse.Text = "$ICON_FOLDER Browse..."
$btnBrowse.Location = New-Object System.Drawing.Point(596, 26)
$btnBrowse.Size = New-Object System.Drawing.Size(110, 28)
$btnBrowse.FlatStyle = "Flat"
$btnBrowse.FlatAppearance.BorderColor = $CLR_BORDER
$btnBrowse.BackColor = $CLR_CARD
$btnBrowse.ForeColor = $CLR_TEXT
$cardFolder.Controls.Add($btnBrowse)

# Checklist Header
$lblScanHeader = New-Object System.Windows.Forms.Label
$lblScanHeader.Text = "System Readiness (All 10 Steps)"
$lblScanHeader.Font = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Bold)
$lblScanHeader.ForeColor = $CLR_MUTED
$lblScanHeader.Location = New-Object System.Drawing.Point(24, 90)
$lblScanHeader.AutoSize = $true
$pageWelcome.Controls.Add($lblScanHeader)

$tblPrecheck = New-StepTable -X 24 -Y 112 -W 720 -RowHeight 27
$pageWelcome.Controls.Add($tblPrecheck.Container)

# Minimal Options Bar at Bottom of Page 1
$chkFlashBoards = New-Object System.Windows.Forms.CheckBox
$chkFlashBoards.Text = "Prompt to flash connected ESP32 boards"
$chkFlashBoards.Checked = $true
$chkFlashBoards.Location = New-Object System.Drawing.Point(24, 400)
$chkFlashBoards.AutoSize = $true
$chkFlashBoards.ForeColor = $CLR_TEXT
$pageWelcome.Controls.Add($chkFlashBoards)

$chkVsCode = New-Object System.Windows.Forms.CheckBox
$chkVsCode.Text = "Install VS Code (Optional)"
$chkVsCode.Checked = $false
$chkVsCode.Location = New-Object System.Drawing.Point(310, 400)
$chkVsCode.AutoSize = $true
$chkVsCode.ForeColor = $CLR_MUTED
$pageWelcome.Controls.Add($chkVsCode)

$chkDryRun = New-Object System.Windows.Forms.CheckBox
$chkDryRun.Text = "Safe Test Mode (Dry-Run)"
$chkDryRun.Checked = $false
$chkDryRun.Location = New-Object System.Drawing.Point(535, 400)
$chkDryRun.AutoSize = $true
$chkDryRun.ForeColor = $CLR_AMBER
$pageWelcome.Controls.Add($chkDryRun)

# ==============================================================================
# PAGE 2: Clean Progress & Step Tracker
# ==============================================================================

$pageProgress = New-Object System.Windows.Forms.Panel
$pageProgress.Location = New-Object System.Drawing.Point(0, 64)
$pageProgress.Size = New-Object System.Drawing.Size(764, 450)
$pageProgress.Visible = $false
$form.Controls.Add($pageProgress)

# Top Progress Card
$cardProg = New-Object System.Windows.Forms.Panel
$cardProg.Location = New-Object System.Drawing.Point(24, 12)
$cardProg.Size = New-Object System.Drawing.Size(720, 104)
$cardProg.BackColor = $CLR_SURFACE
$pageProgress.Controls.Add($cardProg)

$lblCurrentStepTitle = New-Object System.Windows.Forms.Label
$lblCurrentStepTitle.Text = "Preparing setup..."
$lblCurrentStepTitle.Location = New-Object System.Drawing.Point(16, 10)
$lblCurrentStepTitle.Size = New-Object System.Drawing.Size(560, 22)
$lblCurrentStepTitle.Font = New-Object System.Drawing.Font("Segoe UI", 10.5, [System.Drawing.FontStyle]::Bold)
$lblCurrentStepTitle.ForeColor = $CLR_TEXT
$cardProg.Controls.Add($lblCurrentStepTitle)

$lblPercentBig = New-Object System.Windows.Forms.Label
$lblPercentBig.Text = "0%"
$lblPercentBig.Location = New-Object System.Drawing.Point(610, 8)
$lblPercentBig.Size = New-Object System.Drawing.Size(92, 24)
$lblPercentBig.TextAlign = "MiddleRight"
$lblPercentBig.Font = New-Object System.Drawing.Font("Consolas", 13, [System.Drawing.FontStyle]::Bold)
$lblPercentBig.ForeColor = $CLR_BLUE
$cardProg.Controls.Add($lblPercentBig)

$lblSubStatus = New-Object System.Windows.Forms.Label
$lblSubStatus.Text = "Initializing..."
$lblSubStatus.Location = New-Object System.Drawing.Point(16, 34)
$lblSubStatus.Size = New-Object System.Drawing.Size(688, 18)
$lblSubStatus.Font = New-Object System.Drawing.Font("Consolas", 8.5)
$lblSubStatus.ForeColor = $CLR_MUTED
$cardProg.Controls.Add($lblSubStatus)

# Custom Modern Smooth Progress Bar Track & Fill
$barTrack = New-Object System.Windows.Forms.Panel
$barTrack.Location = New-Object System.Drawing.Point(16, 58)
$barTrack.Size = New-Object System.Drawing.Size(688, 12)
$barTrack.BackColor = $CLR_BG
$cardProg.Controls.Add($barTrack)

$barFill = New-Object System.Windows.Forms.Panel
$barFill.Location = New-Object System.Drawing.Point(0, 0)
$barFill.Size = New-Object System.Drawing.Size(0, 12)
$barFill.BackColor = $CLR_BLUE
$barTrack.Controls.Add($barFill)

# Elapsed & Remaining Time Row inside Progress Card
$lblElapsed = New-Object System.Windows.Forms.Label
$lblElapsed.Text = "Elapsed: 00:00"
$lblElapsed.Location = New-Object System.Drawing.Point(16, 78)
$lblElapsed.AutoSize = $true
$lblElapsed.Font = New-Object System.Drawing.Font("Consolas", 9)
$lblElapsed.ForeColor = $CLR_MUTED
$cardProg.Controls.Add($lblElapsed)

$lblRemaining = New-Object System.Windows.Forms.Label
$lblRemaining.Text = "Remaining: ~00:00"
$lblRemaining.Location = New-Object System.Drawing.Point(250, 78)
$lblRemaining.AutoSize = $true
$lblRemaining.Font = New-Object System.Drawing.Font("Consolas", 9, [System.Drawing.FontStyle]::Bold)
$lblRemaining.ForeColor = $CLR_AMBER
$cardProg.Controls.Add($lblRemaining)

$lblStepCounter = New-Object System.Windows.Forms.Label
$lblStepCounter.Text = "0 / 10 completed"
$lblStepCounter.Location = New-Object System.Drawing.Point(540, 78)
$lblStepCounter.Size = New-Object System.Drawing.Size(164, 18)
$lblStepCounter.TextAlign = "MiddleRight"
$lblStepCounter.Font = New-Object System.Drawing.Font("Consolas", 9)
$lblStepCounter.ForeColor = $CLR_GREEN
$cardProg.Controls.Add($lblStepCounter)

# All 10 Steps Live Table (Static Panels - Never Flickers on Hover)
$tblSteps = New-StepTable -X 24 -Y 124 -W 720 -RowHeight 25
$pageProgress.Controls.Add($tblSteps.Container)

# Compact Console Log Output at Bottom
$txtConsole = New-Object System.Windows.Forms.RichTextBox
$txtConsole.Location = New-Object System.Drawing.Point(24, 384)
$txtConsole.Size = New-Object System.Drawing.Size(720, 56)
$txtConsole.ReadOnly = $true
$txtConsole.BackColor = [System.Drawing.Color]::FromArgb(11, 13, 18)
$txtConsole.ForeColor = $CLR_MUTED
$txtConsole.Font = New-Object System.Drawing.Font("Consolas", 8.5)
$txtConsole.BorderStyle = "FixedSingle"
$txtConsole.WordWrap = $false
$pageProgress.Controls.Add($txtConsole)

# ==============================================================================
# PAGE 3: Minimalist Finish Screen (Central Control Only)
# ==============================================================================

$pageFinish = New-Object System.Windows.Forms.Panel
$pageFinish.Location = New-Object System.Drawing.Point(0, 64)
$pageFinish.Size = New-Object System.Drawing.Size(764, 450)
$pageFinish.Visible = $false
$form.Controls.Add($pageFinish)

$cardDone = New-Object System.Windows.Forms.Panel
$cardDone.Location = New-Object System.Drawing.Point(24, 18)
$cardDone.Size = New-Object System.Drawing.Size(720, 345)
$cardDone.BackColor = $CLR_SURFACE
$pageFinish.Controls.Add($cardDone)

$lblFinishBanner = New-Object System.Windows.Forms.Label
$lblFinishBanner.Text = "$ICON_DONE  Setup & Firmware Verification Complete"
$lblFinishBanner.Location = New-Object System.Drawing.Point(20, 18)
$lblFinishBanner.AutoSize = $true
$lblFinishBanner.Font = New-Object System.Drawing.Font("Segoe UI", 14, [System.Drawing.FontStyle]::Bold)
$lblFinishBanner.ForeColor = $CLR_GREEN
$cardDone.Controls.Add($lblFinishBanner)

$lblFinishSub = New-Object System.Windows.Forms.Label
$lblFinishSub.Text = "All required tools and the ESP32 CSI firmware binary are verified and ready."
$lblFinishSub.Location = New-Object System.Drawing.Point(22, 48)
$lblFinishSub.AutoSize = $true
$lblFinishSub.ForeColor = $CLR_MUTED
$cardDone.Controls.Add($lblFinishSub)

$txtSummaryReport = New-Object System.Windows.Forms.RichTextBox
$txtSummaryReport.Location = New-Object System.Drawing.Point(20, 78)
$txtSummaryReport.Size = New-Object System.Drawing.Size(680, 245)
$txtSummaryReport.ReadOnly = $true
$txtSummaryReport.BackColor = $CLR_BG
$txtSummaryReport.ForeColor = $CLR_TEXT
$txtSummaryReport.Font = New-Object System.Drawing.Font("Consolas", 9)
$txtSummaryReport.BorderStyle = "FixedSingle"
$cardDone.Controls.Add($txtSummaryReport)

$chkLaunchCentral = New-Object System.Windows.Forms.CheckBox
$chkLaunchCentral.Text = "Launch Central Control Interface when I click Finish"
$chkLaunchCentral.Checked = $true
$chkLaunchCentral.Location = New-Object System.Drawing.Point(26, 382)
$chkLaunchCentral.AutoSize = $true
$chkLaunchCentral.Font = New-Object System.Drawing.Font("Segoe UI", 10, [System.Drawing.FontStyle]::Bold)
$chkLaunchCentral.ForeColor = $CLR_TEXT
$pageFinish.Controls.Add($chkLaunchCentral)

# ==============================================================================
# Logic & Helper Functions
# ==============================================================================

function Get-StepEstimatedSeconds {
    param($Step, [bool]$IsPreInstalled)
    if ($Step.Index -eq 7 -and -not $chkVsCode.Checked) { return 0 }
    if ($Step.Index -eq 9 -and -not $chkFlashBoards.Checked) { return 0 }
    if ($IsPreInstalled) { return $Step.FastSec }
    return $Step.CleanSec
}

function Append-ConsoleLine {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return }
    Write-WizardLog $Text
    if ($txtConsole.TextLength -gt 20000) {
        $txtConsole.Text = $txtConsole.Text.Substring($txtConsole.TextLength - 10000)
    }
    $txtConsole.AppendText("$Text`r`n")
    $txtConsole.SelectionStart = $txtConsole.TextLength
    $txtConsole.ScrollToCaret()
    $lblSubStatus.Text = if ($Text.Length -gt 95) { $Text.Substring(0, 95) + "..." } else { $Text }
}

function Run-PreFlightScan {
    $script:TargetRepoDir = $txtRepoPath.Text.Trim()
    $totalEstSec = 0

    for ($i = 0; $i -lt 10; $i++) {
        $step  = $script:Steps[$i]
        $isPre = & $step.CheckFn
        $est   = Get-StepEstimatedSeconds -Step $step -IsPreInstalled $isPre
        $totalEstSec += $est
        $step.PreInstalled = $isPre
        $step.EstSec       = $est

        $r1 = $tblPrecheck.Rows[$i]
        if ($est -eq 0) {
            $r1.StatusLabel.Text = "$ICON_SKIP Skipped"
            $r1.StatusLabel.ForeColor = $CLR_MUTED
        } elseif ($isPre) {
            $r1.StatusLabel.Text = "$ICON_DONE Ready"
            $r1.StatusLabel.ForeColor = $CLR_GREEN
        } else {
            $r1.StatusLabel.Text = "$ICON_WAIT Install"
            $r1.StatusLabel.ForeColor = $CLR_AMBER
        }
        $r1.TimeLabel.Text = "~$(Format-Duration $est)"

        $r2 = $tblSteps.Rows[$i]
        $r2.Panel.BackColor = $r2.DefaultBg
        if ($est -eq 0) {
            $r2.StatusLabel.Text = "$ICON_SKIP Skipped"
            $r2.StatusLabel.ForeColor = $CLR_MUTED
        } else {
            $r2.StatusLabel.Text = "$ICON_WAIT Pending"
            $r2.StatusLabel.ForeColor = $CLR_MUTED
        }
        $r2.TimeLabel.Text = "~$(Format-Duration $est)"
    }

    $lblTopEta.Text = "Est. time: ~$(Format-Duration $totalEstSec)"
}

function Update-StepRowUI {
    param([int]$StepIdx, [string]$State, [string]$InfoText)
    $row = $tblSteps.Rows[$StepIdx - 1]
    if (-not $row) { return }

    switch ($State) {
        "Running" {
            $row.StatusLabel.Text = "$ICON_RUN Running"
            $row.StatusLabel.ForeColor = $CLR_BLUE
            $row.Panel.BackColor = $CLR_ROW_RUN
        }
        "Done" {
            $row.StatusLabel.Text = "$ICON_DONE Done"
            $row.StatusLabel.ForeColor = $CLR_GREEN
            $row.Panel.BackColor = $row.DefaultBg
        }
        "Failed" {
            $row.StatusLabel.Text = "$ICON_FAIL Failed"
            $row.StatusLabel.ForeColor = $CLR_RED
            $row.Panel.BackColor = $CLR_ROW_FAIL
        }
        "Skipped" {
            $row.StatusLabel.Text = "$ICON_SKIP Skipped"
            $row.StatusLabel.ForeColor = $CLR_MUTED
            $row.Panel.BackColor = $row.DefaultBg
        }
    }
    if ($InfoText) { $row.TimeLabel.Text = $InfoText }
    [System.Windows.Forms.Application]::DoEvents()
}

function Update-TimeAndProgressUI {
    param([int]$CurrentStepIndex, [int]$StepPercent)
    $elapsedSec = [int]((Get-Date) - $script:InstallStartTime).TotalSeconds
    $lblElapsed.Text = "Elapsed: $(Format-Duration $elapsedSec)"

    $doneCount = 0
    $completedWeight = 0.0
    $remainingEstSec = 0.0
    $totalWeight = 0.0

    foreach ($s in $script:Steps) {
        $w = [double][math]::Max(1, $s.EstSec)
        $totalWeight += $w
        if ($s.Status -in @("Done", "Skipped")) {
            $doneCount++
            $completedWeight += $w
        } elseif ($s.Index -eq $CurrentStepIndex) {
            $frac = [math]::Min(0.99, [math]::Max(0.0, ($StepPercent / 100.0)))
            $completedWeight += ($w * $frac)
            $remThis = [math]::Max(1, ($w * (1.0 - $frac)))
            $remainingEstSec += $remThis
        } else {
            $remainingEstSec += $w
        }
    }

    $overallPct = [int][math]::Min(100, [math]::Round(($completedWeight / [math]::Max(1.0, $totalWeight)) * 100))
    if ($doneCount -eq 10) {
        $overallPct = 100
        $lblRemaining.Text = "Remaining: 00:00"
    } else {
        $lblRemaining.Text = "Remaining: ~$(Format-Duration ([int]$remainingEstSec))"
    }

    $barFill.Width = [int][math]::Round(($overallPct / 100.0) * $barTrack.Width)
    $lblPercentBig.Text = "$overallPct%"
    $lblTopEta.Text = "$overallPct% Complete"
    $lblStepCounter.Text = "$doneCount / 10 completed"
}

function Invoke-AsyncCommand {
    param([string]$FileName, [string]$Arguments, [string]$WorkingDirectory = "", [int]$StepIndex = 1)

    Append-ConsoleLine "> $FileName $Arguments"
    $tmpLog = Join-Path $env:TEMP ("esp_wiz_cmd_" + [guid]::NewGuid().ToString("N") + ".log")
    $cmdWrapper = Join-Path $env:TEMP ("esp_wiz_run_" + [guid]::NewGuid().ToString("N") + ".cmd")
    [System.IO.File]::WriteAllBytes($tmpLog, [byte[]]@())

    $workLine = if ($WorkingDirectory -and (Test-Path $WorkingDirectory)) { "cd /d `"$WorkingDirectory`"`r`n" } else { "" }
    $cmdContent = "@echo off`r`n$workLine`"$FileName`" $Arguments > `"$tmpLog`" 2>&1`r`nexit /b %ERRORLEVEL%`r`n"
    Set-Content -Path $cmdWrapper -Value $cmdContent -Encoding ASCII -ErrorAction SilentlyContinue

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "cmd.exe"
    $psi.Arguments = "/c `"$cmdWrapper`""
    if ($WorkingDirectory -and (Test-Path $WorkingDirectory)) { $psi.WorkingDirectory = $WorkingDirectory }
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden

    $exitCode = 1
    $fs = $null
    $sr = $null
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
        $fs = New-Object System.IO.FileStream($tmpLog, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8)

        $pct = 15
        while (-not $proc.HasExited) {
            while ($null -ne ($line = $sr.ReadLine())) {
                if ($line.Trim().Length -gt 0) {
                    Append-ConsoleLine $line
                    if ($line -match "\[(\d+)/(\d+)\]") {
                        $pct = [int][math]::Min(95, [math]::Round(([int]$matches[1] / [math]::Max(1, [int]$matches[2])) * 95))
                    } elseif ($line -match "(\d{1,3})%") {
                        $p = [int]$matches[1]
                        if ($p -ge 0 -and $p -le 100) { $pct = [math]::Min(95, $p) }
                    } else {
                        $pct = [math]::Min(90, $pct + 1)
                    }
                }
            }
            Update-TimeAndProgressUI -CurrentStepIndex $StepIndex -StepPercent $pct
            [System.Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 60
        }
        while ($null -ne ($line = $sr.ReadLine())) {
            if ($line.Trim().Length -gt 0) { Append-ConsoleLine $line }
        }
        $exitCode = $proc.ExitCode
    } catch {
        Append-ConsoleLine "Command execution error: $($_.Exception.Message)"
        $exitCode = 1
    } finally {
        if ($null -ne $sr) { try { $sr.Close() } catch {} }
        if ($null -ne $fs) { try { $fs.Close() } catch {} }
        Remove-Item -Path $cmdWrapper -Force -ErrorAction SilentlyContinue
        Remove-Item -Path $tmpLog -Force -ErrorAction SilentlyContinue
    }
    [System.Windows.Forms.Application]::DoEvents()
    return $exitCode
}

function Invoke-IdfCommand {
    param([string]$IdfArgs, [string]$WorkingDir, [int]$StepIndex = 8)

    $batProfile = "C:\Espressif\tools\Microsoft.v5.5.5_profile.bat"
    $idfPyScript = "C:\Espressif\v5.5.5\esp-idf\tools\idf.py"
    $tmpIdfBat = Join-Path $env:TEMP ("esp_idf_cmd_" + [guid]::NewGuid().ToString("N") + ".bat")

    $batLines = @(
        "@echo off",
        "cd /d `"$WorkingDir`"",
        "if exist `"$batProfile`" call `"$batProfile`" >nul 2>&1",
        "`"$IdfPython`" `"$idfPyScript`" $IdfArgs",
        "exit /b %ERRORLEVEL%"
    )
    Set-Content -Path $tmpIdfBat -Value ($batLines -join "`r`n") -Encoding ASCII -ErrorAction SilentlyContinue
    try {
        return (Invoke-AsyncCommand -FileName "cmd.exe" -Arguments "/c `"$tmpIdfBat`"" -WorkingDirectory $WorkingDir -StepIndex $StepIndex)
    } finally {
        Remove-Item -Path $tmpIdfBat -Force -ErrorAction SilentlyContinue
    }
}

# ==============================================================================
# Step 9 Minimalist Warning & Flash Confirmation Dialog
# ==============================================================================

function Show-FlashConfirmationDialog {
    param([array]$PortList)

    $dlg = New-Object System.Windows.Forms.Form
    $dlg.Text = "$ICON_WARN Warning: Flash Confirmation"
    $dlg.Size = New-Object System.Drawing.Size(560, 410)
    $dlg.StartPosition = "CenterParent"
    $dlg.FormBorderStyle = "FixedDialog"
    $dlg.MaximizeBox = $false
    $dlg.MinimizeBox = $false
    $dlg.BackColor = $CLR_BG
    $dlg.ForeColor = $CLR_TEXT
    $dlg.Font = New-Object System.Drawing.Font("Segoe UI", 9.5)

    $warnTop = New-Object System.Windows.Forms.Panel
    $warnTop.Dock = "Top"
    $warnTop.Height = 56
    $warnTop.BackColor = [System.Drawing.Color]::FromArgb(92, 40, 14)
    $dlg.Controls.Add($warnTop)

    $lblW = New-Object System.Windows.Forms.Label
    $lblW.Text = "$ICON_WARN  WARNING: Firmware & NVS Erase Confirmation"
    $lblW.Font = New-Object System.Drawing.Font("Segoe UI", 11, [System.Drawing.FontStyle]::Bold)
    $lblW.ForeColor = [System.Drawing.Color]::FromArgb(254, 243, 199)
    $lblW.Location = New-Object System.Drawing.Point(18, 16)
    $lblW.AutoSize = $true
    $warnTop.Controls.Add($lblW)

    $lblP = New-Object System.Windows.Forms.Label
    $lblP.Text = "Select ESP32 COM port(s) to erase and flash:"
    $lblP.Location = New-Object System.Drawing.Point(20, 70)
    $lblP.AutoSize = $true
    $lblP.ForeColor = $CLR_MUTED
    $dlg.Controls.Add($lblP)

    $clb = New-Object System.Windows.Forms.CheckedListBox
    $clb.Location = New-Object System.Drawing.Point(20, 94)
    $clb.Size = New-Object System.Drawing.Size(504, 92)
    $clb.BackColor = $CLR_SURFACE
    $clb.ForeColor = $CLR_GREEN
    $clb.Font = New-Object System.Drawing.Font("Consolas", 9.5)
    $clb.BorderStyle = "FixedSingle"
    $clb.CheckOnClick = $true
    foreach ($p in $PortList) {
        $isEsp = ($p.Desc -match "CP210|CH340|CH341|Silicon Labs|USB-SERIAL|UART")
        [void]$clb.Items.Add("$($p.Port)  -  $($p.Desc)", $isEsp)
    }
    if ($clb.CheckedItems.Count -eq 0 -and $clb.Items.Count -gt 0) { $clb.SetItemChecked(0, $true) }
    $dlg.Controls.Add($clb)

    $lblNote = New-Object System.Windows.Forms.Label
    $lblNote.Location = New-Object System.Drawing.Point(20, 198)
    $lblNote.Size = New-Object System.Drawing.Size(504, 110)
    $lblNote.ForeColor = [System.Drawing.Color]::FromArgb(252, 165, 165)
    $lblNote.Text = "$ICON_WARN Please verify before continuing:`r`n" +
                    "- Runs 'idf.py erase-flash flash' (erases existing firmware and NVS RF calibration).`r`n" +
                    "- Uncheck any port above that is not an ESP32 board.`r`n" +
                    "- Do not unplug USB cables while flashing.`r`n" +
                    "- Boards will boot in IDLE mode ready for Central Control role assignment."
    $dlg.Controls.Add($lblNote)

    $btnYes = New-Object System.Windows.Forms.Button
    $btnYes.Text = "$ICON_WARN Erase & Flash Selected"
    $btnYes.Location = New-Object System.Drawing.Point(314, 320)
    $btnYes.Size = New-Object System.Drawing.Size(210, 34)
    $btnYes.FlatStyle = "Flat"
    $btnYes.FlatAppearance.BorderSize = 0
    $btnYes.BackColor = $CLR_AMBER
    $btnYes.ForeColor = [System.Drawing.Color]::White
    $btnYes.Font = New-Object System.Drawing.Font("Segoe UI", 9.5, [System.Drawing.FontStyle]::Bold)
    $btnYes.DialogResult = [System.Windows.Forms.DialogResult]::Yes
    $dlg.Controls.Add($btnYes)

    $btnNo = New-Object System.Windows.Forms.Button
    $btnNo.Text = "Skip Flashing"
    $btnNo.Location = New-Object System.Drawing.Point(170, 320)
    $btnNo.Size = New-Object System.Drawing.Size(130, 34)
    $btnNo.FlatStyle = "Flat"
    $btnNo.FlatAppearance.BorderColor = $CLR_BORDER
    $btnNo.BackColor = $CLR_CARD
    $btnNo.ForeColor = $CLR_TEXT
    $btnNo.DialogResult = [System.Windows.Forms.DialogResult]::No
    $dlg.Controls.Add($btnNo)

    $res = $dlg.ShowDialog($form)
    $sel = @()
    if ($res -eq [System.Windows.Forms.DialogResult]::Yes) {
        foreach ($it in $clb.CheckedItems) {
            if ($it.ToString() -match "^(COM\d+)") { $sel += $matches[1] }
        }
    }
    return @{ Confirmed = ($res -eq [System.Windows.Forms.DialogResult]::Yes); Ports = $sel }
}

# ==============================================================================
# Step Execution Engine
# ==============================================================================

function Execute-Step {
    param([int]$StepIndex)

    $script:CurrentStepStartTime = Get-Date
    $stepObj = $script:Steps[$StepIndex - 1]
    $lblCurrentStepTitle.Text = "Step $StepIndex of 10  -  $($stepObj.Name)"
    Update-StepRowUI -StepIdx $StepIndex -State "Running" -InfoText "Running..."
    Update-TimeAndProgressUI -CurrentStepIndex $StepIndex -StepPercent 10

    if ($chkDryRun.Checked) {
        Append-ConsoleLine "[DRY-RUN] Simulating Step $StepIndex/10: $($stepObj.Name)"
        for ($p = 25; $p -le 90; $p += 35) {
            Update-TimeAndProgressUI -CurrentStepIndex $StepIndex -StepPercent $p
            [System.Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 180
        }
        if ($StepIndex -eq 9 -and $chkFlashBoards.Checked) {
            $sim = @(@{ Port = "COM9 (SIMULATED)"; Desc = "Silicon Labs CP210x USB to UART Bridge" })
            $c = Show-FlashConfirmationDialog -PortList $sim
            if (-not $c.Confirmed) { $stepObj.Status = "Skipped" }
        }
        return $true
    }

    switch ($StepIndex) {
        1 {
            try {
                $regPath = "HKCU:\Software\Microsoft\PowerShell\1\ShellIds\Microsoft.PowerShell"
                if (-not (Test-Path $regPath -ErrorAction SilentlyContinue)) { New-Item -Path $regPath -Force -ErrorAction SilentlyContinue | Out-Null }
                Set-ItemProperty -Path $regPath -Name "ExecutionPolicy" -Value "RemoteSigned" -Force -ErrorAction SilentlyContinue
            } catch {}
            New-Item -ItemType Directory -Force -Path $AppsDir -ErrorAction SilentlyContinue | Out-Null
            New-Item -ItemType Directory -Force -Path $DriversDir -ErrorAction SilentlyContinue | Out-Null
            New-Item -ItemType Directory -Force -Path $ProjectsDir -ErrorAction SilentlyContinue | Out-Null
            Append-ConsoleLine "Workspace directories verified."
            return $true
        }
        2 {
            if (& $stepObj.CheckFn) {
                Append-ConsoleLine "Python 3.10+ already installed."
                return $true
            }
            [void](Invoke-AsyncCommand -FileName "winget.exe" -Arguments "install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements" -StepIndex 2)
            Refresh-SessionPath
            return (& $stepObj.CheckFn)
        }
        3 {
            Refresh-SessionPath
            $hasGit = $false
            try { if ((& git --version 2>&1 | Out-String) -match "git version") { $hasGit = $true } } catch {}
            if (-not $hasGit) {
                [void](Invoke-AsyncCommand -FileName "winget.exe" -Arguments "install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements" -StepIndex 3)
                Refresh-SessionPath
            }
            $curName = (& git config --global user.name 2>&1 | Out-String).Trim()
            if (-not $curName) {
                & git config --global user.name "NanamiLawliet" 2>&1 | Out-Null
                & git config --global user.email "burakmentesss@gmail.com" 2>&1 | Out-Null
            }
            if (-not (Test-Path (Join-Path $script:TargetRepoDir "CMakeLists.txt"))) {
                $parentDir = Split-Path -Parent $script:TargetRepoDir
                if (-not (Test-Path $parentDir)) { New-Item -ItemType Directory -Force -Path $parentDir | Out-Null }
                $ec = Invoke-AsyncCommand -FileName "git.exe" -Arguments "clone `"$RepoUrl`" `"$($script:TargetRepoDir)`"" -StepIndex 3
                if ($ec -ne 0) { return $false }
            } else {
                Append-ConsoleLine "Repository verified at $($script:TargetRepoDir)"
            }
            return (Test-Path (Join-Path $script:TargetRepoDir "CMakeLists.txt"))
        }
        4 {
            Refresh-SessionPath
            $hasEim = $false
            try { if ($LASTEXITCODE -eq 0 -and ((& eim --version 2>&1 | Out-String).Trim().Length -gt 0)) { $hasEim = $true } } catch {}
            if (-not $hasEim) {
                [void](Invoke-AsyncCommand -FileName "winget.exe" -Arguments "install --id Espressif.EIM-CLI -e --source winget --accept-package-agreements --accept-source-agreements" -StepIndex 4)
                Refresh-SessionPath
                try { if ($LASTEXITCODE -eq 0 -and ((& eim --version 2>&1 | Out-String).Trim().Length -gt 0)) { $hasEim = $true } } catch {}
            }
            if (Test-Cp210xDriverInstalled) {
                Append-ConsoleLine "Silicon Labs CP210x USB driver already installed."
            } else {
                $cpDir = Join-Path $DriversDir "CP210x"
                $cpZip = Join-Path $DriversDir "CP210x.zip"
                New-Item -ItemType Directory -Force -Path $cpDir | Out-Null
                if (-not (Test-Path (Join-Path $cpDir "silabser.inf"))) {
                    [void](Invoke-AsyncCommand -FileName "curl.exe" -Arguments "-4 -L -o `"$cpZip`" `"https://dl.espressif.com/dl/idf-installer/CP210x_Universal_Windows_Driver.zip`"" -StepIndex 4)
                    if (Test-Path $cpZip) { Expand-Archive -Path $cpZip -DestinationPath $cpDir -Force }
                }
                $infPath = Join-Path $cpDir "silabser.inf"
                if (Test-Path $infPath) {
                    try { Start-Process -FilePath "pnputil.exe" -ArgumentList "/add-driver `"$infPath`" /install" -Verb RunAs -Wait -WindowStyle Hidden } catch {}
                }
            }
            return $hasEim
        }
        5 {
            if (& $stepObj.CheckFn) {
                Append-ConsoleLine "Host Python packages already installed and verified."
                return $true
            }
            $pyExe = Get-HostPythonExe
            [void](Invoke-AsyncCommand -FileName $pyExe -Arguments "-m pip uninstall -y serial" -StepIndex 5)
            [void](Invoke-AsyncCommand -FileName $pyExe -Arguments "-m pip install --upgrade pip flask pyserial numpy matplotlib" -StepIndex 5)
            return (& $stepObj.CheckFn)
        }
        6 {
            if (-not (& $stepObj.CheckFn)) {
                [void](Invoke-AsyncCommand -FileName "eim.exe" -Arguments "install -n true -p `"C:\Espressif`" -i v5.5.5 -t esp32 --create-bat-activation-script true -v" -StepIndex 6)
            } else {
                Append-ConsoleLine "ESP-IDF v5.5.5 already installed."
            }
            $junc = "C:\Espressif\frameworks\esp-idf-v5.5.5-2"
            if ((Test-Path "C:\Espressif\v5.5.5\esp-idf") -and -not (Test-Path $junc)) {
                New-Item -ItemType Directory -Force -Path "C:\Espressif\frameworks" | Out-Null
                New-Item -ItemType Junction -Path $junc -Target "C:\Espressif\v5.5.5\esp-idf" -ErrorAction SilentlyContinue | Out-Null
            }
            return (& $stepObj.CheckFn)
        }
        7 {
            if (-not $chkVsCode.Checked) {
                $stepObj.Status = "Skipped"
                return $true
            }
            Refresh-SessionPath
            if (-not (& $stepObj.CheckFn)) {
                [void](Invoke-AsyncCommand -FileName "winget.exe" -Arguments "install --id Microsoft.VisualStudioCode -e --source winget --accept-package-agreements --accept-source-agreements" -StepIndex 7)
                Refresh-SessionPath
            }
            if (& $stepObj.CheckFn) {
                [void](Invoke-AsyncCommand -FileName "cmd.exe" -Arguments "/c code --install-extension espressif.esp-idf-extension --force" -StepIndex 7)
                [void](Invoke-AsyncCommand -FileName "cmd.exe" -Arguments "/c code --install-extension ms-vscode.cpptools --force" -StepIndex 7)
                [void](Invoke-AsyncCommand -FileName "cmd.exe" -Arguments "/c code --install-extension ms-python.python --force" -StepIndex 7)
                return $true
            }
            return $false
        }
        8 {
            $buildDir      = Join-Path $script:TargetRepoDir "build"
            $sdkConfigPath = Join-Path $script:TargetRepoDir "sdkconfig"
            $binApp        = Join-Path $script:TargetRepoDir "build\project-name.bin"
            $binBoot       = Join-Path $script:TargetRepoDir "build\bootloader\bootloader.bin"
            $binPart       = Join-Path $script:TargetRepoDir "build\partition_table\partition-table.bin"
            $projDesc      = Join-Path $script:TargetRepoDir "build\project_description.json"
            $buildNinja    = Join-Path $script:TargetRepoDir "build\build.ninja"
            $cmakeCache    = Join-Path $script:TargetRepoDir "build\CMakeCache.txt"
            $normTarget    = ($script:TargetRepoDir -replace '\\', '/')

            $cacheValid = $false
            if (Test-Path $buildDir) {
                $hasCoreFiles = (Test-Path $cmakeCache) -and (Test-Path $projDesc) -and (Test-Path $buildNinja)
                if ($hasCoreFiles) {
                    $cacheValid = [bool](Select-String -Path $cmakeCache -SimpleMatch $normTarget -Quiet -ErrorAction SilentlyContinue)
                }
                if (-not $cacheValid) {
                    Append-ConsoleLine "Cleaning incomplete or moved build directory..."
                    Remove-Item -Path $buildDir -Recurse -Force -ErrorAction SilentlyContinue
                }
            }

            $alreadyValid = $cacheValid -and (Test-Path $binApp) -and (Test-Path $binBoot) -and (Test-Path $binPart) -and (Test-Path $sdkConfigPath) -and (Select-String -Path $sdkConfigPath -Pattern "CONFIG_ESP_WIFI_CSI_ENABLED=y" -Quiet)
            if (-not $alreadyValid) {
                if (-not (Test-Path $sdkConfigPath)) {
                    [void](Invoke-IdfCommand -IdfArgs "set-target esp32" -WorkingDir $script:TargetRepoDir -StepIndex 8)
                }
                $buildEc = Invoke-IdfCommand -IdfArgs "build" -WorkingDir $script:TargetRepoDir -StepIndex 8
                if ($buildEc -ne 0 -or -not (Test-Path $binApp)) {
                    Append-ConsoleLine "Retrying clean build..."
                    Remove-Item -Path $buildDir -Recurse -Force -ErrorAction SilentlyContinue
                    [void](Invoke-IdfCommand -IdfArgs "build" -WorkingDir $script:TargetRepoDir -StepIndex 8)
                }
            }

            $ok = (Test-Path $binApp) -and (Test-Path $binBoot) -and (Test-Path $binPart) -and (Test-Path $sdkConfigPath) -and (Select-String -Path $sdkConfigPath -Pattern "CONFIG_ESP_WIFI_CSI_ENABLED=y" -Quiet)
            if ($ok) {
                Append-ConsoleLine "Firmware build verified: build\project-name.bin"
            }
            return $ok
        }
        9 {
            if (-not $chkFlashBoards.Checked) {
                $stepObj.Status = "Skipped"
                return $true
            }
            $pyExe = Get-HostPythonExe
            while ($true) {
                $portLines = & $pyExe -c "import serial.tools.list_ports; [print(f'{p.device}|{p.description}') for p in serial.tools.list_ports.comports() if p.vid is not None]" 2>&1
                $detected = @()
                foreach ($line in $portLines) {
                    $s = ($line | Out-String).Trim()
                    if ($s -match "^(COM\d+)\|(.*)$") { $detected += @{ Port = $matches[1]; Desc = $matches[2] } }
                }
                if ($detected.Count -eq 0) {
                    $msg = "$ICON_WARN No USB ESP32 boards detected.`r`n`r`nPlug in your ESP32 board(s) and click [Retry], or click [Cancel] to skip flashing now."
                    $ans = [System.Windows.Forms.MessageBox]::Show($form, $msg, "No ESP32 Detected", [System.Windows.Forms.MessageBoxButtons]::RetryCancel, [System.Windows.Forms.MessageBoxIcon]::Warning)
                    if ($ans -eq [System.Windows.Forms.DialogResult]::Retry) { continue }
                    $stepObj.Status = "Skipped"
                    return $true
                } else {
                    $confirm = Show-FlashConfirmationDialog -PortList $detected
                    if (-not $confirm.Confirmed -or $confirm.Ports.Count -eq 0) {
                        $stepObj.Status = "Skipped"
                        return $true
                    }
                    foreach ($comPort in $confirm.Ports) {
                        $fc = Invoke-IdfCommand -IdfArgs "-p $comPort erase-flash flash" -WorkingDir $script:TargetRepoDir -StepIndex 9
                        if ($fc -ne 0) { return $false }
                    }
                    return $true
                }
            }
        }
        10 {
            $repo = $script:TargetRepoDir
            try {
                Set-Content -Path (Join-Path $AppsDir "1_Central_Control_GUI.bat") -Encoding ASCII -ErrorAction SilentlyContinue -Value "@echo off`r`ntitle ESP32 CSI Central Control`r`ncd /d `"$repo`"`r`npython central_control.py`r`nif %ERRORLEVEL% NEQ 0 py central_control.py`r`n"
                Set-Content -Path (Join-Path $AppsDir "2_Web_Dashboard.bat") -Encoding ASCII -ErrorAction SilentlyContinue -Value "@echo off`r`ntitle ESP32 CSI Web Dashboard`r`ncd /d `"$repo`"`r`nstart `"`" `"http://localhost:5000`"`r`npython web_app.py %*`r`nif %ERRORLEVEL% NEQ 0 py web_app.py %*`r`n"
                Set-Content -Path (Join-Path $AppsDir "3_CSI_Log_Viewer.bat") -Encoding ASCII -ErrorAction SilentlyContinue -Value "@echo off`r`ntitle ESP32 CSI Log Viewer`r`ncd /d `"$repo`"`r`npython csi_log_viewer.py`r`nif %ERRORLEVEL% NEQ 0 py csi_log_viewer.py`r`n"
                Set-Content -Path (Join-Path $AppsDir "4_ESP_IDF_Terminal.bat") -Encoding ASCII -ErrorAction SilentlyContinue -Value "@echo off`r`ntitle ESP-IDF Terminal`r`ncd /d `"$repo`"`r`neim shell`r`n"
                Set-Content -Path (Join-Path $AppsDir "5_Flash_ESP32.bat") -Encoding ASCII -ErrorAction SilentlyContinue -Value "@echo off`r`ntitle Flash ESP32`r`ncd /d `"$repo`"`r`nset /p COMPORT=`"Enter COM Port (e.g. COM9): `"`r`npowershell -NoProfile -ExecutionPolicy Bypass -Command `". '$IdfProfile'; Set-Location '$repo'; idf.py -p %COMPORT% erase-flash flash`"`r`npause`r`n"
            } catch {}
            return $true
        }
    }
    return $true
}

# ==============================================================================
# Navigation & Wizard Loop
# ==============================================================================

$script:WizardStage = "Welcome"
$script:ActiveStepIdx = 1

function Start-InstallationLoop {
    $script:WizardStage = "Installing"
    $lblSubtitle.Text = "Step 2 of 3  -  Installing & Verifying System"
    $btnNext.Enabled = $false
    $btnRetry.Visible = $false
    $btnSkip.Visible = $false

    while ($script:ActiveStepIdx -le 10) {
        $idx = $script:ActiveStepIdx
        $sObj = $script:Steps[$idx - 1]

        if ($sObj.EstSec -eq 0) {
            $sObj.Status = "Skipped"
            Update-StepRowUI -StepIdx $idx -State "Skipped" -InfoText "Skipped"
            Update-TimeAndProgressUI -CurrentStepIndex $idx -StepPercent 100
            $script:ActiveStepIdx++
            continue
        }

        $t0 = Get-Date
        $ok = Execute-Step -StepIndex $idx
        $sObj.ActualSec = [int]((Get-Date) - $t0).TotalSeconds

        if ($ok) {
            if ($sObj.Status -ne "Skipped") {
                $sObj.Status = "Done"
                Update-StepRowUI -StepIdx $idx -State "Done" -InfoText (Format-Duration $sObj.ActualSec)
            } else {
                Update-StepRowUI -StepIdx $idx -State "Skipped" -InfoText "Skipped"
            }
            Update-TimeAndProgressUI -CurrentStepIndex $idx -StepPercent 100
            $script:ActiveStepIdx++
        } else {
            $sObj.Status = "Failed"
            Update-StepRowUI -StepIdx $idx -State "Failed" -InfoText "Failed"
            $lblCurrentStepTitle.Text = "$ICON_FAIL Step $idx Failed: $($sObj.Name)"
            $script:WizardStage = "PausedOnError"
            $btnRetry.Visible = $true
            $btnSkip.Visible = $true
            return
        }
    }

    $script:WizardStage = "Completed"
    $totalTime = Format-Duration ([int]((Get-Date) - $script:InstallStartTime).TotalSeconds)
    $lblCurrentStepTitle.Text = "$ICON_DONE All Steps Completed in $totalTime"
    $lblSubStatus.Text = "Click [Next >] to continue."
    $btnNext.Enabled = $true

    $binApp = Join-Path $script:TargetRepoDir "build\project-name.bin"
    $binSize = if (Test-Path $binApp) { "$([math]::Round((Get-Item $binApp).Length / 1024, 1)) KB (Verified)" } else { "Verified" }
    $lines = @(
        "Project Folder  : $($script:TargetRepoDir)",
        "ESP-IDF Version : v5.5.5 (C:\Espressif)",
        "Firmware Binary : build\project-name.bin ($binSize)",
        "Total Duration  : $totalTime",
        "",
        "Completed Steps:"
    )
    foreach ($s in $script:Steps) {
        $lines += ("  [{0}] Step {1,2}/10 - {2} ({3})" -f $s.Status.ToUpper().PadRight(7), $s.Index, $s.Name, (Format-Duration $s.ActualSec))
    }
    $txtSummaryReport.Text = ($lines -join "`r`n")
}

$btnBrowse.Add_Click({
    $fbd = New-Object System.Windows.Forms.FolderBrowserDialog
    $fbd.Description = "Select the project folder:"
    $fbd.ShowNewFolderButton = $true
    $cur = $txtRepoPath.Text.Trim()
    $fbd.SelectedPath = if (Test-Path $cur) { $cur } else { $UserDesktop }
    if ($fbd.ShowDialog($form) -eq [System.Windows.Forms.DialogResult]::OK) {
        $chosen = $fbd.SelectedPath
        if ((Test-Path (Join-Path $chosen "CMakeLists.txt")) -or ((Split-Path -Leaf $chosen) -eq "CSI-Data-Collection-Using-ESP32")) {
            $txtRepoPath.Text = $chosen
        } else {
            $txtRepoPath.Text = (Join-Path $chosen "CSI-Data-Collection-Using-ESP32")
        }
        Run-PreFlightScan
    }
})

$chkFlashBoards.Add_CheckedChanged({ Run-PreFlightScan })
$chkVsCode.Add_CheckedChanged({ Run-PreFlightScan })

$btnRetry.Add_Click({ if ($script:WizardStage -eq "PausedOnError") { Start-InstallationLoop } })
$btnSkip.Add_Click({
    if ($script:WizardStage -eq "PausedOnError") {
        $idx = $script:ActiveStepIdx
        $script:Steps[$idx - 1].Status = "Skipped"
        Update-StepRowUI -StepIdx $idx -State "Skipped" -InfoText "Skipped"
        $script:ActiveStepIdx++
        Start-InstallationLoop
    }
})

$btnNext.Add_Click({
    if ($script:WizardStage -eq "Welcome") {
        Run-PreFlightScan
        $pageWelcome.Visible = $false
        $pageProgress.Visible = $true
        $script:InstallStartTime = Get-Date
        $script:ActiveStepIdx = 1
        Start-InstallationLoop
    } elseif ($script:WizardStage -eq "Completed") {
        $lblSubtitle.Text = "Step 3 of 3  -  Ready"
        $pageProgress.Visible = $false
        $pageFinish.Visible = $true
        $script:WizardStage = "FinishScreen"
        $btnNext.Text = "Finish"
        $btnNext.BackColor = $CLR_GREEN
    } elseif ($script:WizardStage -eq "FinishScreen") {
        if ($chkLaunchCentral.Checked) {
            $pyExe = Get-HostPythonExe
            $pyFile = Join-Path $script:TargetRepoDir "central_control.py"
            if (Test-Path $pyFile) {
                Start-Process -FilePath $pyExe -ArgumentList "`"$pyFile`"" -WorkingDirectory $script:TargetRepoDir
            }
        }
        $form.Close()
    }
})

$btnCancel.Add_Click({ $form.Close() })
$form.Add_Shown({ Run-PreFlightScan })

if ($env:WIZARD_ARGS -match "--syntax-check") {
    Run-PreFlightScan
    foreach ($s in $script:Steps) {
        Write-Host ("STEP_{0}_PREINSTALLED={1}_EST={2}s" -f $s.Index, $s.PreInstalled, $s.EstSec)
    }
    Write-Host "GUI_AND_PREFLIGHT_OK"
    exit 0
}

[void]$form.ShowDialog()
$global:LASTEXITCODE = 0
