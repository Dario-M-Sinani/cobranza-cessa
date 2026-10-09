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
