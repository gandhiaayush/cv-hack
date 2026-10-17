"""PCB Studio as a desktop app: a native window around the local server. Nothing leaves your machine except the model calls.

    python app.py            (or double-click "PCB Studio.app", made by bin/make_app.sh)"""
import os
import shutil
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
if not os.environ.get("KICAD_CLI"):
    found = shutil.which("kicad-cli") or str(Path.home() / "Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
    if Path(found).exists():
        os.environ["KICAD_CLI"] = found


def free_port(preferred: int = 8000) -> int:
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port")


def main() -> None:
    import uvicorn
    import webview

    if not (ROOT / "dashboard/web/dist/index.html").exists():
        sys.exit("The interface is not built yet. Run: cd dashboard/web && npm install && npm run build")
    port = free_port()
    server = uvicorn.Server(uvicorn.Config("dashboard.server:app", host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    for _ in range(120):  # wait until the server answers
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    webview.create_window("PCB Studio", url, width=1600, height=1000, min_size=(1100, 700))
    webview.start()
    server.should_exit = True


if __name__ == "__main__":
    main()
