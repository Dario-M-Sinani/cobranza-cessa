from celery import shared_task

from services.alertas import hay_canal_configurado, notificar

from .alertas import resumen_de_errores


@shared_task
def enviar_alerta(asunto: str, texto: str):
    return notificar(asunto, texto)


@shared_task
def resumen_diario_errores():
    """Programada a las 08:00 (CELERY_BEAT_SCHEDULE): solo avisa si hay pagos web sin factura."""
    if not hay_canal_configurado():
        return 0
    resumen = resumen_de_errores()
    return notificar(*resumen) if resumen else 0


@shared_task
def conciliacion_diaria():
    """Programada a las 07:30: concilia el día anterior y avisa solo si hay diferencias."""
    from datetime import timedelta

    from django.utils import timezone

    from .conciliacion import conciliar

    reporte = conciliar(timezone.localdate() - timedelta(days=1))
    if reporte.diferencias and hay_canal_configurado():
        notificar(f"Conciliación de pagos: {len(reporte.diferencias)} diferencias", reporte.texto())
    return len(reporte.diferencias)


@shared_task
def reintento_nocturno():
    """Programada a las 02:00 (REINTENTO_NOCTURNO_HORA): un intento más de los pagos que siguen en
    error -- ya agotaron los reintentos automáticos de la web o del bot, o se cortaron por un
    rechazo definitivo que pudo ser pasajero (ej. "la deuda cambió" falso del 06/11) -- y que nadie
    regularizó (los descartados no se tocan). El resultado sigue las alertas de siempre: si se
    factura llega "resuelto"; si no, no se repite el aviso final. La web y el bot se enteran
    consultando el alias en el gateway (idempotente: nunca se paga dos veces)."""
    from apps.auditoria.models import LogAuditoria

    from .models import SolicitudLiquidacion
    from .panel import _lock_liquidacion
    from .services import liquidar_solicitud

    facturados = 0
    pendientes = SolicitudLiquidacion.objects.filter(estado=SolicitudLiquidacion.Estado.ERROR).defer("comprobante_pdf")
    for solicitud in pendientes.order_by("recibido_en"):
        with _lock_liquidacion(solicitud.pk) as obtenido:
            if not obtenido:
                continue
            solicitud.refresh_from_db()
            if solicitud.estado != SolicitudLiquidacion.Estado.ERROR:
                continue
            LogAuditoria.objects.create(
                usuario=None, accion="Liquidación web: reintento nocturno",
                entidad_afectada=f"SolicitudLiquidacion:{solicitud.pk}",
            )
            if liquidar_solicitud(solicitud).estado == SolicitudLiquidacion.Estado.FACTURADO:
                facturados += 1
    return facturados
