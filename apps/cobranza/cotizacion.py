"""Cotización del dólar: la del SIIC (ADMINDTA.COTIZA, con la que el SIIC cierra las cajas: es la que
manda) junto a la oficial del BCB (referencia). Queda una fila por día en CotizacionDolar como
constancia de qué valía cada una y cuándo se leyó.

- La del SIIC se lee con el token de la Nest de quien mira (no hay otro usuario de la Nest que lo
  pueda hacer en segundo plano) y se vuelve a leer cada pocos minutos: si a la mañana todavía no
  estaba cargada, aparece en cuanto la carguen.
- La del BCB la lee una tarea cada hora (apps/cobranza/tasks.py) y, si falta, la primera consulta.

Alertas para la supervisora: sin cotización en el SIIC hoy (las cajas no van a poder cerrar) y
cotización del SIIC distinta de la oficial.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone

from apps.usuarios.models import Usuario
from services.cobranzas_banco_client import CajeraCobranzasClient, CobranzasBancoError
from services.tipo_cambio import TipoCambioError, tipo_cambio_oficial

from .models import CotizacionDolar

RELEER_SIIC_CADA = timedelta(minutes=10)
RELEER_BCB_CADA = timedelta(hours=1)
TOLERANCIA = Decimal("0.005")


def _del_dia(fecha=None) -> CotizacionDolar:
    fila, _ = CotizacionDolar.objects.get_or_create(fecha=fecha or timezone.localdate())
    return fila


def actualizar_bcb(fila: CotizacionDolar | None = None, forzar: bool = False) -> CotizacionDolar:
    fila = fila or _del_dia()
    ahora = timezone.now()
    if not forzar and fila.bcb is not None and fila.bcb_leida_en and ahora - fila.bcb_leida_en < RELEER_BCB_CADA:
        return fila
    try:
        oficial = tipo_cambio_oficial()
    except TipoCambioError as exc:
        fila.bcb_error = str(exc)[:500]
        fila.save(update_fields=["bcb_error", "actualizada_en"])
        return fila
    fila.bcb, fila.bcb_vigencia, fila.bcb_fuente = oficial.valor, oficial.vigencia, oficial.fuente
    fila.bcb_leida_en, fila.bcb_error = ahora, ""
    fila.save()
    return fila


def actualizar_siic(fila: CotizacionDolar, usuario) -> CotizacionDolar:
    if getattr(usuario, "origen", "") != Usuario.Origen.NEST:
        return fila  # sin token de la Nest no hay cómo preguntarle al SIIC
    ahora = timezone.now()
    if fila.siic_leida_en and ahora - fila.siic_leida_en < RELEER_SIIC_CADA and not fila.siic_error:
        return fila
    if not cache.add(f"cotizacion-siic-{fila.fecha}", 1, timeout=30):  # varias pantallas a la vez: una sola consulta
        return fila
    try:
        datos = CajeraCobranzasClient(usuario).cotizacion_dolar(fila.fecha.strftime("%Y%m%d"))
    except CobranzasBancoError as exc:
        fila.siic_error = str(exc)[:500]
        fila.save(update_fields=["siic_error", "actualizada_en"])
        return fila
    valor = datos.get("cotizacion")
    fila.siic = Decimal(str(valor)) if valor not in (None, "") else None
    fila.siic_leida_en, fila.siic_error = ahora, ""
    fila.save()
    return fila


def estado(fila: CotizacionDolar) -> dict:
    alertas = []
    if fila.siic_leida_en and fila.siic is None:
        alertas.append({
            "tipo": "sin_siic",
            "mensaje": "Hoy no hay cotización del dólar cargada en el SIIC: las cajas no van a poder cerrar hasta que se cargue.",
        })
    if fila.siic is not None and fila.bcb is not None and abs(fila.siic - fila.bcb) >= TOLERANCIA:
        alertas.append({
            "tipo": "diferencia",
            "mensaje": f"La cotización del SIIC (Bs {fila.siic:.2f}) no coincide con la oficial del BCB (Bs {fila.bcb:.2f}).",
        })
    return {
        "fecha": fila.fecha.isoformat(),
        "siic": f"{fila.siic:.4f}" if fila.siic is not None else None,
        "siic_leida_en": fila.siic_leida_en,
        "siic_error": fila.siic_error,
        "bcb": f"{fila.bcb:.4f}" if fila.bcb is not None else None,
        "bcb_vigencia": fila.bcb_vigencia,
        "bcb_fuente": fila.bcb_fuente,
        "bcb_leida_en": fila.bcb_leida_en,
        "bcb_error": fila.bcb_error,
        "alertas": alertas,
    }


def de_hoy(usuario) -> dict:
    fila = _del_dia()
    actualizar_siic(fila, usuario)
    if fila.bcb is None and not fila.bcb_error:
        actualizar_bcb(fila)
    return estado(fila)


def historial(desde, hasta) -> list[dict]:
    return [estado(f) for f in CotizacionDolar.objects.filter(fecha__gte=desde, fecha__lte=hasta).order_by("-fecha")]
