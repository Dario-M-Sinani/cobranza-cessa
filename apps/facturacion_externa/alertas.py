"""Cuándo avisar al equipo de un pago web sin factura (el cliente pagó, la liquidación falló).

- Rechazo definitivo (no se arregla reintentando): alerta inmediata.
- Otro error: alerta recién al llegar a ALERTAS_INTENTOS_MINIMOS intentos fallidos.
- Caja del SIIC fuera de horario o cerrada: sin alerta (se resuelve sola con la caja del día).
- Una vez por liquidación (`alertado_en`); si después se factura, aviso de "resuelto".
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

    if solicitud.estado == SolicitudLiquidacion.Estado.FACTURADO:
        if solicitud.alertado_en:
            enviar_alerta.delay("Pago web resuelto: ya tiene factura", describir(solicitud) + _enlace())
            SolicitudLiquidacion.objects.filter(pk=solicitud.pk).update(alertado_en=None)
        return

    if solicitud.estado != SolicitudLiquidacion.Estado.ERROR or solicitud.alertado_en or es_de_caja(solicitud.error):
        return
    definitivo = es_definitivo(solicitud.error)
    if not definitivo and solicitud.intentos < settings.ALERTAS_INTENTOS_MINIMOS:
        return

    asunto = "Pago web SIN FACTURA: revisar a mano" if definitivo else "Pago web sin factura: sigue fallando"
    cuerpo = f"{describir(solicitud)}\nMotivo: {solicitud.error[:700]}{_enlace()}"
    # Se marca antes de encolar: así dos procesos que terminan casi juntos no avisan dos veces.
    marcadas = SolicitudLiquidacion.objects.filter(pk=solicitud.pk, alertado_en__isnull=True).update(alertado_en=timezone.now())
    if marcadas:
        enviar_alerta.delay(asunto, cuerpo)


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
