import os
import runpy
from pathlib import Path

BASE = Path(__file__).resolve().parent
for ln in (BASE / "creds.env").read_text(encoding="utf-8").splitlines():
    ln = ln.strip()
    if ln and not ln.startswith("#") and "=" in ln:
        k, v = ln.split("=", 1)
        os.environ[k.strip()] = v.strip()
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
runpy.run_path(str(BASE / "auto_checkin.py"), run_name="__main__")
