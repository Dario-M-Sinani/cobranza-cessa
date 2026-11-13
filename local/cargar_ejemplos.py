"""Carga pagos web de ejemplo en el entorno local, por los mismos endpoints que usan
cessa-laravel, el bot y el kiosco (nada se inserta a mano en la base):

    docker compose -f local/docker-compose.yml exec web python local/cargar_ejemplos.py

- Pagos QR de web, panel web, bot-telegram y kiosco, BISA y BNB, hoy y ayer → se facturan
  contra el banco simulado y quedan con N° de factura y PDF.
- Dos pagos con error a propósito (pagan un comprobante que no es el más antiguo).
- QR generados que vencieron o se anularon sin pagar.
Se puede correr varias veces: cada corrida usa alias nuevos y la deuda que quede.
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from decimal import Decimal

import requests

API = "http://localhost:8001/api/externo"
SIIC = "http://siic:6012"
CABECERAS = {"X-Api-Key": "local"}
PREFIJOS = {"web": "CESSA-SIM-", "panel": "CESSA-ADMIN-", "bot": "CESSA-BOT-", "kiosco": "CESSA-KIO-"}
PAGADORES = ["MARIA QUISPE", "JUAN FLORES", "ROSA VARGAS", "LUIS ARCE", "CARMEN ROJAS", "PEDRO CHOQUE"]

azar = random.Random()
sello = datetime.now().strftime("%H%M%S")
contador = 0


def alias(canal: str) -> str:
    global contador
    contador += 1
    return f"{PREFIJOS[canal]}{datetime.now():%Y%m%d}{sello}-{contador:03d}"


def deuda(nro: str) -> list[dict]:
    r = requests.get(f"{SIIC}/v1/consulta/cliente", params={"nro_cliente": nro, "ver_deuda": "si"}, timeout=10)
    return r.json().get("deuda") or [] if r.ok else []


def monto(items: list[dict]) -> Decimal:
    return sum((Decimal(i["importe"]) * (-1 if i["debito_credito"] == "CREDITO" else 1) for i in items), Decimal("0"))


def pagar(nro: str, canal: str, cuando: datetime, cuantos: int | None = None, saltar_el_primero=False) -> None:
    items = deuda(nro)
    if saltar_el_primero:
        items = items[1:]  # el SIIC exige el más antiguo: esto termina en "La deuda cambió"
    elegidos = items[: cuantos or len(items)]
    total = monto(elegidos)
    if not elegidos or total <= 0:
        print(f"  {nro}: sin deuda para pagar")
        return
    cuerpo = {
        "alias": alias(canal), "nro_cliente": nro, "monto": f"{total:.2f}", "moneda": "BOB",
        "detalle": elegidos, "fecha_pago": cuando.astimezone().isoformat(),
        "banco": azar.choice(["BISA", "BISA", "BNB"]), "pagador_nombre": azar.choice(PAGADORES),
        "pagador_documento": str(azar.randint(1_000_000, 9_999_999)),
    }
    r = requests.post(f"{API}/recibos-web/liquidar/", json=cuerpo, headers=CABECERAS, timeout=30)
    estado = r.json().get("estado") if r.headers.get("content-type", "").startswith("application/json") else r.status_code
    print(f"  {canal:7} {nro} Bs {total:>8} → {estado}")


def qr_sin_pagar(nro: str, canal: str, estado: str, cuando: datetime) -> None:
    items = deuda(nro)[:1]
    if not items:
        return
    a = alias(canal)
    base = {"alias": a, "nro_cliente": nro, "monto": f"{monto(items):.2f}", "banco": "BISA", "detalle": items,
            "creado_en": cuando.astimezone().isoformat(), "vence_en": (cuando + timedelta(minutes=10)).astimezone().isoformat()}
    requests.post(f"{API}/qr-generados/", json=base, headers=CABECERAS, timeout=10)
    r = requests.post(f"{API}/qr-generados/", json={"alias": a, "estado": estado}, headers=CABECERAS, timeout=10)
    print(f"  {canal:7} {nro} QR {estado} → HTTP {r.status_code}")


if __name__ == "__main__":
    ahora = datetime.now()
    ayer = ahora - timedelta(days=1)
    print("Pagos facturados:")
    for nro, canal, cuando, cuantos in [
        ("100001", "web", ahora - timedelta(hours=3), None),
        ("100002", "bot", ahora - timedelta(hours=2), 1),
        ("100003", "kiosco", ahora - timedelta(minutes=50), None),
        ("100004", "web", ahora - timedelta(minutes=20), 1),
        ("100005", "panel", ahora - timedelta(minutes=5), None),
        ("100006", "web", ayer.replace(hour=10), None),
        ("100007", "bot", ayer.replace(hour=16), 1),
        ("100008", "kiosco", ayer.replace(hour=11), 1),
    ]:
        pagar(nro, canal, cuando, cuantos)
        time.sleep(0.2)
    print("Pagos con error (deuda cambiada):")
    pagar("100009", "web", ahora - timedelta(hours=1), 1, saltar_el_primero=True)
    pagar("777777", "bot", ahora - timedelta(minutes=30), 1, saltar_el_primero=True)
    print("QR sin pagar:")
    qr_sin_pagar("100012", "web", "expirado", ahora - timedelta(hours=4))
    qr_sin_pagar("100013", "bot", "expirado", ahora - timedelta(hours=1))
    qr_sin_pagar("100014", "web", "anulado", ahora - timedelta(minutes=40))
    print("Listo. En el panel: Pagos web (supervisor_demo / admin_demo).")
