from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from apps.facturacion_externa.models import SolicitudLiquidacion

from . import services
from .models import PagoKiosco


@shared_task
def revisar_pagos_kiosco():
    """Cada 60 s (CELERY_BEAT_SCHEDULE). El kiosco no tiene a nadie que reintente por él (la web
    y el bot reintentan solos), así que lo hace el gateway, con sus mismas reglas:

    1. QR todavía abiertos: se consulta a SIP por si el aviso no llegó y la pantalla ya se cerró
       (pagado → se factura; vencido → se da de baja).
    2. Pagos del kiosco sin factura: un intento por minuto (services.se_reintenta_solo).
    """
    for pago in PagoKiosco.objects.filter(estado=PagoKiosco.Estado.PENDIENTE):
        services.consultar_sip(pago)

    facturados = 0
    hace_un_minuto = timezone.now() - timedelta(minutes=1)
    pendientes = SolicitudLiquidacion.objects.filter(
        origen=SolicitudLiquidacion.Origen.KIOSCO,
        estado__in=[SolicitudLiquidacion.Estado.PENDIENTE, SolicitudLiquidacion.Estado.ERROR],
    ).defer("comprobante_pdf")
    for solicitud in pendientes.order_by("recibido_en"):
        if solicitud.estado == SolicitudLiquidacion.Estado.PENDIENTE:
            # Recién creada: la está facturando quien registró el pago. Si quedó así más de un
            # minuto, ese proceso se cortó a la mitad.
            if solicitud.recibido_en > hace_un_minuto:
                continue
        elif not services.se_reintenta_solo(solicitud):
            continue
        if services.facturar(solicitud).estado == SolicitudLiquidacion.Estado.FACTURADO:
            facturados += 1
    return facturados
