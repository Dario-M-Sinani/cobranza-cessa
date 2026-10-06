"""Front de deuda de SIIC test (BKLDTA) para correr en la .88 (no tiene Node).

    python3 tools/deuda-test-front/server.py      # → http://127.0.0.1:5180

Desde la PC: `ssh -L 5180:127.0.0.1:5180 soporte@10.1.1.88` y abrir http://localhost:5180.

Mismo comportamiento que server.mjs: sirve index.html y reenvía a la API Nest de test
solo los GET de lectura de clientes/deuda, agregando el token (que nunca llega al
navegador). Token: API_TOKEN, o archivo .token al lado, o el primer ACCEPTED_SECRETS de
/opt/cessa-api-siicnest/.env.test (legible por soporte).
"""
import json
import os
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIR = Path(__file__).resolve().parent
PUERTO = int(os.environ.get("PORT", "5180"))
HOST = os.environ.get("HOST", "127.0.0.1")
API = os.environ.get("API_URL", "http://127.0.0.1:6012")
ENV_TEST = Path("/opt/cessa-api-siicnest/.env.test")

# Test tiene escrituras habilitadas: no dejar pasar nada que no sea lectura.
PERMITIDAS = [re.compile(r"^/v1/clientes$"), re.compile(r"^/v1/clientes/\d+$"), re.compile(r"^/v1/clientes/\d+/deuda$")]


def leer_token() -> str:
    if os.environ.get("API_TOKEN"):
        return os.environ["API_TOKEN"].strip()
    if (DIR / ".token").exists():
        return (DIR / ".token").read_text().strip()
    if ENV_TEST.exists():
        for linea in ENV_TEST.read_text().splitlines():
            if linea.startswith("ACCEPTED_SECRETS="):
                return linea.split("=", 1)[1].split(",")[0].strip().strip("\r")
    return ""


TOKEN = leer_token()


class Handler(BaseHTTPRequestHandler):
    def _responder(self, status, cuerpo: bytes, tipo="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        ruta, _, query = self.path.partition("?")
        if ruta in ("/", "/index.html"):
            return self._responder(200, (DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        if not ruta.startswith("/api/"):
            return self._responder(404, b"")
        destino = ruta[4:]
        if not any(p.match(destino) for p in PERMITIDAS):
            return self._responder(403, json.dumps({"message": "Ruta no permitida en este front (solo lectura)"}).encode())
        pedido = urllib.request.Request(
            f"{API}{destino}" + (f"?{query}" if query else ""),
            headers={"Authorization": TOKEN, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(pedido, timeout=60) as r:
                return self._responder(r.status, r.read(), r.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            return self._responder(e.code, e.read(), e.headers.get("Content-Type", "application/json"))
        except Exception as e:  # sin red / timeout
            return self._responder(502, json.dumps({"message": f"No se pudo llegar a {API}: {e}"}).encode())

    def do_POST(self):
        self._responder(403, json.dumps({"message": "Solo lectura"}).encode())

    def log_message(self, formato, *args):
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Falta el token: API_TOKEN, archivo .token o ACCEPTED_SECRETS en /opt/cessa-api-siicnest/.env.test")
    print(f"Deuda SIIC test → http://{HOST}:{PUERTO}  (API {API})")
    ThreadingHTTPServer((HOST, PUERTO), Handler).serve_forever()
