# loggin_utils.py
from datetime import datetime
import os

_LOG_PATH = r"C:/Temp/import_sql_thread_log.txt"

def log(msg: str) -> None:
    """File-only logging. Safe to call from any thread."""
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now()}] {msg}\n")
    except Exception:
        # Last-resort fallback – avoid raising from logger
        try:
            print(f"[{datetime.now()}] {msg}")
        except Exception:
            pass

# Optional: only call from main thread / after UI is available
def try_ui_message(msg: str) -> None:
    try:
        import adsk.core  # local import to avoid hard dep at import time
        app = adsk.core.Application.get()
        ui = app.userInterface if app else None
        if ui:
            ui.messageBox(msg)
    except Exception:
        # swallow – UI calls can fail if not on main thread
        pass