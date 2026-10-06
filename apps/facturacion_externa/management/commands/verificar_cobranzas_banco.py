"""Verifica, SOLO LECTURA, unas credenciales de api-cobranzas-bancos antes de usarlas.

Pensado para el pase a producción: probar las credenciales del cajero web de PROD sin tocar el
`.env` del gateway que está corriendo (ese sigue apuntando a test hasta que todo dé OK).

Por defecto usa la configuración actual (settings). Para probar otras, se pasan como variables
de entorno con prefijo VERIFICAR_ (no quedan en ningún archivo ni en el historial si se cargan
con `read -s`):

    read -s VERIFICAR_PASSWORD; export VERIFICAR_PASSWORD
    VERIFICAR_BASE_URL=https://... VERIFICAR_CLIENT_ID=... VERIFICAR_CLIENT_SECRET=... \\
    VERIFICAR_USERNAME=... python manage.py verificar_cobranzas_banco

Qué hace: pide un token (OAuth password grant), consulta si el cajero tiene caja del día
(`GET /v1/cajas/existe`) y lista los catálogos `GET /v1/entes` y `GET /v1/bancos` (para elegir
DOCUMENTO_ENTE_ID / DOCUMENTO_BANCO_ID). NUNCA apertura caja ni crea/paga transacciones, y no
usa la caché del token del gateway.
"""

import json
import os

import requests
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Verifica (solo lectura) credenciales de api-cobranzas-bancos: token, caja del día y catálogos."

    def _valor(self, nombre: str) -> str:
        return os.environ.get(f"VERIFICAR_{nombre}") or getattr(settings, f"COBRANZAS_BANCO_{nombre}", "") or ""

    def _mostrar(self, titulo: str, response: requests.Response) -> None:
        try:
            cuerpo = json.dumps(response.json(), ensure_ascii=False, indent=2)
        except ValueError:
            cuerpo = response.text
        self.stdout.write(f"\n== {titulo}: HTTP {response.status_code}\n{cuerpo[:3000]}")

    def handle(self, *args, **options):
        base_url = self._valor("BASE_URL").rstrip("/")
        username = self._valor("USERNAME")
        origen = "variables VERIFICAR_*" if os.environ.get("VERIFICAR_BASE_URL") else "settings actuales"
        self.stdout.write(f"Base URL: {base_url}  |  usuario: {username}  |  origen: {origen}")

        if not base_url:
            self.stderr.write("Falta la base URL.")
            return

        try:
            response = requests.post(
                f"{base_url}/oauth/token",
                json={
                    "grant_type": "password",
                    "client_id": self._valor("CLIENT_ID"),
                    "client_secret": self._valor("CLIENT_SECRET"),
                    "username": username,
                    "password": self._valor("PASSWORD"),
                    "scope": "",
                },
                timeout=30,
            )
        except requests.RequestException as e:
            self.stderr.write(f"Sin conexión con {base_url}: {e}")
            return

        token = (response.json() or {}).get("access_token") if response.ok else None
        if not token:
            self.stderr.write(f"Login FALLÓ: HTTP {response.status_code}: {response.text[:500]}")
            return
        self.stdout.write(self.style.SUCCESS("Login OK (token recibido)."))

        headers = {"Authorization": f"Bearer {token}"}
        for titulo, path in (
            ("Caja del día (404 = todavía no se aperturó hoy, se abre sola con el primer pago)", "/v1/cajas/existe"),
            ("Entes (DOCUMENTO_ENTE_ID)", "/v1/entes"),
            ("Bancos (DOCUMENTO_BANCO_ID)", "/v1/bancos"),
        ):
            try:
                self._mostrar(titulo, requests.get(f"{base_url}{path}", headers=headers, timeout=30))
            except requests.RequestException as e:
                self.stderr.write(f"{path}: {e}")
