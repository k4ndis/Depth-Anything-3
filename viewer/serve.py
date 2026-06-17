#!/usr/bin/env python3
"""
Lokaler HTTP-Server fuer den Room Scan Viewer.

Nutzung:
  1. scene.glb in diesen Ordner (viewer/) kopieren:
       cp ~/Depth-Anything-3/workspace/wohnzimmer/scan_wohnzimmer_large/scene.glb .
  2. Server starten:
       python serve.py
  3. Browser oeffnen: http://localhost:8080

Optionaler Port als Argument:
  python serve.py 9090
"""
import http.server
import socketserver
import os
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080


class CORSHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")


os.chdir(os.path.dirname(os.path.abspath(__file__)))

print()
print("  Room Scan Viewer – lokaler Server")
print("  ─────────────────────────────────")
print(f"  http://localhost:{PORT}")
print(f"  Verzeichnis: {os.getcwd()}")
print()
print("  Benoetigt: scene.glb im selben Ordner (viewer/)")
print("  Stoppen: Ctrl+C")
print()

with socketserver.TCPServer(('', PORT), CORSHandler) as httpd:
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n  Server gestoppt.')
