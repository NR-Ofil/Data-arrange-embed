"""
Portal server — serves the search portal at http://localhost:8765
Run from the project root:
    python portal/serve.py
"""

import http.server
import socketserver
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

PORT = 8765
ROOT = Path(__file__).parent.parent  # project root
QDRANT_URL = "http://100.84.73.5:6333"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        # Proxy /qdrant/* → Jetson Qdrant (avoids browser CORS restrictions)
        if self.path.startswith("/qdrant/"):
            qdrant_path = self.path[len("/qdrant"):]
            try:
                with urllib.request.urlopen(QDRANT_URL + qdrant_path, timeout=5) as r:
                    body = r.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"error":"qdrant unreachable"}')
            return
        super().do_GET()

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
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
