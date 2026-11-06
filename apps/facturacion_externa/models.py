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
        CESSA_LARAVEL_PANEL = "cessa_laravel_panel", "cessa-laravel (QR generado desde el panel)"
        BOT_TELEGRAM = "bot_telegram", "bot-telegram (pago QR por Telegram)"

    # El canal sale del alias (la web y el bot usan la misma API key): CESSA-WEB- lo genera el
    # cliente en la web, CESSA-ADMIN- el panel de administración de la web, CESSA-BOT- el bot.
    PREFIJO_BOT_TELEGRAM = "CESSA-BOT-"
    PREFIJO_PANEL_WEB = "CESSA-ADMIN-"

    @classmethod
    def origen_de_alias(cls, alias: str) -> str:
        if alias.startswith(cls.PREFIJO_BOT_TELEGRAM):
            return cls.Origen.BOT_TELEGRAM
        if alias.startswith(cls.PREFIJO_PANEL_WEB):
            return cls.Origen.CESSA_LARAVEL_PANEL
        return cls.Origen.CESSA_LARAVEL_WEB

    class Estado(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente"
        FACTURADO = "facturado", "Facturado"
        ERROR = "error", "Error"
        # Revisado a mano y cerrado sin factura (ej. pruebas viejas, o regularizado por otra vía):
        # no se vuelve a intentar pagar aunque cessa-laravel lo reenvíe, y no cuenta para alertas.
        DESCARTADO = "descartado", "Descartado"

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
    # Banco por el que entró la plata, según cessa-laravel (`Recibo::provider`: "sip_bisa", "bnb").
    # Define el `banco_id` del documento en el SIIC (ver services.banco_id_para); vacío = el de
    # COBRANZAS_BANCO_DOCUMENTO_BANCO_ID, como antes.
    banco = models.CharField(max_length=30, blank=True, default="")

    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.PENDIENTE)
    cobranzas_uuid = models.CharField(max_length=100, blank=True, default="")
    comprobante_pdf = models.BinaryField(null=True, blank=True)
    intentos = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True, default="")

    recibido_en = models.DateTimeField(auto_now_add=True)
    procesado_en = models.DateTimeField(null=True, blank=True)
    # Cuándo se avisó al equipo que este pago quedó sin factura (ver alertas.py); una sola vez.
    alertado_en = models.DateTimeField(null=True, blank=True)
    # Cuándo se avisó que ya NO se va a facturar solo (rechazo definitivo o intentos agotados).
    alerta_final_en = models.DateTimeField(null=True, blank=True)
    # Por qué se descartó (estado DESCARTADO); quién y cuándo queda en la auditoría.
    nota_descarte = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-recibido_en"]

    def __str__(self):
        return f"SolicitudLiquidacion#{self.pk} [{self.estado}] {self.alias}"


class AvisoSip(models.Model):
    """Cada aviso de pago que SIP manda al callback único (sip_callback.py), tal cual llegó y con
    Basic Auth válido. De acá salen el pagador, su documento y el N° de orden para el reporte de
    cobros: sirve para todos los canales (web, panel de la web, bot), sin pedirle nada a cessa-laravel
    ni al bot. Puede haber varios por alias (SIP reintenta); el reporte usa el último."""

    alias = models.CharField(max_length=100, db_index=True)
    datos = models.JSONField(encoder=DjangoJSONEncoder)
    recibido_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-recibido_en"]

    def __str__(self):
        return f"AvisoSip#{self.pk} {self.alias}"
