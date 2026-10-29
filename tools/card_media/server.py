"""Card media capture server (localhost only): serves index.html with the integration's card,
and saves the PNG frames the page posts to frames/. Usage: python3 server.py [port]"""
import base64, http.server, sys
from pathlib import Path
HARNESS = Path(__file__).parent
CARD = HARNESS.parents[1] / "custom_components" / "aciq_thermostat" / "frontend" / "aciq-system-card.js"
OUT = HARNESS / "frames"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
OUT.mkdir(exist_ok=True)

class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=str(HARNESS), **k)
    def do_GET(self):
        if self.path.split("?")[0] == "/aciq-system-card.js":
            data = CARD.read_bytes()
            self.send_response(200); self.send_header("Content-Type", "text/javascript")
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data); return
        super().do_GET()
    def do_POST(self):
        name = self.path.split("name=")[1]
        assert all(c.isalnum() or c in "_-" for c in name)
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        (OUT / f"{name}.png").write_bytes(base64.b64decode(body.split(",", 1)[1]))
        self.send_response(204); self.end_headers()
    def log_message(self, *a): pass

http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
