from django.core.serializers.json import DjangoJSONEncoder
from django.db import models

from apps.facturacion_externa.models import SolicitudLiquidacion


class PagoKiosco(models.Model):
    """Un QR generado en una pantalla táctil de autoservicio (red interna, sin internet).

    A diferencia de la web y el bot, el kiosco no habla con SIP ni con el SIIC: todo lo hace este
    gateway. Mientras el QR está en pantalla vive acá; cuando SIP confirma el pago se crea su
    `SolicitudLiquidacion` (origen KIOSCO) y se factura con el mismo `liquidar_solicitud` de
    siempre, así aparece en el panel, el reporte Excel, las alertas y el reintento de las 02:00."""

    class Estado(models.TextChoices):
        PENDIENTE = "pendiente", "QR en pantalla, sin pagar"
        # Pagado en SIP; la factura se sigue en `solicitud`.
        PAGADO = "pagado", "Pagado"
        VENCIDO = "vencido", "Vencido sin pagar"
        CANCELADO = "cancelado", "Cancelado sin pagar"

    alias = models.CharField(max_length=50, unique=True)
    # Nombre de la pantalla (KIOSCO_TOKENS): sirve para saber en qué oficina se cobró.
    kiosco = models.CharField(max_length=50)
    nro_cliente = models.CharField(max_length=20)
    nombre_cliente = models.CharField(max_length=200, blank=True, default="")
    # Los N comprobantes más antiguos tal cual vinieron del SIIC (+ importe_firmado), igual que
    # `Recibo::debt_items` de cessa-laravel y el detalle que manda el bot.
    detalle = models.JSONField(encoder=DjangoJSONEncoder)
    monto = models.DecimalField(max_digits=12, decimal_places=2)
    glosa = models.CharField(max_length=30)

    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.PENDIENTE)
    creado_en = models.DateTimeField(auto_now_add=True)
    vence_en = models.DateTimeField()
    pagado_en = models.DateTimeField(null=True, blank=True)
    numero_orden = models.CharField(max_length=50, blank=True, default="")
    # Última consulta de estado a SIP: la pantalla pregunta cada pocos segundos y no hace falta
    # consultar a SIP en cada pregunta.
    consultado_sip_en = models.DateTimeField(null=True, blank=True)
    solicitud = models.OneToOneField(
        SolicitudLiquidacion, null=True, blank=True, on_delete=models.PROTECT, related_name="pago_kiosco"
    )
    # Se guarda la primera vez que se muestra (sale del comprobante de api-cobranzas).
    nro_factura = models.CharField(max_length=50, blank=True, default="")
    # Veces que se mandó la factura por correo (tope en services.MAX_CORREOS_POR_PAGO).
    correos_enviados = models.PositiveSmallIntegerField(default=0)
    # Ídem por WhatsApp (tope en services.MAX_WHATSAPP_POR_PAGO).
    whatsapp_enviados = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-creado_en"]

    def __str__(self):
        return f"PagoKiosco#{self.pk} [{self.estado}] {self.alias}"


class EnvioFactura(models.Model):
    """Cada vez que la pantalla mandó (o intentó mandar) la factura por correo o WhatsApp: a quién,
    cuándo y si salió. Se ve en el detalle del pago en el panel de cobranza."""

    class Canal(models.TextChoices):
        CORREO = "correo", "Correo"
        WHATSAPP = "whatsapp", "WhatsApp"

    pago = models.ForeignKey(PagoKiosco, on_delete=models.PROTECT, related_name="envios")
    canal = models.CharField(max_length=10, choices=Canal.choices)
    destino = models.CharField(max_length=254)
    # True: el correo/celular registrado en la web; False: lo escribió el cliente en la pantalla.
    registrado = models.BooleanField(default=False)
    exitoso = models.BooleanField(default=True)
    error = models.CharField(max_length=300, blank=True, default="")
    enviado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["enviado_en"]

    def __str__(self):
        return f"EnvioFactura#{self.pk} {self.canal} {self.pago.alias}"
