"""Proformas (como la pestaña "Proformas" de la app Java): una empresa o institución con muchos
medidores que paga todo junto. Se lee la proforma de la Nest de cobranzas (clientes y su deuda hasta un
mes) y el panel suma esos clientes al cobro de varios clientes que ya existe: cada uno se consulta como
siempre y se factura por separado, con un solo comprobante y un solo pago.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings

from services.cobranzas_banco_client import CajeraCobranzasClient, CobranzasBancoError, get_cobranzas_banco_client


class ProformaError(Exception):
    def __init__(self, mensaje: str, status: int = 400):
        super().__init__(mensaje)
        self.status = status


def _cliente_http(usuario):
    if settings.CAJAS_POR_CAJERA and getattr(usuario, "origen", "") == "nest":
        return CajeraCobranzasClient(usuario)
    cliente = get_cobranzas_banco_client()
    if not hasattr(cliente, "_request_autenticado"):
        raise ProformaError("Las proformas no están disponibles con esta configuración.", 503)
    return cliente


def _get(cliente, ruta: str, **kwargs):
    try:
        respuesta = cliente._request_autenticado("get", ruta, **kwargs)
    except CobranzasBancoError as exc:
        raise ProformaError(f"Sistema de cobranzas: {exc}", 502) from exc
    if respuesta.status_code == 404:
        return None
    if not respuesta.ok:
        raise ProformaError(cliente._extraer_error(respuesta), 502)
    return respuesta.json()


def consultar(usuario, numero: int, anio: int, mes: int) -> dict:
    if numero <= 0:
        raise ProformaError("N° de proforma inválido.")
    if not (1 <= mes <= 12 and 2000 <= anio <= 2100):
        raise ProformaError("Año o mes inválido.")
    cliente = _cliente_http(usuario)
    cabecera = _get(cliente, f"/v1/proformas/{numero}")
    if cabecera is None:
        raise ProformaError(f"No existe la proforma {numero}.", 404)
    deudas = _get(cliente, f"/v1/proformas/{numero}/deudas", params={"anio": anio, "mes": mes}) or []
    por_cliente = {str(d["nro_cliente"]).strip(): d.get("deudas") or [] for d in deudas}

    clientes = []
    for c in cabecera.get("clientes") or []:
        nro = str(c.get("nro_cliente") or "").strip()
        items = por_cliente.get(nro, [])
        total = sum((Decimal(str(i.get("importe") or 0)) for i in items), Decimal("0"))
        clientes.append({
            "nro_cliente": nro,
            "nombre": str(c.get("nombre") or "").strip(),
            "direccion": str(c.get("direccion") or "").strip(),
            "categoria": str(c.get("categoria_descripcion") or "").strip(),
            "cantidad": len(items),
            "total": f"{total:.2f}",
        })
    return {
        "numero": numero,
        "descripcion": str(cabecera.get("descripcion") or "").strip(),
        "clientes": clientes,
        "con_deuda": sum(1 for c in clientes if c["cantidad"]),
    }
