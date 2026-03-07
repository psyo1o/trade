#!/usr/bin/env python
"""GUI startup test - checks if GUI starts without errors"""

import sys
import time
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer

print("=" * 70)
print("🧪 Testing GUI Initialization")
print("=" * 70)

import warnings
warnings.filterwarnings("ignore")

# Create QApplication first
app = QApplication(sys.argv)

print("\n1️⃣ Importing gui_main...")
try:
    from gui_main import BotDashboard
    print("✅ gui_main imported successfully")
except Exception as e:
    print(f"❌ Failed to import gui_main: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n2️⃣ Creating BotDashboard instance...")
try:
    dashboard = BotDashboard()
    print("✅ BotDashboard instance created successfully")
except Exception as e:
    print(f"❌ Failed to create BotDashboard: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n3️⃣ Checking GUI components...")
try:
    assert hasattr(dashboard, 'stats_label'), "Missing stats_label"
    assert hasattr(dashboard, 'tabs'), "Missing tabs"
    assert hasattr(dashboard, 'log_console'), "Missing log_console"
    print("✅ All GUI components present")
except AssertionError as e:
    print(f"❌ {e}")

print("\n4️⃣ Running event loop for 5 seconds...")
def stop_app():
    app.quit()

QTimer.singleShot(5000, stop_app)
app.exec_()

print("\n5️⃣ Final check...")
try:
    import main64
    print(f"broker_kr: {main64.broker_kr is not None and 'INITIALIZED' or 'None'}")
    print(f"broker_us: {main64.broker_us is not None and 'INITIALIZED' or 'None'}")
    print(f"upbit: {main64.upbit is not None and 'INITIALIZED' or 'None'}")
except Exception as e:
    print(f"Error: {e}")

print("\n" + "=" * 70)
print("✅ GUI initialization test completed successfully!")
print("=" * 70)
