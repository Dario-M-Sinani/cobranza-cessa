from celery import shared_task

from .models import Factura, TransaccionQR
from .services import MAX_INTENTOS_AUTOMATICOS_FACTURACION, enviar_factura_a_siic, verificar_pago


@shared_task
def verificar_transacciones_pendientes():
    """Tarea periódica: por cada TransaccionQR en pendiente_confirmacion,
    consulta su estado real contra MC4/SIP y aplica la transición que
    corresponda (incluyendo la creación de Factura si quedó pagada). Ver
    `services.verificar_pago` para el detalle."""
    ids = list(
        TransaccionQR.objects.filter(
            estado=TransaccionQR.Estado.PENDIENTE_CONFIRMACION
        ).values_list("id", flat=True)
    )
    for transaccion_id in ids:
        transaccion = TransaccionQR.objects.get(pk=transaccion_id)
        verificar_pago(transaccion)


@shared_task
def enviar_facturas_pendientes():
    """Tarea periódica: envía a api-cobranzas-bancos las Facturas que
    todavía no se registraron ahí -- las recién creadas (PENDIENTE) y las
    que fallaron sin agotar el tope de reintentos automáticos (ERROR). Las
    que ya agotaron el tope quedan para revisión manual (ver
    `services.reintentar_facturacion`, sin tope). Mismo patrón poll-based
    que `verificar_transacciones_pendientes` -- ver `services.
    enviar_factura_a_siic` para el detalle de cada envío."""
    ids = list(
        Factura.objects.filter(estado_envio=Factura.EstadoEnvio.PENDIENTE).values_list("id", flat=True)
    ) + list(
        Factura.objects.filter(
            estado_envio=Factura.EstadoEnvio.ERROR, intentos__lt=MAX_INTENTOS_AUTOMATICOS_FACTURACION
        ).values_list("id", flat=True)
    )
    for factura_id in ids:
        enviar_factura_a_siic(Factura.objects.get(pk=factura_id))
