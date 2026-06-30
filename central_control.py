"""
central_control.py  —  ESP32 CSI Central Control Interface (Serial Standalone)
Discover and dynamically configure role assignments (Transmitter / Receiver)
for ESP32 generic firmware devices purely via USB Serial COM ports.
"""

import tkinter as tk
from tkinter import ttk, messagebox
import serial
import serial.tools.list_ports
import json
import threading
import time
import os
import re

CONFIG_FILE = "config.json"

# Regex to match the status advertisement on Serial Console
# E.g.: STATUS:Silikonlabs:ESP32:MAC:08:D1:F9:F6:7C:EC:IP:0.0.0.0:ROLE:IDLE:PEER:00:00:00:00:00:00:TXID:1
STATUS_REGEX = re.compile(
    r'(?:STATUS:)?Silikonlabs:ESP32:MAC:([0-9a-fA-F:]{17}):IP:([0-9.]+):ROLE:([A-Z_]+):PEER:([0-9a-fA-F:]{17}):TXID:(\d+)'
)

class CentralControlApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ESP32 CSI Central Control Interface (Serial Mode)")
        self.root.geometry("1000x600")
        self.root.configure(bg="#121212")

        # Configure styles
        self.style = ttk.Style()
        self.style.theme_use('clam')
        self.style.configure(".", background="#121212", foreground="#ffffff")
        self.style.configure("Treeview", 
                             background="#1e1e1e", 
                             foreground="#ffffff", 
                             fieldbackground="#1e1e1e",
                             borderwidth=0,
                             font=("Consolas", 10))
        self.style.configure("Treeview.Heading", 
                             background="#2d2d2d", 
                             foreground="#ffffff", 
                             font=("Segoe UI", 10, "bold"),
                             borderwidth=0)
        self.style.map("Treeview", background=[('selected', '#1e88e5')])
        self.style.configure("TLabel", background="#121212", foreground="#ffffff", font=("Segoe UI", 10))
        self.style.configure("Header.TLabel", background="#121212", foreground="#3ea6ff", font=("Segoe UI", 14, "bold"))
        self.style.configure("TButton", 
                             background="#2d2d2d", 
                             foreground="#ffffff", 
                             borderwidth=1, 
                             focuscolor="none",
                             font=("Segoe UI", 10, "bold"))
        self.style.map("TButton", 
                       background=[('active', '#1e88e5'), ('pressed', '#1565c0')],
                       foreground=[('active', '#ffffff')])
        self.style.configure("Action.TButton", 
                             background="#1e88e5", 
                             foreground="#ffffff", 
                             font=("Segoe UI", 10, "bold"))

        # Devices list: { MAC: { mac, com_port, role, peer_mac, tx_id, last_seen } }
        self.devices = {}
        
        # Selected MAC
        self.selected_mac = None

        # Load local configs
        self.config_mappings = {}
        self.load_local_config()

        # Run states
        self.running = True

        self._build_ui()

        # Start periodic serial scanner thread
        self.serial_thread = threading.Thread(target=self.periodic_serial_scan, daemon=True)
        self.serial_thread.start()

        # Timer loops
        self.update_table_loop()

    def _build_ui(self):
        # Top banner
        top_frame = tk.Frame(self.root, bg="#121212", padx=15, pady=10)
        top_frame.pack(fill=tk.X)
        
        lbl_title = ttk.Label(top_frame, text="ESP32 CSI CENTRAL CONTROL (SERIAL STANDALONE)", style="Header.TLabel")
        lbl_title.pack(side=tk.LEFT)
        
        btn_refresh = ttk.Button(top_frame, text="Scan COM Ports Now", command=self.trigger_manual_scan)
        btn_refresh.pack(side=tk.RIGHT)

        # Main Paned Window
        paned = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, bg="#121212", bd=0, sashwidth=5)
        paned.pack(fill=tk.BOTH, expand=True, padx=15, pady=10)

        # Left panel: Devices table
        left_frame = tk.Frame(paned, bg="#121212")
        paned.add(left_frame, minsize=650)

        table_label = ttk.Label(left_frame, text="Discovered Serial Devices (Silicon Labs ESP32)", font=("Segoe UI", 11, "bold"))
        table_label.pack(anchor=tk.W, pady=(0, 5))

        # Scrollbar
        scroll = ttk.Scrollbar(left_frame, orient=tk.VERTICAL)
        
        cols = ('mac', 'com', 'role', 'peer', 'tx_id', 'status')
        self.tree = ttk.Treeview(left_frame, columns=cols, show='headings', yscrollcommand=scroll.set, height=18)
        scroll.config(command=self.tree.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.tree.heading('mac', text='MAC Address')
        self.tree.heading('com', text='COM Port')
        self.tree.heading('role', text='Current Role')
        self.tree.heading('peer', text='Target Peer (RX)')
        self.tree.heading('tx_id', text='TX ID')
        self.tree.heading('status', text='Status')

        self.tree.column('mac', width=160, anchor=tk.CENTER)
        self.tree.column('com', width=100, anchor=tk.CENTER)
        self.tree.column('role', width=120, anchor=tk.CENTER)
        self.tree.column('peer', width=160, anchor=tk.CENTER)
        self.tree.column('tx_id', width=60, anchor=tk.CENTER)
        self.tree.column('status', width=90, anchor=tk.CENTER)
        
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.on_device_selected)

        # Right panel: Configuration Form
        self.right_frame = tk.Frame(paned, bg="#1e1e1e", padx=15, pady=15, bd=1, relief=tk.FLAT)
        paned.add(self.right_frame, minsize=320)

        form_title = ttk.Label(self.right_frame, text="ROLE CONFIGURATION PANEL", font=("Segoe UI", 12, "bold"))
        form_title.pack(anchor=tk.W, pady=(0, 15))
        
        # Target device label
        self.lbl_selected_dev = ttk.Label(self.right_frame, text="Selected: None", font=("Segoe UI", 10, "italic"), foreground="#ffb300")
        self.lbl_selected_dev.pack(anchor=tk.W, pady=(0, 15))

        # Role Selector
        ttk.Label(self.right_frame, text="Select Role:").pack(anchor=tk.W, pady=(5, 2))
        self.role_var = tk.StringVar(value="IDLE")
        self.cb_role = ttk.Combobox(self.right_frame, textvariable=self.role_var, values=["IDLE", "TRANSMITTER", "RECEIVER"], state="readonly")
        self.cb_role.pack(fill=tk.X, pady=(0, 10))
        self.cb_role.bind("<<ComboboxSelected>>", self.on_role_changed)

        # Transmitter Options Frame
        self.tx_options_frame = tk.LabelFrame(self.right_frame, text="Transmitter Configuration Options", bg="#1e1e1e", fg="#3ea6ff", padx=10, pady=10)
        
        ttk.Label(self.tx_options_frame, text="Target Receiver MAC:").pack(anchor=tk.W, pady=(2, 2))
        self.peer_var = tk.StringVar()
        self.cb_peer = ttk.Combobox(self.tx_options_frame, textvariable=self.peer_var)
        self.cb_peer.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(self.tx_options_frame, text="Transmitter ID (tx_id):").pack(anchor=tk.W, pady=(2, 2))
        self.tx_id_var = tk.IntVar(value=1)
        self.sp_tx_id = ttk.Spinbox(self.tx_options_frame, from_=1, to=10, textvariable=self.tx_id_var, width=10)
        self.sp_tx_id.pack(anchor=tk.W, pady=(0, 5))

        # Apply Button
        self.btn_apply = ttk.Button(self.right_frame, text="Apply Configuration", style="Action.TButton", command=self.apply_device_role)
        self.btn_apply.pack(fill=tk.X, pady=(20, 10))

        # Log Console Box
        ttk.Label(self.right_frame, text="Activity Logs:").pack(anchor=tk.W, pady=(10, 2))
        self.txt_log = tk.Text(self.right_frame, bg="#121212", fg="#a0a0a0", font=("Consolas", 9), height=10, bd=0, wrap=tk.WORD)
        self.txt_log.pack(fill=tk.BOTH, expand=True)
        self.txt_log.config(state=tk.DISABLED)

        self.log_message("System started in Serial Standalone mode. Scanning COM ports...")

    def log_message(self, message):
        self.txt_log.config(state=tk.NORMAL)
        timestamp = time.strftime("[%H:%M:%S] ")
        self.txt_log.insert(tk.END, timestamp + message + "\n")
        self.txt_log.see(tk.END)
        self.txt_log.config(state=tk.DISABLED)

    def load_local_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)
                    self.config_mappings = data.get("devices", {})
            except Exception as e:
                print(f"Error loading config.json: {e}")

    def save_local_config(self):
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump({"devices": self.config_mappings}, f, indent=4)
        except Exception as e:
            print(f"Error saving config.json: {e}")

    # ── Device Data Processing ────────────────────────────────────────────────
    def parse_and_update_device(self, msg, com_port):
        match = STATUS_REGEX.match(msg)
        if match:
            mac = match.group(1).upper()
            role = match.group(3)
            peer_mac = match.group(4).upper()
            tx_id = int(match.group(5))

            # Update or insert
            if mac not in self.devices:
                self.devices[mac] = {
                    'mac': mac,
                    'com_port': com_port,
                    'role': role,
                    'peer_mac': peer_mac,
                    'tx_id': tx_id,
                    'last_seen': time.time()
                }
                self.log_message(f"Discovered device: MAC {mac} on COM port {com_port}")
            else:
                self.devices[mac]['com_port'] = com_port
                self.devices[mac]['role'] = role
                self.devices[mac]['peer_mac'] = peer_mac
                self.devices[mac]['tx_id'] = tx_id
                self.devices[mac]['last_seen'] = time.time()

            # Keep config_mappings up to date
            if mac not in self.config_mappings:
                self.config_mappings[mac] = {}
            self.config_mappings[mac]['port'] = com_port
            self.config_mappings[mac]['role'] = role
            self.config_mappings[mac]['peer_mac'] = peer_mac
            self.config_mappings[mac]['tx_id'] = tx_id
            self.save_local_config()

            # Update receiver MAC list in peer combobox dynamically
            self.update_peer_combobox()

    def update_peer_combobox(self):
        receivers = []
        for mac, dev in self.devices.items():
            if dev['role'] == 'RECEIVER' or dev['role'] == 'IDLE':
                receivers.append(mac)
        self.cb_peer['values'] = receivers

    # ── Serial Port Scanning ──────────────────────────────────────────────────
    def trigger_manual_scan(self):
        self.log_message("Triggering manual COM port scan...")
        threading.Thread(target=self.scan_serial_ports, args=(True,), daemon=True).start()

    def periodic_serial_scan(self):
        # Scan once on startup to discover plugged boards without resetting them in a loop
        time.sleep(1.0)
        self.scan_serial_ports(verbose=False)

    def scan_serial_ports(self, verbose=False):
        try:
            ports = list(serial.tools.list_ports.comports())
            found_ports = []
            for p in ports:
                desc = p.description or ""
                mfg = p.manufacturer or ""
                port_desc = f"{p.device} ({desc})"
                found_ports.append(port_desc)
                
                # Broaden keywords to catch CH340, FTDI, Espressif native USB, and generic USB serial bridges
                keywords = ["Silicon", "CP210", "WCH", "CH34", "USB Serial", "USB-Serial", "Espressif", "FTDI"]
                if any(kw.lower() in desc.lower() or kw.lower() in mfg.lower() for kw in keywords):
                    port_name = p.device
                    threading.Thread(target=self.query_device_via_serial, args=(port_name, verbose), daemon=True).start()
            
            # Log scanned ports to UI activity console only when the list changes
            ports_str = ", ".join(found_ports)
            if not hasattr(self, '_last_ports_str') or self._last_ports_str != ports_str or verbose:
                self._last_ports_str = ports_str
                self.log_message(f"Scanned ports: {ports_str if found_ports else 'None'}")
        except Exception as e:
            print(f"Serial port scan error: {e}")

    def query_device_via_serial(self, port_name, verbose=False):
        try:
            with serial.Serial(port_name, 115200, timeout=1.0, dsrdtr=False, rtscts=False) as ser:
                ser.dtr = False
                ser.rts = False
                # Wait 1.2 seconds for the ESP32 to finish bootloader phase in case RTS/DTR reset triggered
                time.sleep(1.2)
                ser.reset_input_buffer()  # Flush bootloader log output
                ser.write(b"GET_STATUS\n")
                time.sleep(0.2)
                for _ in range(10):
                    line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if "Silikonlabs:ESP32" in line or "STATUS:Silikonlabs:ESP32" in line:
                        idx = line.find("Silikonlabs:ESP32")
                        if idx != -1:
                            status_line = line[idx:]
                            self.parse_and_update_device(status_line, port_name)
                            return
                if verbose:
                    self.log_message(f"COM port {port_name} opened but did not reply to GET_STATUS.")
        except Exception as e:
            if verbose:
                self.log_message(f"Could not open port {port_name}: {str(e)}")

    # ── UI Event Callbacks ────────────────────────────────────────────────────
    def on_device_selected(self, event):
        selected_items = self.tree.selection()
        if not selected_items:
            return
        
        item = selected_items[0]
        mac = self.tree.item(item, "values")[0]
        
        # If the same device is re-selected (e.g. during periodic table refresh),
        # do NOT overwrite the user's active edits in the combobox/form!
        if self.selected_mac == mac:
            return
            
        self.selected_mac = mac
        dev = self.devices.get(mac)
        if not dev:
            return

        self.lbl_selected_dev.config(text=f"Selected: {mac} ({dev.get('com_port', '—')})")
        
        # Load values to form
        current_role = dev.get('role', 'IDLE')
        self.role_var.set(current_role)
        self.on_role_changed(None)

        peer_mac = dev.get('peer_mac', '')
        if peer_mac == "00:00:00:00:00:00":
            peer_mac = ""
        self.peer_var.set(peer_mac)
        
        self.tx_id_var.set(dev.get('tx_id', 1))

    def on_role_changed(self, event):
        role = self.role_var.get()
        if role == "TRANSMITTER":
            self.tx_options_frame.pack(fill=tk.X, pady=10)
        else:
            self.tx_options_frame.pack_forget()

    # ── Apply Configuration (Write Commands) ───────────────────────────────────
    def apply_device_role(self):
        if not self.selected_mac:
            messagebox.showwarning("No Device Selected", "Please select an ESP32 device from the table first.")
            return

        role = self.role_var.get()
        dev = self.devices[self.selected_mac]
        
        # Build command
        if role == "IDLE":
            cmd = "SET_ROLE:IDLE"
        elif role == "RECEIVER":
            cmd = "SET_ROLE:RECEIVER"
        elif role == "TRANSMITTER":
            peer = self.peer_var.get().strip()
            tx_id = self.tx_id_var.get()
            if not peer:
                messagebox.showerror("Validation Error", "Transmitter mode requires a valid target Receiver MAC address.")
                return
            cmd = f"SET_ROLE:TRANSMITTER:{peer}:{tx_id}"
            
        self.log_message(f"Sending command to {self.selected_mac}: {cmd}")

        # Send command in background thread to keep UI responsive
        threading.Thread(target=self.dispatch_command_thread, args=(dev, cmd), daemon=True).start()

    def dispatch_command_thread(self, dev, cmd):
        success = False
        mac = dev['mac']
        com = dev.get('com_port', '')

        # Send via Serial
        if com:
            self.log_message(f"Attempting to write command over Serial ({com})...")
            try:
                with serial.Serial(com, 115200, timeout=1.0, dsrdtr=False, rtscts=False) as ser:
                    ser.dtr = False
                    ser.rts = False
                    # Wait 1.2 seconds for the ESP32 to boot up after hardware reset
                    time.sleep(1.2)
                    ser.reset_input_buffer()
                    ser.write(f"{cmd}\n".encode())
                    time.sleep(0.2)
                    success = True
                    self.log_message(f"Serial command dispatched to {com} successfully.")
            except Exception as e:
                self.log_message(f"Serial dispatch failed on {com}: {e}")

        if success:
            # Save role to local config mappings
            if mac not in self.config_mappings:
                self.config_mappings[mac] = {}
            self.config_mappings[mac]['role'] = dev['role'] = self.role_var.get()
            if self.role_var.get() == "TRANSMITTER":
                self.config_mappings[mac]['peer_mac'] = dev['peer_mac'] = self.peer_var.get().strip()
                self.config_mappings[mac]['tx_id'] = dev['tx_id'] = self.tx_id_var.get()
            else:
                self.config_mappings[mac]['peer_mac'] = dev['peer_mac'] = "00:00:00:00:00:00"
                self.config_mappings[mac]['tx_id'] = dev['tx_id'] = 1
            self.save_local_config()
            
            self.root.after(0, lambda: messagebox.showinfo("Success", f"Configuration sent to device {mac}.\nIt will now restart in its new role."))
        else:
            self.root.after(0, lambda: messagebox.showerror("Error", f"Failed to send configuration command to device {mac}.\nVerify connection is active."))

    # ── Table Refresh ─────────────────────────────────────────────────────────
    def update_table_loop(self):
        try:
            # Clear treeview items
            for item in self.tree.get_children():
                self.tree.delete(item)

            current_time = time.time()
            for mac, dev in self.devices.items():
                # If we haven't seen the device in 12 seconds, mark offline
                is_offline = (current_time - dev['last_seen']) > 12
                status_str = "Offline" if is_offline else "Online"
                
                peer_str = dev.get('peer_mac', '')
                if peer_str == "00:00:00:00:00:00":
                    peer_str = "—"
                
                com_str = dev.get('com_port', '')
                if not com_str:
                    com_str = "—"

                tx_id_str = str(dev.get('tx_id', '—')) if dev['role'] == 'TRANSMITTER' else "—"

                self.tree.insert('', tk.END, values=(
                    mac,
                    com_str,
                    dev.get('role', 'IDLE'),
                    peer_str,
                    tx_id_str,
                    status_str
                ))

            # Reselect previously selected device in tree if still present
            if self.selected_mac:
                for item in self.tree.get_children():
                    if self.tree.item(item, "values")[0] == self.selected_mac:
                        self.tree.selection_set(item)
                        break
        except Exception as e:
            print(f"Error updating Treeview table: {e}")

        # Refresh every 1.5 seconds
        self.root.after(1500, self.update_table_loop)

    def close(self):
        self.running = False
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = CentralControlApp(root)
    root.protocol("WM_DELETE_WINDOW", app.close)
    root.mainloop()
