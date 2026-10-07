"""Facturas ya pagadas de un cliente y su PDF, leídas directo del SIIC (solo lectura).

api-cobranzas-bancos (la .102) no expone el historial de pagos: su `/v1/consulta/deuda`
con `ver_pagos=si` trae solo los últimos 12 consumos, sin número de comprobante ni fecha
de pago. El SIIC sí: `GET /v1/clientes/{c}/pagos` (todas, por cualquier canal, de la más
nueva a la más vieja) y `POST /v1/comprobantes` (PDF real de la factura, el mismo que arma
para cajas; es POST pero no escribe). Es lo mismo que usa "Tus últimas facturas" en
cessa-laravel (`CessaApiService::ultimosPagos()` / `comprobantePdf()`).

Usa el mismo SIIC y token que `SiicDeudaClient` (SIIC_DEUDA_BASE_URL / SIIC_DEUDA_TOKEN):
en test, la API Nest :6012 sobre BKLDTA.
"""
from __future__ import annotations

import requests
from django.conf import settings

from services.deuda_client import _reparar_codificacion_recursivo

# Lo que identifica un comprobante en el SIIC (lo que pide /v1/comprobantes).
CAMPOS_CLAVE = (
    "codigo_sucursal", "nro_comprobante", "nro_suministro", "fecha",
    "tipo", "letra_comprobante", "nro_autorizacion", "nro_cliente",
)


class SiicHistorialError(Exception):
    """El SIIC no respondió o respondió algo inesperado."""


def _base() -> tuple[str, dict]:
    if not settings.SIIC_DEUDA_BASE_URL:
        raise SiicHistorialError("SIIC_DEUDA_BASE_URL no está configurado.")
    # Token tal cual, SIN "Bearer" (mismo contrato que SiicDeudaClient).
    return settings.SIIC_DEUDA_BASE_URL.rstrip("/"), {"Authorization": settings.SIIC_DEUDA_TOKEN}


def facturas_pagadas(nro_cliente: str, timeout: int = 30) -> list[dict]:
    """Todas las facturas pagadas del cliente, de la más nueva a la más vieja. Cada ítem trae
    la clave del comprobante + `detalle`, `importe`, `pago_fecha` (yyyymmdd) y `pago_hora`."""
    base, headers = _base()
    try:
        # Lumen/Nest: limit negativo = sin límite.
        r = requests.get(f"{base}/v1/clientes/{nro_cliente}/pagos", params={"limit": -1}, headers=headers, timeout=timeout)
        body = r.json()
    except requests.RequestException as exc:
        raise SiicHistorialError(f"pagos {nro_cliente}: {exc}") from exc
    except ValueError as exc:
        raise SiicHistorialError(f"pagos {nro_cliente}: respuesta no es JSON") from exc
    if r.status_code in (401, 403) or r.status_code >= 500:
        raise SiicHistorialError(f"pagos {nro_cliente}: HTTP {r.status_code}")
    if not isinstance(body, dict):
        raise SiicHistorialError(f"pagos {nro_cliente}: respuesta inesperada")
    return _reparar_codificacion_recursivo(body.get("items") or [])


def factura_pdf(clave: dict, timeout: int = 60) -> bytes:
    """PDF de la factura de un comprobante (`clave` = los CAMPOS_CLAVE tal como vinieron)."""
    base, headers = _base()
    item = {campo: clave.get(campo) for campo in CAMPOS_CLAVE}
    try:
        r = requests.post(
            f"{base}/v1/comprobantes", json={"formato": "pdf", "items": [item]}, headers=headers, timeout=timeout
        )
    except requests.RequestException as exc:
        raise SiicHistorialError(f"comprobante {item['nro_comprobante']}: {exc}") from exc
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        raise SiicHistorialError(f"comprobante {item['nro_comprobante']}: el SIIC no generó el PDF (HTTP {r.status_code})")
    return r.content
