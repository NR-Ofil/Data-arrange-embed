"""
Portal server — serves the search portal at http://localhost:8765
Run from the project root:
    python portal/serve.py
"""

import http.server
import socketserver
import webbrowser
import os
import sys
from pathlib import Path

PORT = 8765
ROOT = Path(__file__).parent.parent  # project root


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, format, *args):
        # suppress per-request noise; only show startup message
        pass


if __name__ == "__main__":
    os.chdir(ROOT)

    index = ROOT / "output" / "file_index.json"
    if not index.exists():
        print("ERROR: output/file_index.json not found.")
        print("       Run the scanner first:  python scanner/scan.py")
        sys.exit(1)

    url = f"http://localhost:{PORT}/portal/index.html"

    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"Portal running at  {url}")
        print("Press Ctrl-C to stop.")
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
