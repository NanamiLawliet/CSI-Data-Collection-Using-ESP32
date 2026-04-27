#!/usr/bin/env python3
"""Test if web_app can import and set up properly"""

import sys

try:
    print("Testing imports...")
    from flask import Flask, render_template, jsonify, request
    print("✓ Flask imported")
    
    import serial
    print("✓ pyserial imported")
    
    import json
    print("✓ json imported")
    
    import re
    print("✓ re imported")
    
    import datetime
    print("✓ datetime imported")
    
    import csv
    print("✓ csv imported")
    
    import threading
    print("✓ threading imported")
    
    import time
    print("✓ time imported")
    
    import os
    print("✓ os imported")
    
    from collections import deque
    print("✓ deque imported")
    
    import math
    print("✓ math imported")
    
    import uuid
    print("✓ uuid imported")
    
    print("\nAll imports successful!")
    print("\nNow testing web_app.py syntax...")
    
    # Import the actual web_app module to check for syntax errors
    import web_app
    print("✓ web_app module loaded successfully")
    
    print("\n" + "=" * 60)
    print("SUCCESS: web_app.py is ready to run!")
    print("=" * 60)
    print("\nTo start the Flask server, run:")
    print("  python web_app.py")
    print("\nThen access it at:")
    print("  http://localhost:5000")
    print("\nConnect RX ESP32 to COM10 then click 'Connect & Start Logging'")
    
except ImportError as e:
    print(f"✗ Import error: {e}")
    sys.exit(1)
except SyntaxError as e:
    print(f"✗ Syntax error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"✗ Error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
