from django.core.serializers.json import DjangoJSONEncoder
from django.db import models


class SolicitudLiquidacion(models.Model):
    """Un cobro ya pagado en un sistema externo (hoy: el pago QR web de
    cessa-laravel, `Recibo` en ese repo) que ese sistema pide liquidar como
    factura real en api-cobranzas-bancos -- este backend es el único que
    tiene red hacia esa API (ver services/cobranzas_banco_client.py).

    No se relaciona con `apps.cobranza.Factura`: esa es la factura de las
    TransaccionQR/CobroEfectivo propias de este panel de cajera, generadas
    puertas adentro. Un `SolicitudLiquidacion` en cambio siempre llega
    empujado desde afuera, con el detalle de deuda ya armado por quien pagó
    -- son dos orígenes distintos que conviene no mezclar en un solo modelo.
    """

    class Origen(models.TextChoices):
        CESSA_LARAVEL_WEB = "cessa_laravel_web", "cessa-laravel (pago QR web)"

    class Estado(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente"
        FACTURADO = "facturado", "Facturado"
        ERROR = "error", "Error"

    # Único por diseño en el sistema de origen (alias del Recibo de
    # cessa-laravel) -- sirve como clave de idempotencia: si el mismo aviso
    # llega dos veces (reintento de red del lado de cessa-laravel), no se
    # crea una segunda solicitud ni se vuelve a pagar en api-cobranzas-bancos.
    alias = models.CharField(max_length=100, unique=True)
    origen = models.CharField(max_length=30, choices=Origen.choices, default=Origen.CESSA_LARAVEL_WEB)
    nro_cliente = models.CharField(max_length=20)
    monto = models.DecimalField(max_digits=12, decimal_places=2)
    moneda = models.CharField(max_length=3, default="BOB")
    # Snapshot crudo de deuda (misma forma que `Recibo::debt_items` de
    # cessa-laravel / `ItemDeuda`) -- se transforma recién al momento de
    # pagar (ver services.construir_detalle), nunca se guarda ya transformado.
    detalle = models.JSONField(encoder=DjangoJSONEncoder)
    fecha_pago = models.DateTimeField()
    numero_orden_originante = models.CharField(max_length=50, blank=True, default="")

    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.PENDIENTE)
    cobranzas_uuid = models.CharField(max_length=100, blank=True, default="")
    comprobante_pdf = models.BinaryField(null=True, blank=True)
    intentos = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True, default="")

    recibido_en = models.DateTimeField(auto_now_add=True)
    procesado_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-recibido_en"]

    def __str__(self):
        return f"SolicitudLiquidacion#{self.pk} [{self.estado}] {self.alias}"
