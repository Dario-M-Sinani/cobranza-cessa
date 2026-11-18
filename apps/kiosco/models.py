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


class PantallaKiosco(models.Model):
    """Lo que el gateway sabe de cada pantalla (KIOSCO_TOKENS) para vigilarla (apps/kiosco/salud.py):
    cuándo preguntó por última vez y qué problemas se avisaron por Telegram."""

    nombre = models.CharField(max_length=50, unique=True)
    creada_en = models.DateTimeField(auto_now_add=True)
    # Última vez que la pantalla preguntó su estado (lo hace cada minuto mientras está prendida).
    ultimo_contacto = models.DateTimeField(null=True, blank=True)
    # Última pasada de la tarea `vigilar_kioscos` (Celery beat cada minuto): si se atrasa, el worker
    # que detecta pagos y factura no está corriendo.
    revisada_en = models.DateTimeField(null=True, blank=True)
    # Problemas vigentes ya avisados ("" = todo bien) y el cambio que espera confirmarse en la
    # siguiente pasada, para no avisar por un corte de unos segundos.
    problemas_avisados = models.CharField(max_length=500, blank=True, default="")
    problemas_nuevos = models.CharField(max_length=500, blank=True, default="")
    veces_nuevos = models.PositiveSmallIntegerField(default=0)
    # Cómo se reparte la pantalla (cajero, videos, cortes, preguntas). Sin diseño: solo el cajero.
    diseno = models.ForeignKey(
        "DisenoKiosco", null=True, blank=True, on_delete=models.SET_NULL, related_name="pantallas",
        help_text="Cómo se reparte la pantalla. Vacío: solo el cajero, a pantalla completa.",
    )

    def __str__(self):
        return f"PantallaKiosco {self.nombre}"


class Modulo(models.TextChoices):
    CAJERO = "cajero", "Cajero (consulta y pago)"
    VIDEO = "video", "Videos (spots)"
    CORTES = "cortes", "Cortes de energía"
    PREGUNTAS = "preguntas", "Preguntas frecuentes"


class DisenoKiosco(models.Model):
    """Cómo se reparte la pantalla del kiosco: franjas de arriba a abajo, cada una con uno o dos
    cuadros (cajero, videos, cortes o preguntas frecuentes). Se elige en el admin para cada
    pantalla y la pantalla lo toma sola (pregunta cada minuto, lo aplica al volver al inicio)."""

    nombre = models.CharField(max_length=80, unique=True)
    modificado_en = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "diseño de pantalla"
        verbose_name_plural = "diseños de pantalla"

    def __str__(self):
        return self.nombre

    def para_pantalla(self) -> dict:
        franjas = []
        for f in self.franjas.all():
            celdas = [{"modulo": f.modulo, "ancho": f.ancho}]
            if f.modulo_2:
                celdas.append({"modulo": f.modulo_2, "ancho": 100 - f.ancho})
            franjas.append({"alto": f.alto, "celdas": celdas})
        return {"nombre": self.nombre, "franjas": franjas}


class FranjaDiseno(models.Model):
    """Una franja horizontal del diseño. `alto` es relativo a las demás franjas (30 y 70 = 30 % y
    70 %). Con un segundo cuadro, la franja se parte en dos columnas (`ancho` % para el primero)."""

    diseno = models.ForeignKey(DisenoKiosco, on_delete=models.CASCADE, related_name="franjas")
    orden = models.PositiveSmallIntegerField(default=0, help_text="De arriba (menor) a abajo (mayor).")
    alto = models.PositiveSmallIntegerField(default=50, help_text="Alto relativo a las otras franjas (ej. 25, 60, 15).")
    modulo = models.CharField("cuadro", max_length=20, choices=Modulo.choices)
    modulo_2 = models.CharField("segundo cuadro", max_length=20, choices=Modulo.choices, blank=True, default="",
                                help_text="Opcional: parte la franja en dos columnas.")
    ancho = models.PositiveSmallIntegerField(default=100, help_text="% de ancho del primer cuadro si hay segundo.")

    class Meta:
        ordering = ["orden", "id"]
        verbose_name = "franja"

    def __str__(self):
        return f"{self.get_modulo_display()}" + (f" | {self.get_modulo_2_display()}" if self.modulo_2 else "")


def _ruta_video(instancia, nombre):
    return f"kiosco/videos/{nombre}"


class VideoKiosco(models.Model):
    """Un spot para el cuadro de videos. Lo sirve el gateway (la pantalla no tiene internet) y la
    pantalla lo guarda en su caché para no bajarlo de nuevo."""

    titulo = models.CharField(max_length=120)
    archivo = models.FileField(upload_to=_ruta_video, help_text="MP4 (H.264). Mejor de menos de 100 MB.")
    orden = models.PositiveSmallIntegerField(default=0)
    activo = models.BooleanField(default=True)
    con_sonido = models.BooleanField(default=False, help_text="Si se desmarca, se reproduce en silencio.")
    subido_en = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["orden", "id"]
        verbose_name = "video del kiosco"
        verbose_name_plural = "videos del kiosco"

    def __str__(self):
        return self.titulo
