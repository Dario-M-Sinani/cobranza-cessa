"""Agregaciones de solo lectura para los dashboards de supervisor/admin
(Épica G/H de HISTORIAS_USUARIO.md) -- a diferencia de services.py, nada acá
muta estado, solo arma resúmenes a partir de lo que ya existe.

Nota de alcance (pregunta 8 de REQUISITOS_COBRANZA.md, sin responder): no
existe todavía un concepto de sucursal/agencia en el modelo de Usuario, así
que "mis cajas" para un supervisor no se puede filtrar por sucursal -- hoy
el resumen es el mismo para supervisor y administrador (todas las cajas/
cobros), y la diferencia entre ambos roles queda solo en qué secciones
extra arma el frontend (rango de fechas, comparativa por cajero, etc. son
igual de visibles vía API para ambos roles)."""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from .models import Caja, CobroEfectivo, Factura, TransaccionQR


def _rango_del_dia(desde: date, hasta: date) -> tuple[datetime, datetime]:
    tz = timezone.get_current_timezone()
    inicio = timezone.make_aware(datetime.combine(desde, time.min), tz)
    fin = timezone.make_aware(datetime.combine(hasta, time.max), tz)
    return inicio, fin


def resumen_dashboard(desde: date, hasta: date) -> dict:
    inicio, fin = _rango_del_dia(desde, hasta)

    qr_pagados = TransaccionQR.objects.filter(
        estado=TransaccionQR.Estado.PAGADO, creado_en__range=(inicio, fin)
    )
    cobros_efectivo = CobroEfectivo.objects.filter(creado_en__range=(inicio, fin))

    qr_agg = qr_pagados.aggregate(cantidad=Count("id"), monto=Sum("monto_snapshot"))
    efectivo_agg = cobros_efectivo.aggregate(cantidad=Count("id"), monto=Sum("monto_snapshot"))

    monto_qr = qr_agg["monto"] or Decimal("0.00")
    monto_efectivo = efectivo_agg["monto"] or Decimal("0.00")

    facturas_registradas = Factura.objects.filter(
        Q(transaccion_qr__in=qr_pagados) | Q(cobro_efectivo__in=cobros_efectivo)
    ).count()

    cajas_en_rango = Caja.objects.filter(abierta_en__range=(inicio, fin)).select_related(
        "cajero", "abierta_por", "cerrada_por"
    )

    por_cajero: dict[str, dict] = {}

    def _acumular(filas):
        for fila in filas:
            username = fila["usuario__username"]
            entrada = por_cajero.setdefault(
                username, {"cajero": username, "cantidad_cobros": 0, "monto_total": Decimal("0.00")}
            )
            entrada["cantidad_cobros"] += fila["cantidad"]
            entrada["monto_total"] += fila["monto"] or Decimal("0.00")

    _acumular(qr_pagados.values("usuario__username").annotate(cantidad=Count("id"), monto=Sum("monto_snapshot")))
    _acumular(
        cobros_efectivo.values("usuario__username").annotate(cantidad=Count("id"), monto=Sum("monto_snapshot"))
    )

    return {
        "desde": desde,
        "hasta": hasta,
        "monto_total": monto_qr + monto_efectivo,
        "cantidad_cobros": (qr_agg["cantidad"] or 0) + (efectivo_agg["cantidad"] or 0),
        "por_forma_pago": {
            "qr": {"cantidad": qr_agg["cantidad"] or 0, "monto": monto_qr},
            "efectivo": {"cantidad": efectivo_agg["cantidad"] or 0, "monto": monto_efectivo},
        },
        "facturas_registradas": facturas_registradas,
        "cajas": list(cajas_en_rango),
        "por_cajero": sorted(por_cajero.values(), key=lambda fila: fila["monto_total"], reverse=True),
    }


def resumen_caja(caja: Caja) -> dict:
    """Corte de caja: todo lo cobrado (QR pagado + efectivo) vinculado a
    esta Caja puntual -- a diferencia de `resumen_dashboard()` (por rango de
    fechas, todas las cajas), esto es "el turno de este cajero". Sirve tanto
    para una caja ya cerrada (el cierre en sí) como para una todavía
    abierta (total corrido del turno)."""
    qr_pagados = TransaccionQR.objects.filter(caja=caja, estado=TransaccionQR.Estado.PAGADO)
    qr_pendientes = TransaccionQR.objects.filter(
        caja=caja,
        estado__in=[TransaccionQR.Estado.GENERADO, TransaccionQR.Estado.PENDIENTE_CONFIRMACION],
    )
    cobros_efectivo = CobroEfectivo.objects.filter(caja=caja)

    qr_agg = qr_pagados.aggregate(cantidad=Count("id"), monto=Sum("monto_snapshot"))
    efectivo_agg = cobros_efectivo.aggregate(cantidad=Count("id"), monto=Sum("monto_snapshot"))

    monto_qr = qr_agg["monto"] or Decimal("0.00")
    monto_efectivo = efectivo_agg["monto"] or Decimal("0.00")

    facturas_registradas = Factura.objects.filter(
        Q(transaccion_qr__in=qr_pagados) | Q(cobro_efectivo__in=cobros_efectivo)
    ).count()

    return {
        "monto_total": monto_qr + monto_efectivo,
        "cantidad_cobros": (qr_agg["cantidad"] or 0) + (efectivo_agg["cantidad"] or 0),
        "por_forma_pago": {
            "qr": {"cantidad": qr_agg["cantidad"] or 0, "monto": monto_qr},
            "efectivo": {"cantidad": efectivo_agg["cantidad"] or 0, "monto": monto_efectivo},
        },
        "facturas_registradas": facturas_registradas,
        # QR generados en este turno que todavía no se confirmaron como
        # pagados (el cliente no escaneó, o sigue esperando SIP) -- importa
        # para el cierre: son compromisos abiertos que el cajero se lleva
        # sin resolver.
        "qr_pendientes_de_confirmar": qr_pendientes.count(),
    }
