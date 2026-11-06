"""Cuándo avisar al equipo de un pago web sin factura (el cliente pagó, la liquidación falló).

Dos avisos como máximo por pago (decisión 2026-11-06):
- Primer error: "no se pudo facturar, se reintenta" con el motivo (`alertado_en`).
- Sin más reintentos automáticos -- rechazo definitivo o ALERTAS_INTENTOS_MAXIMOS intentos (el
  mismo tope que la web y el bot) --: "NO SE PUDO FACTURAR" con el motivo, para revisión a mano
  (`alerta_final_en`). Un rechazo definitivo en el primer intento manda solo este.
- Caja del SIIC fuera de horario o cerrada: sin alerta (se resuelve sola con la caja del día).
- Si después se factura, aviso de "resuelto".
- Resumen diario (tarea programada) de las que siguen con error.
Los mensajes se mandan en segundo plano (tarea Celery), nunca demoran el pago.
"""
from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from .models import SolicitudLiquidacion

# Mismo criterio que FacturacionRecibo::RECHAZOS_DEFINITIVOS de cessa-laravel.
RECHAZOS_DEFINITIVOS = (
    "ya figura pagado por otro medio",
    "La deuda no existe",
    "La deuda cambió desde que se generó el QR",
    "no coincide con la suma de los comprobantes",
)
ERRORES_DE_CAJA = ("fuera de horario", "la caja del día de hoy ha sido cerrada")

BANCOS = {"sip_bisa": "BISA", "bnb": "BNB"}


def es_definitivo(error: str) -> bool:
    return any(texto in (error or "") for texto in RECHAZOS_DEFINITIVOS)


def es_de_caja(error: str) -> bool:
    return any(texto in (error or "") for texto in ERRORES_DE_CAJA)


def _bs(monto) -> str:
    numero = f"{monto:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")  # 3,068.60 → 3.068,60
    return f"Bs. {numero}"


def _enlace() -> str:
    return f"\nVer: {settings.PANEL_URL.rstrip('/')}/pagos-web" if settings.PANEL_URL else ""


def describir(solicitud: SolicitudLiquidacion) -> str:
    return (
        f"Cliente {solicitud.nro_cliente} · {_bs(solicitud.monto)} · {BANCOS.get(solicitud.banco, solicitud.banco or '—')}\n"
        f"Recibo {solicitud.alias} · pagado {timezone.localtime(solicitud.fecha_pago):%d/%m/%Y %H:%M}\n"
        f"Intentos: {solicitud.intentos}"
    )


def evaluar(solicitud: SolicitudLiquidacion) -> None:
    """Llamar después de cada liquidar_solicitud(). Decide si hay que avisar y lo encola."""
    from services.alertas import hay_canal_configurado

    from .tasks import enviar_alerta  # import tardío: tasks importa este módulo

    # Sin canal no se encola nada (ni se marca: si después se configura, avisa de lo nuevo).
    if not hay_canal_configurado():
        return

    pago = SolicitudLiquidacion.objects.filter(pk=solicitud.pk)
    if solicitud.estado == SolicitudLiquidacion.Estado.FACTURADO:
        if solicitud.alertado_en or solicitud.alerta_final_en:
            enviar_alerta.delay("Pago web resuelto: ya tiene factura", describir(solicitud) + _enlace())
            pago.update(alertado_en=None, alerta_final_en=None)
        return

    if solicitud.estado != SolicitudLiquidacion.Estado.ERROR or es_de_caja(solicitud.error):
        return
    cuerpo = f"{describir(solicitud)}\nMotivo: {solicitud.error[:700]}{_enlace()}"
    ahora = timezone.now()
    # Se marca antes de encolar (update condicional): dos procesos casi juntos no avisan dos veces.
    if es_definitivo(solicitud.error) or solicitud.intentos >= settings.ALERTAS_INTENTOS_MAXIMOS:
        if pago.filter(alerta_final_en__isnull=True).update(alerta_final_en=ahora, alertado_en=solicitud.alertado_en or ahora):
            enviar_alerta.delay("Pago web NO SE PUDO FACTURAR: revisar a mano", cuerpo)
    elif pago.filter(alertado_en__isnull=True).update(alertado_en=ahora):
        enviar_alerta.delay("Pago web sin factura: se reintenta", cuerpo)


def resumen_de_errores() -> tuple[str, str] | None:
    """Las liquidaciones que siguen con error (cualquier día), o None si no hay."""
    pendientes = list(
        SolicitudLiquidacion.objects.filter(estado=SolicitudLiquidacion.Estado.ERROR)
        .defer("comprobante_pdf")
        .order_by("recibido_en")
    )
    if not pendientes:
        return None
    lineas = [
        f"• {s.nro_cliente} · {_bs(s.monto)} · {timezone.localtime(s.recibido_en):%d/%m} · {s.error[:120]}"
        for s in pendientes[:30]
    ]
    if len(pendientes) > 30:
        lineas.append(f"… y {len(pendientes) - 30} más")
    return f"{len(pendientes)} pagos web siguen sin factura", "\n".join(lineas) + _enlace()
