"""Cobrar con otro documento en vez de billetes (como el "Tipo de pago" de savePagoDialog.xhtml en la
app Java): cheque, depósito, tarjeta, Sigma, dividendo. Solo con la caja del SIIC de la cajera
(CAJAS_POR_CAJERA): los tipos y bancos salen de los catálogos del SIIC, y el cobro se factura con
`/pagar-otro-documento`, así que en el arqueo del cierre figura como ese documento y no como efectivo.

Reglas de la app Java que se mantienen:
- Cheque: fecha de emisión y de vencimiento; el vencimiento no puede ser anterior a la emisión, ni
  estar a más de 27 días de ella, ni a 3 días o menos de hoy.
- Cajeras de bancos (entidad distinta de CESSA): el cobro tiene que ser menor a Bs 50.000.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from services.cobranzas_banco_client import CajeraCobranzasClient, CobranzasBancoError

ENTIDAD_CESSA = 1
TOPE_BANCOS = Decimal("50000")
# Los que no se eligen al cobrar: el efectivo es la pestaña Efectivo y el QR, la pestaña QR.
NO_SE_ELIGEN = {"EFECTIVO", "DOLARES", "PAGO POR QR"}
DIAS_MAX_CHEQUE = 27
DIAS_MIN_PARA_VENCER = 3


class FormaPagoInvalida(Exception):
    pass


def disponible_para(usuario) -> bool:
    return bool(settings.CAJAS_POR_CAJERA and getattr(usuario, "origen", "") == "nest")


def catalogo(usuario) -> dict:
    """Tipos de documento y bancos del SIIC para la pestaña "Otro documento"."""
    if not disponible_para(usuario):
        return {"disponible": False, "entes": [], "bancos": []}
    cliente = CajeraCobranzasClient(usuario)
    try:
        entes = [e for e in cliente.catalogo("entes") if str(e.get("descripcion", "")).strip().upper() not in NO_SE_ELIGEN]
        bancos = cliente.catalogo("bancos")
    except CobranzasBancoError as exc:
        raise FormaPagoInvalida(f"No se pudieron leer los tipos de pago del SIIC: {exc}") from exc
    return {
        "disponible": True,
        "entes": [{"id": int(e["id"]), "descripcion": str(e["descripcion"]).strip()} for e in entes],
        "bancos": [{"id": int(b["id"]), "descripcion": str(b["descripcion"]).strip()} for b in bancos],
    }


def _fecha(valor, campo: str) -> date:
    try:
        return datetime.strptime(str(valor or "").replace("-", ""), "%Y%m%d").date()
    except ValueError:
        raise FormaPagoInvalida(f"La fecha de {campo} del cheque no es válida.")


def validar_documento(usuario, datos: dict) -> dict:
    """Valida lo que mandó el panel contra los catálogos del SIIC y devuelve el documento a guardar."""
    if not disponible_para(usuario):
        raise FormaPagoInvalida("El pago con otro documento solo está disponible con la caja del SIIC.")
    if not isinstance(datos, dict):
        raise FormaPagoInvalida("Faltan los datos del documento.")
    cat = catalogo(usuario)
    try:
        ente_id, banco_id = int(datos.get("ente_id")), int(datos.get("banco_id"))
    except (TypeError, ValueError):
        raise FormaPagoInvalida("Elige el tipo de documento y el banco.")
    ente = next((e for e in cat["entes"] if e["id"] == ente_id), None)
    banco = next((b for b in cat["bancos"] if b["id"] == banco_id), None)
    if ente is None or banco is None:
        raise FormaPagoInvalida("Ese tipo de documento o banco no existe en el SIIC.")
    numero = str(datos.get("numero") or "").strip()
    if not numero or len(numero) > 20:
        raise FormaPagoInvalida("El número del documento es obligatorio (hasta 20 caracteres).")

    hoy = timezone.localdate()
    if ente["descripcion"].upper() == "CHEQUE":
        emision = _fecha(datos.get("fecha_registro"), "emisión")
        vence = _fecha(datos.get("fecha_vencimiento"), "vencimiento")
        if vence < emision:
            raise FormaPagoInvalida("El vencimiento del cheque es anterior a su emisión.")
        if (vence - emision).days > DIAS_MAX_CHEQUE:
            raise FormaPagoInvalida(f"El vencimiento del cheque está a más de {DIAS_MAX_CHEQUE} días de su emisión.")
        if (vence - hoy).days <= DIAS_MIN_PARA_VENCER:
            raise FormaPagoInvalida(
                "El cheque está vencido o vence en 3 días o menos: no se puede recibir."
                if vence >= hoy else "El cheque está vencido."
            )
    else:
        emision = vence = hoy
    return {
        "ente_id": ente_id,
        "ente": ente["descripcion"],
        "banco_id": banco_id,
        "banco": banco["descripcion"],
        "numero": numero,
        "fecha_registro": emision.strftime("%Y%m%d"),
        "fecha_vencimiento": vence.strftime("%Y%m%d"),
    }


def validar_tope(usuario, total: Decimal) -> None:
    """Como la app Java: en las cajas de los bancos, solo cobros menores a Bs 50.000."""
    entidad = getattr(usuario, "entidad_financiera_id", None)
    if entidad and entidad != ENTIDAD_CESSA and total >= TOPE_BANCOS:
        raise FormaPagoInvalida(
            "No se puede cobrar Bs 50.000 o más en una entidad financiera: ese pago se hace en oficinas de CESSA."
        )


def documento_para_siic(cobro) -> dict:
    """El `documento` de `/pagar-otro-documento` para un cobro con documento."""
    d = cobro.documento
    return {
        "ente_id": int(d["ente_id"]),
        "moneda": "B",
        "banco_id": int(d["banco_id"]),
        "numero": d["numero"],
        "importe": float(cobro.monto_snapshot),
        "fecha": int(d["fecha_registro"]),
        "fecha_registro": int(d["fecha_registro"]),
        "fecha_vencimiento": int(d["fecha_vencimiento"]),
    }
