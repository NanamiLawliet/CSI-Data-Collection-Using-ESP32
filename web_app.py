"""
web_app.py  —  Live web dashboard for the ESP32 ESP-NOW CSI receiver.

Connects to the RECEIVER board's serial port, parses the lines emitted by
main/main.c (CSI_START{...}CSI_END, ESP_NOW_RX logs, CSI diag logs), shows them
live in the browser and optionally records the CSI frames to

    sessions/session-<id>/csi_data_<timestamp>.csv

using the same column layout as csi_log_viewer.py.

Run:   py web_app.py                          then open http://localhost:5000
       py web_app.py --serial COM11            auto-connect on start
       py web_app.py --serial COM11 --record   auto-connect and start recording
Deps:  py -m pip install flask pyserial

NOTE (Windows + ESP-IDF): inside an ESP-IDF terminal `python` points to the
ESP-IDF interpreter, which has no Flask. Use `py web_app.py` instead.
"""

import sys

try:
    from flask import Flask, jsonify, request, send_from_directory
    import serial
    import serial.tools.list_ports
except ImportError as exc:  # pragma: no cover - environment problem, not code
    print(
        f"\n[web_app] Missing Python package: {exc.name}\n"
        f"  Interpreter in use: {sys.executable}\n"
        "  If this is the ESP-IDF Python (C:\\Espressif\\...), start the app with your\n"
        "  normal Python instead:\n"
        "      py web_app.py\n"
        "  and install the dependencies with:\n"
        "      py -m pip install flask pyserial\n"
    )
    sys.exit(1)

import argparse
import atexit
import csv
import datetime
import glob
import itertools
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from collections import deque

# ── configuration ────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

DEFAULT_BAUD = 115200        # must match uart_config.baud_rate in main/main.c
GROUP_WINDOW_MS = 50         # must match firmware SEND_INTERVAL_MS; frames from
                             # different TXs in the same window share a group_id
FRAME_BUFFER = 4000          # CSI frames kept in memory for the live plots
LOG_BUFFER = 600             # serial console lines kept in memory
INITIAL_FRAMES = 1500        # frames sent to a freshly opened browser tab
RATE_WINDOW_S = 2.0          # window used for Hz / bytes-per-second figures

CSV_FIELDS = [
    'timestamp', 'group_id', 'tx_id',
    'packet_type', 'rssi', 'rate', 'channel', 'bandwidth',
    'data_length', 'esp_timestamp', 'csi_data', 'seq',
]

CSI_REGEX = re.compile(r'CSI_START(\{.+\})CSI_END')
ESPNOW_RX_REGEX = re.compile(r'seq=(\d+)(?:\s+tx_id=(\d+))?\s+rssi=(-?\d+)', re.IGNORECASE)
ESPNOW_LEGACY_REGEX = re.compile(r'RX,SEQ=(\d+),RSSI=(-?\d+),TS=(\d+)')
# Base diag fields; txN=M pairs are parsed separately for N-TX extensibility
CSI_DIAG_REGEX = re.compile(
    r'callbacks=(\d+)\s+matches=(\d+)\s+zero_len=(\d+)\s+queue_drop=(\d+)',
    re.IGNORECASE,
)
TX_DIAG_REGEX = re.compile(r'\btx(\d+)=(\d+)', re.IGNORECASE)
ANSI_REGEX = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')

INSTANCE_ID = uuid.uuid4().hex[:8]   # lets the browser detect a server restart


# ── helpers ──────────────────────────────────────────────────────────────────

def guess_role(text):
    """Guess the firmware role of a board from a chunk of its serial output."""
    if 'ESP_NOW_TX' in text or 'ROLE:TRANSMITTER' in text:
        return 'TRANSMITTER'
    if ('CSI_START' in text or 'ESP_NOW_RX' in text or 'CSI diag' in text
            or 'ROLE:RECEIVER' in text):
        return 'RECEIVER'
    if 'ROLE:IDLE' in text:
        return 'IDLE'
    return None


def csi_amplitudes(raw):
    """ESP32 CSI buffers are int8 (imag, real) pairs -> per-subcarrier amplitude."""
    n = len(raw) // 2
    out = []
    for k in range(n):
        try:
            out.append(round(math.hypot(raw[2 * k], raw[2 * k + 1]), 1))
        except TypeError:
            out.append(None)
    return out


def open_serial(port, baud, timeout=0.02):
    s = serial.Serial()
    s.port = port
    s.baudrate = baud
    s.timeout = timeout
    # Set BEFORE open(): otherwise Windows asserts DTR/RTS on open, which can
    # reset the board or hold it in the bootloader.
    s.dtr = False
    s.rts = False
    s.open()
    try:
        s.set_buffer_size(rx_size=65536)
    except Exception:
        pass
    return s


def friendly_serial_error(port, exc):
    msg = str(exc)
    low = msg.lower()
    if 'permissionerror' in low or 'denied' in low or 'erişim' in low or 'errno 13' in low:
        return (f"{port} is busy — another program has it open (CSI Log Viewer, "
                f"Central Control, idf.py monitor, a serial monitor...). Close it and try again.")
    if 'filenotfounderror' in low or 'cannot find' in low or 'no such file' in low:
        return f"{port} was not found. Is the board plugged in?"
    return f"Could not open {port}: {msg}"


def load_config_roles():
    """{port: set(roles)} from config.json written by central_control.py."""
    roles = {}
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            for dev in json.load(f).get('devices', {}).values():
                port, role = dev.get('port'), dev.get('role')
                if port and role:
                    roles.setdefault(port, set()).add(role)
    except Exception:
        pass
    return roles


def _port_sort_key(device):
    nums = re.findall(r'\d+', device)
    return (re.sub(r'\d+', '', device), int(nums[-1]) if nums else 0)


# ── serial monitor / recorder ────────────────────────────────────────────────

class CSIMonitor:
    def __init__(self):
        self.lock = threading.RLock()
        self.serial_conn = None
        self.reader_thread = None
        self.stop_event = threading.Event()
        self.port = None
        self.baud = DEFAULT_BAUD
        self.conn_id = 0
        self.error = None
        self._frame_ids = itertools.count(1)
        self._log_ids = itertools.count(1)
        self.last_frame_id = 0
        self.last_log_id = 0
        self.frames = deque(maxlen=FRAME_BUFFER)
        self.logs = deque(maxlen=LOG_BUFFER)
        # recording state
        self.csv_file = None
        self.csv_writer = None
        self.rec_path = None
        self.rec_started = None
        self.rec_rows = 0
        self._last_flush = 0.0
        self._reset_stats()

    def _reset_stats(self):
        self.connected_at = None
        self.raw_lines = 0
        self.bytes_total = 0
        self.csi_frames = 0
        self.espnow_packets = 0
        self.parse_errors = 0
        self.last_line_time = None
        self.last_csi_time = None
        self.byte_samples = deque(maxlen=2000)     # (time, nbytes)
        self.tx = {}                               # {tx_id: stats dict}
        self.diag = {}
        self.role_votes = {'RECEIVER': 0, 'TRANSMITTER': 0, 'IDLE': 0}

    # ── logging into the in-memory console ──
    def _add_log(self, kind, text):
        self.last_log_id = next(self._log_ids)
        self.logs.append({
            'id': self.last_log_id,
            'ts': datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3],
            'kind': kind,
            'text': text if len(text) <= 400 else text[:400] + ' …',
        })

    def _tx(self, tx_id):
        st = self.tx.get(tx_id)
        if st is None:
            st = self.tx[tx_id] = {
                'frames': 0, 'espnow': 0, 'rssi': None, 'espnow_rssi': None,
                'last_csi': None, 'last_espnow': None,
                'channel': None, 'bandwidth': None, 'rate': None, 'data_length': None,
                'last_seq': None, 'last_seq_time': 0.0,
                'seq_received': 0, 'seq_missing': 0,
                'csi_times': deque(maxlen=1000), 'espnow_times': deque(maxlen=1000),
            }
        return st

    # ── connection lifecycle ──
    def connect(self, port, baud=DEFAULT_BAUD, reset=False):
        self.disconnect()
        try:
            conn = open_serial(port, baud)
        except (serial.SerialException, OSError, ValueError) as exc:
            with self.lock:
                self.error = friendly_serial_error(port, exc)
            return False, self.error

        try:
            if reset:
                # EN pulse via RTS (DTR stays low so GPIO0 is high -> normal boot)
                conn.rts = True
                time.sleep(0.12)
                conn.rts = False
            else:
                conn.reset_input_buffer()
        except Exception:
            pass

        with self.lock:
            self._reset_stats()
            self.frames.clear()
            self.logs.clear()
            self.serial_conn = conn
            self.port = port
            self.baud = baud
            self.error = None
            self.conn_id += 1
            self.connected_at = time.time()
            self.stop_event = threading.Event()
            self._add_log('info', f"Connected to {port} @ {baud} baud" + (" (board reset)" if reset else ""))
            stop_event = self.stop_event

        self.reader_thread = threading.Thread(
            target=self._reader, args=(conn, stop_event), daemon=True, name='serial-reader')
        self.reader_thread.start()
        print(f"[web_app] connected to {port} @ {baud}")
        return True, None

    def disconnect(self):
        self.stop_recording()
        with self.lock:
            conn = self.serial_conn
            self.serial_conn = None
            self.stop_event.set()
            thread = self.reader_thread
            self.reader_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            with self.lock:
                self._add_log('info', f"Disconnected from {self.port}")
            print(f"[web_app] disconnected from {self.port}")

    def _on_connection_lost(self, conn, exc):
        with self.lock:
            if self.serial_conn is not conn:
                return
            self.error = f"Serial connection to {self.port} was lost ({exc}). Is the cable unplugged?"
            self._add_log('error', self.error)
            self.serial_conn = None
            self.stop_recording()
        try:
            conn.close()
        except Exception:
            pass

    # ── reader thread ──
    def _reader(self, conn, stop_event):
        buf = b''
        while not stop_event.is_set():
            try:
                waiting = conn.in_waiting
                chunk = conn.read(min(max(waiting, 1), 16384))
            except (serial.SerialException, OSError, TypeError, AttributeError) as exc:
                if not stop_event.is_set():
                    self._on_connection_lost(conn, exc)
                return
            if not chunk:
                continue
            now = time.time()
            with self.lock:
                self.bytes_total += len(chunk)
                self.byte_samples.append((now, len(chunk)))
            buf += chunk
            if b'\n' not in buf:
                if len(buf) > 65536:          # garbage without newlines (wrong baud?)
                    buf = buf[-4096:]
                continue
            *lines, buf = buf.split(b'\n')
            for raw in lines:
                line = raw.decode('utf-8', errors='ignore').replace('\x00', '').strip()
                if not line:
                    continue
                try:
                    self._handle_line(line, now)
                except Exception as exc:
                    with self.lock:
                        self.parse_errors += 1
                        self._add_log('error', f"Line handling failed: {exc}")

    def _handle_line(self, line, now):
        clean = ANSI_REGEX.sub('', line)
        with self.lock:
            self.raw_lines += 1
            self.last_line_time = now
            role = guess_role(clean)
            if role:
                self.role_votes[role] += 1

            m = CSI_REGEX.search(clean)
            if m:
                try:
                    pkt = json.loads(m.group(1))
                except json.JSONDecodeError:
                    self.parse_errors += 1
                    self._add_log('error', 'Malformed CSI line: ' + clean[:160])
                    return
                self._on_csi(pkt, now)
                return

            m = ESPNOW_RX_REGEX.search(clean)
            if m:
                self._on_espnow(int(m.group(1)), int(m.group(2) or 0), int(m.group(3)), now)
                self._add_log('espnow', clean)
                return

            m = ESPNOW_LEGACY_REGEX.search(clean)
            if m:
                self._on_espnow(int(m.group(1)), 0, int(m.group(2)), now)
                self._add_log('espnow', clean)
                return

            m = CSI_DIAG_REGEX.search(clean)
            if m:
                self.diag = {
                    'callbacks': int(m.group(1)),
                    'matches': int(m.group(2)),
                    'zero_len': int(m.group(3)),
                    'queue_drop': int(m.group(4)),
                    'tx_counts': {t: int(n) for t, n in TX_DIAG_REGEX.findall(clean)},
                    'age_ref': now,
                }
                self._add_log('diag', clean)
                return

            if clean.startswith('E (') or 'Guru Meditation' in clean or 'abort()' in clean:
                kind = 'error'
            elif clean.startswith('W ('):
                kind = 'warn'
            else:
                kind = 'other'
            self._add_log(kind, clean)

    def _on_espnow(self, seq, tx_id, rssi, now):
        st = self._tx(tx_id)
        st['espnow'] += 1
        st['espnow_rssi'] = rssi
        st['last_espnow'] = now
        st['espnow_times'].append(now)
        self.espnow_packets += 1
        if st['last_seq'] is not None:
            gap = seq - st['last_seq']
            if 1 < gap < 1000:
                st['seq_missing'] += gap - 1
            elif gap <= 0 or gap >= 1000:     # transmitter rebooted -> restart stats
                st['seq_missing'] = 0
                st['seq_received'] = 0
        st['seq_received'] += 1
        st['last_seq'] = seq
        st['last_seq_time'] = now

    def _on_csi(self, pkt, now):
        tx_id = int(pkt.get('tx_id', 0) or 0)
        csi = pkt.get('csi_data') or []
        if isinstance(csi, str):
            try:
                csi = json.loads(csi)
            except Exception:
                csi = []
        data_length = int(pkt.get('data_length', len(csi)) or 0)
        rssi = int(pkt.get('rssi', 0) or 0)
        esp_ts = int(pkt.get('esp_timestamp', 0) or 0)
        # ESP32 hardware timestamp (us) -> frames of different TXs in the same
        # send window get the same group_id regardless of serial ordering.
        group_id = int((esp_ts / 1000) / GROUP_WINDOW_MS)

        st = self._tx(tx_id)
        st['frames'] += 1
        st['rssi'] = rssi
        st['last_csi'] = now
        st['csi_times'].append(now)
        st['channel'] = pkt.get('channel')
        st['bandwidth'] = pkt.get('bandwidth')
        st['rate'] = pkt.get('rate')
        st['data_length'] = data_length
        self.csi_frames += 1
        self.last_csi_time = now

        # correlate with the most recent ESP-NOW sequence number of this TX
        seq = st['last_seq'] if (st['last_seq'] is not None and now - st['last_seq_time'] <= 1.0) else ''

        if self.csv_writer is not None and data_length > 0:
            try:
                self.csv_writer.writerow({
                    'timestamp': datetime.datetime.fromtimestamp(now).isoformat(),
                    'group_id': group_id,
                    'tx_id': tx_id,
                    'packet_type': 'csi',
                    'rssi': rssi,
                    'rate': pkt.get('rate', 0),
                    'channel': pkt.get('channel', 0),
                    'bandwidth': pkt.get('bandwidth', 0),
                    'data_length': data_length,
                    'esp_timestamp': esp_ts,
                    'csi_data': json.dumps(csi),
                    'seq': seq,
                })
                self.rec_rows += 1
                if now - self._last_flush > 0.5:
                    self.csv_file.flush()
                    self._last_flush = now
            except Exception as exc:
                self.error = f"Writing CSV failed: {exc}"
                self._add_log('error', self.error)
                self.stop_recording()

        self.last_frame_id = next(self._frame_ids)
        self.frames.append({
            'id': self.last_frame_id,
            't': round(now - self.connected_at, 3),
            'tx': tx_id,
            'rssi': rssi,
            'amp': csi_amplitudes(csi),
        })
        self._add_log('csi', f"CSI  tx={tx_id} rssi={rssi} rate={pkt.get('rate')} "
                             f"ch={pkt.get('channel')} bw={pkt.get('bandwidth')} len={data_length} grp={group_id}")

    # ── recording ──
    def start_recording(self):
        with self.lock:
            if self.serial_conn is None:
                return False, 'Connect to the receiver first.'
            if self.csv_writer is not None:
                return True, None
            session_dir = os.path.join(SESSIONS_DIR, f"session-{uuid.uuid4().hex[:8]}")
            path = os.path.join(session_dir, f"csi_data_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv")
            try:
                os.makedirs(session_dir, exist_ok=True)
                f = open(path, 'w', newline='', encoding='utf-8')
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                writer.writeheader()
                f.flush()
            except OSError as exc:
                return False, f"Could not create CSV file: {exc}"
            self.csv_file, self.csv_writer = f, writer
            self.rec_path, self.rec_started, self.rec_rows = path, time.time(), 0
            self._add_log('info', f"Recording to {os.path.relpath(path, BASE_DIR)}")
            print(f"[web_app] recording to {path}")
            return True, None

    def stop_recording(self):
        with self.lock:
            if self.csv_file is None:
                return None
            info = {'path': os.path.relpath(self.rec_path, BASE_DIR).replace('\\', '/'),
                    'rows': self.rec_rows}
            try:
                self.csv_file.flush()
                self.csv_file.close()
            except Exception:
                pass
            self._add_log('info', f"Recording saved: {info['path']} ({info['rows']} frames)")
            print(f"[web_app] recording saved: {self.rec_path} ({self.rec_rows} frames)")
            self.csv_file = self.csv_writer = None
            self.rec_path = self.rec_started = None
            self.rec_rows = 0
            return info

    # ── snapshot for the browser ──
    @staticmethod
    def _since(buf, since, initial):
        """Items of an id-ordered deque newer than `since` (ids are consecutive)."""
        if not buf:
            return []
        first, last = buf[0]['id'], buf[-1]['id']
        if since < 0 or since > last:                 # new tab / server restarted
            start = max(0, len(buf) - initial)
        else:
            start = max(0, since - first + 1)
        return list(itertools.islice(buf, start, None))

    def snapshot(self, since=-1, log_since=-1):
        now = time.time()
        with self.lock:
            connected = self.serial_conn is not None
            uptime = (now - self.connected_at) if self.connected_at else 0.0
            window = max(0.5, min(RATE_WINDOW_S, uptime)) if uptime else RATE_WINDOW_S
            cutoff = now - window
            bps = sum(n for t, n in self.byte_samples if t >= cutoff) / window
            capacity = self.baud / 10.0           # 8N1 -> 10 bits per byte

            tx_list = []
            for tx_id in sorted(self.tx):
                st = self.tx[tx_id]
                seen = [t for t in (st['last_csi'], st['last_espnow']) if t]
                total_seq = st['seq_received'] + st['seq_missing']
                tx_list.append({
                    'id': tx_id,
                    'frames': st['frames'],
                    'espnow': st['espnow'],
                    'rssi': st['rssi'] if st['rssi'] is not None else st['espnow_rssi'],
                    'csi_hz': round(sum(1 for t in st['csi_times'] if t >= cutoff) / window, 2),
                    'espnow_hz': round(sum(1 for t in st['espnow_times'] if t >= cutoff) / window, 2),
                    'last_seen_age': round(now - max(seen), 2) if seen else None,
                    'last_csi_age': round(now - st['last_csi'], 2) if st['last_csi'] else None,
                    'channel': st['channel'],
                    'bandwidth': st['bandwidth'],
                    'rate': st['rate'],
                    'data_length': st['data_length'],
                    'loss_pct': round(100.0 * st['seq_missing'] / total_seq, 1) if total_seq else None,
                })

            votes = self.role_votes
            role_guess = max(votes, key=votes.get) if any(votes.values()) else None
            diag = dict(self.diag)
            if diag:
                diag['age'] = round(now - diag.pop('age_ref'), 1)

            status = {
                'instance': INSTANCE_ID,
                'conn_id': self.conn_id,
                'connected': connected,
                'port': self.port,
                'baud': self.baud,
                'error': self.error,
                'uptime': round(uptime, 3),
                'raw_lines': self.raw_lines,
                'bytes_total': self.bytes_total,
                'csi_frames': self.csi_frames,
                'espnow_packets': self.espnow_packets,
                'parse_errors': self.parse_errors,
                'serial_live': bool(self.last_line_time and now - self.last_line_time <= 3),
                'last_line_age': round(now - self.last_line_time, 1) if self.last_line_time else None,
                'csi_live': bool(self.last_csi_time and now - self.last_csi_time <= 3),
                'last_csi_age': round(now - self.last_csi_time, 1) if self.last_csi_time else None,
                'csi_hz': round(sum(t['csi_hz'] for t in tx_list), 2),
                'bytes_per_s': round(bps, 1),
                'uart_load': round(bps / capacity, 3) if capacity else 0,
                'role_guess': role_guess,
                'recording': {
                    'active': self.csv_writer is not None,
                    'path': os.path.relpath(self.rec_path, BASE_DIR).replace('\\', '/') if self.rec_path else None,
                    'rows': self.rec_rows,
                    'elapsed': round(now - self.rec_started, 1) if self.rec_started else 0,
                },
            }
            return {
                'status': status,
                'tx': tx_list,
                'diag': diag,
                'frames': self._since(self.frames, since, INITIAL_FRAMES),
                'last_frame_id': self.last_frame_id,
                'logs': self._since(self.logs, log_since, 300),
                'last_log_id': self.last_log_id,
            }

    def role_summary(self):
        with self.lock:
            votes = self.role_votes
            return {
                'role': max(votes, key=votes.get) if any(votes.values()) else None,
                'csi': self.csi_frames,
                'lines': self.raw_lines,
            }


monitor = CSIMonitor()
atexit.register(monitor.disconnect)


# ── port discovery ───────────────────────────────────────────────────────────

def list_ports():
    cfg = load_config_roles()
    ports = []
    for p in serial.tools.list_ports.comports():
        roles = cfg.get(p.device, set())
        cfg_role = 'RECEIVER' if 'RECEIVER' in roles else (sorted(roles)[0] if roles else None)
        ports.append({
            'device': p.device,
            'description': p.description or '',
            'usb': p.vid is not None,          # Bluetooth/virtual ports have no VID
            'config_role': cfg_role,
        })
    ports.sort(key=lambda d: (not d['usb'], _port_sort_key(d['device'])))

    suggested = None
    with monitor.lock:
        if monitor.serial_conn is not None:
            suggested = monitor.port
    if suggested is None:
        for p in ports:
            if p['usb'] and p['config_role'] == 'RECEIVER':
                suggested = p['device']
                break
    if suggested is None:
        suggested = next((p['device'] for p in ports if p['usb']), ports[0]['device'] if ports else None)
    return {'ports': ports, 'suggested': suggested}


def probe_port(device, duration=1.6):
    """Passively listen on a port (no reset, no writes) and guess the board role."""
    result = {'role': None, 'csi': 0, 'lines': 0, 'error': None}
    try:
        s = open_serial(device, DEFAULT_BAUD)
    except (serial.SerialException, OSError, ValueError) as exc:
        result['error'] = 'busy' if 'denied' in str(exc).lower() or 'permission' in str(exc).lower() \
            else str(exc)
        return result
    buf = b''
    try:
        t_end = time.time() + duration
        while time.time() < t_end:
            n = s.in_waiting
            buf += s.read(n) if n else b''
            if not n:
                time.sleep(0.02)
    except Exception as exc:
        result['error'] = str(exc)
    finally:
        try:
            s.close()
        except Exception:
            pass
    text = ANSI_REGEX.sub('', buf.decode('utf-8', errors='ignore'))
    result['lines'] = text.count('\n')
    result['csi'] = text.count('CSI_START')
    result['role'] = guess_role(text)
    return result


def probe_ports():
    with monitor.lock:
        own_port = monitor.port if monitor.serial_conn is not None else None
    devices = [p.device for p in serial.tools.list_ports.comports() if p.vid is not None]
    results, threads = {}, []

    def worker(dev):
        results[dev] = probe_port(dev)

    for dev in devices:
        if dev == own_port:
            results[dev] = dict(monitor.role_summary(), error=None, connected=True)
            continue
        t = threading.Thread(target=worker, args=(dev,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=4)
    return results


def list_recordings(limit=40):
    files = glob.glob(os.path.join(SESSIONS_DIR, '**', '*.csv'), recursive=True)
    files.sort(key=os.path.getmtime, reverse=True)
    out = []
    for path in files[:limit]:
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({
            'path': os.path.relpath(path, SESSIONS_DIR).replace('\\', '/'),
            'name': os.path.basename(path),
            'session': os.path.basename(os.path.dirname(path)),
            'size': st.st_size,
            'mtime': datetime.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
        })
    return out


# ── Flask app ────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path='/static')
logging.getLogger('werkzeug').setLevel(logging.WARNING)   # no per-request spam


@app.route('/')
def home():
    resp = send_from_directory(STATIC_DIR, 'dashboard.html', max_age=0)
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/api/poll')
def api_poll():
    since = request.args.get('since', default=-1, type=int)
    log_since = request.args.get('log_since', default=-1, type=int)
    return jsonify(monitor.snapshot(since, log_since))


@app.route('/api/status')
def api_status():
    return jsonify(monitor.snapshot(10**12, 10**12)['status'])


@app.route('/api/ports')
def api_ports():
    return jsonify(list_ports())


@app.route('/api/probe', methods=['POST'])
def api_probe():
    return jsonify({'results': probe_ports()})


@app.route('/api/connect', methods=['POST'])
def api_connect():
    data = request.get_json(silent=True) or {}
    port = (data.get('port') or '').strip()
    if not port:
        return jsonify({'ok': False, 'error': 'No serial port selected.'})
    try:
        baud = int(data.get('baud') or DEFAULT_BAUD)
    except (TypeError, ValueError):
        baud = DEFAULT_BAUD
    ok, err = monitor.connect(port, baud, reset=bool(data.get('reset')))
    if ok and data.get('record'):
        monitor.start_recording()
    return jsonify({'ok': ok, 'error': err})


@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    monitor.disconnect()
    return jsonify({'ok': True})


@app.route('/api/record/start', methods=['POST'])
def api_record_start():
    ok, err = monitor.start_recording()
    return jsonify({'ok': ok, 'error': err})


@app.route('/api/record/stop', methods=['POST'])
def api_record_stop():
    info = monitor.stop_recording()
    return jsonify({'ok': True, 'saved': info})


@app.route('/api/recordings')
def api_recordings():
    return jsonify(list_recordings())


@app.route('/recordings/<path:rel>')
def download_recording(rel):
    return send_from_directory(SESSIONS_DIR, rel, as_attachment=True)


def main():
    ap = argparse.ArgumentParser(description='ESP32 CSI live web dashboard')
    ap.add_argument('--host', default='0.0.0.0', help='HTTP bind address (default 0.0.0.0)')
    ap.add_argument('--port', type=int, default=5000, help='HTTP port (default 5000)')
    ap.add_argument('--serial', help='serial port to connect on start, e.g. COM11')
    ap.add_argument('--baud', type=int, default=DEFAULT_BAUD, help=f'baud rate (default {DEFAULT_BAUD})')
    ap.add_argument('--record', action='store_true', help='start recording right away (needs --serial)')
    args = ap.parse_args()

    if args.serial:
        ok, err = monitor.connect(args.serial, args.baud)
        if not ok:
            print(f"[web_app] {err}")
        elif args.record:
            monitor.start_recording()

    print(f"\n  ESP32 CSI dashboard running ->  http://localhost:{args.port}\n"
          f"  (Ctrl+C to stop)\n")
    try:
        app.run(host=args.host, port=args.port, debug=False, threaded=True, use_reloader=False)
    finally:
        monitor.disconnect()


if __name__ == '__main__':
    main()
