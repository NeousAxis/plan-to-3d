#!/usr/bin/env python3
"""Tiny receiver for PlanCAD canvas captures.

cad.html's shotPNG() POSTs the WebGL canvas as PNG to
    http://localhost:8778/save/<name>.png
and this server writes it under work/_render/. This is the headless-friendly
path used to batch-produce the geometric base images for img2img enhancement.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "work", "_render")


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        name = os.path.basename(self.path.split("/save/")[-1]) or "shot.png"
        os.makedirs(OUT, exist_ok=True)
        n = int(self.headers.get("Content-Length", 0))
        path = os.path.join(OUT, name)
        with open(path, "wb") as f:
            f.write(self.rfile.read(n))
        print(f"✓ {path}  ({n} bytes)", flush=True)
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8778
    print(f"shot server on :{port} → {OUT}", flush=True)
    HTTPServer(("127.0.0.1", port), H).serve_forever()
