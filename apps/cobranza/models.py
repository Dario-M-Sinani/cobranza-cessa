from __future__ import annotations

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models, transaction
from django.utils import timezone

from apps.auditoria.models import LogAuditoria
from apps.clientes.models import Cliente

from .horario import dentro_de_horario_operativo


class Deuda(models.Model):
    cliente = models.ForeignKey(Cliente, on_delete=models.PROTECT, related_name="deudas")
    monto = models.DecimalField(max_digits=12, decimal_places=2)
    # Snapshot crudo de cada ítem de deuda tal como lo devolvió SIIC
    # (consulta-deuda) al momento de esta consulta -- mismo shape que
    # `ItemDeuda` de services/deuda_client.py. Sin esto no se puede facturar
    # de verdad contra api-cobranzas-bancos (exige codigo_sucursal,
    # nro_comprobante, etc. por ítem, no solo el total) -- mismo motivo por
    # el que cessa-laravel tiene `Recibo::debt_items`.
    items_snapshot = models.JSONField(default=list, blank=True, encoder=DjangoJSONEncoder)
    fecha_consulta = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-fecha_consulta"]

    def __str__(self):
        return f"Deuda {self.cliente} - {self.monto}"


class TransicionEstadoInvalida(Exception):
    """El estado actual de una TransaccionQR no permite la transición solicitada."""


class TransaccionQR(models.Model):
    class Estado(models.TextChoices):
        GENERADO = "generado", "Generado"
        PENDIENTE_CONFIRMACION = "pendiente_confirmacion", "Pendiente de confirmación"
        PAGADO = "pagado", "Pagado"
        VENCIDO = "vencido", "Vencido"
        ERROR = "error", "Error"
        CANCELADO = "cancelado", "Cancelado"

    # Mapa de transiciones válidas. Los estados que no aparecen como llave (o
    # que mapean a un set vacío) son terminales: nada puede salir de ellos.
    TRANSICIONES_PERMITIDAS: dict[str, set[str]] = {
        Estado.GENERADO: {Estado.PENDIENTE_CONFIRMACION, Estado.VENCIDO, Estado.ERROR, Estado.CANCELADO},
        Estado.PENDIENTE_CONFIRMACION: {Estado.PAGADO, Estado.VENCIDO, Estado.ERROR, Estado.CANCELADO},
        Estado.PAGADO: set(),
        Estado.VENCIDO: set(),
        Estado.ERROR: set(),
        Estado.CANCELADO: set(),
    }

    deuda = models.ForeignKey(Deuda, on_delete=models.PROTECT, related_name="transacciones_qr")
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="transacciones_qr"
    )
    # Caja abierta del cajero al momento de generar el QR, si tenía una --
    # mismo criterio que CobroEfectivo.caja: se vincula cuando existe, pero
    # no es obligatorio (no bloquea generar un QR sin caja abierta).
    caja = models.ForeignKey(
        "Caja", on_delete=models.PROTECT, null=True, blank=True, related_name="transacciones_qr"
    )
    id_operacion_mc4 = models.CharField(max_length=100, blank=True, default="")
    # Monto de la deuda al momento de generar el QR. Nunca se recalcula contra
    # la Deuda "en vivo": si la deuda cambia después, esta transacción sigue
    # representando lo que el cliente efectivamente pagó (o va a pagar).
    monto_snapshot = models.DecimalField(max_digits=12, decimal_places=2)
    estado = models.CharField(max_length=30, choices=Estado.choices, default=Estado.GENERADO)
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-creado_en"]

    def __str__(self):
        return f"TransaccionQR#{self.pk} [{self.estado}] {self.monto_snapshot}"

    def save(self, *args, **kwargs):
        # Bloquea cambios de `estado` que no pasen por transicionar_estado():
        # cualquier UPDATE directo de vista/admin queda rechazado acá.
        if self.pk is not None and not getattr(self, "_en_transicion", False):
            estado_previo = (
                TransaccionQR.objects.filter(pk=self.pk).values_list("estado", flat=True).first()
            )
            if estado_previo is not None and estado_previo != self.estado:
                raise TransicionEstadoInvalida(
                    "El estado de TransaccionQR no se puede modificar directamente "
                    "con save(); usar transicionar_estado()."
                )
        super().save(*args, **kwargs)

    @transaction.atomic
    def transicionar_estado(self, nuevo_estado: str, usuario=None) -> "TransaccionQR":
        """Único punto válido para cambiar `estado`. Valida la transición y
        registra el cambio en LogAuditoria en la misma transacción."""
        permitidos = self.TRANSICIONES_PERMITIDAS.get(self.estado, set())
        if nuevo_estado not in permitidos:
            raise TransicionEstadoInvalida(
                f"No se puede pasar de '{self.estado}' a '{nuevo_estado}'."
            )

        estado_anterior = self.estado
        self.estado = nuevo_estado
        self._en_transicion = True
        try:
            self.save(update_fields=["estado", "actualizado_en"])
        finally:
            self._en_transicion = False

        LogAuditoria.objects.create(
            usuario=usuario,
            accion=f"TransaccionQR: {estado_anterior} -> {nuevo_estado}",
            entidad_afectada=f"TransaccionQR:{self.pk}",
        )
        return self


class CobroEfectivo(models.Model):
    """Cobro en efectivo: a diferencia de TransaccionQR no depende de una
    pasarela externa, así que se confirma al instante (no hay estado
    intermedio "pendiente") -- ver apps/cobranza/services.py:
    registrar_cobro_efectivo()."""

    deuda = models.ForeignKey(Deuda, on_delete=models.PROTECT, related_name="cobros_efectivo")
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="cobros_efectivo"
    )
    # Caja abierta del cajero al momento del cobro, si tenía una (ver Épica
    # E) -- se vincula cuando existe, pero todavía no es obligatoria: no se
    # bloquea el cobro si el cajero no abrió caja.
    caja = models.ForeignKey(
        "Caja", on_delete=models.PROTECT, null=True, blank=True, related_name="cobros_efectivo"
    )
    monto_snapshot = models.DecimalField(max_digits=12, decimal_places=2)
    monto_recibido = models.DecimalField(max_digits=12, decimal_places=2)
    vuelto = models.DecimalField(max_digits=12, decimal_places=2)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-creado_en"]

    def __str__(self):
        return f"CobroEfectivo#{self.pk} {self.monto_snapshot}"


class Factura(models.Model):
    class EstadoEnvio(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente"
        ENVIADO = "enviado", "Enviado"
        ERROR = "error", "Error"

    # Origen 0..1 de exactamente un tipo (constraint abajo): puede haber
    # cobros ya confirmados (QR pagado, o efectivo) sin facturar todavía
    # (reintento pendiente contra el SIIC).
    transaccion_qr = models.OneToOneField(
        TransaccionQR, on_delete=models.PROTECT, null=True, blank=True, related_name="factura"
    )
    cobro_efectivo = models.OneToOneField(
        CobroEfectivo, on_delete=models.PROTECT, null=True, blank=True, related_name="factura"
    )
    numero_factura = models.CharField(max_length=100, blank=True, default="")
    estado_envio = models.CharField(
        max_length=20, choices=EstadoEnvio.choices, default=EstadoEnvio.PENDIENTE
    )
    emitida_en = models.DateTimeField(null=True, blank=True)
    # Envío real contra api-cobranzas-bancos (ver
    # apps.cobranza.services.enviar_factura_a_siic) -- mismo patrón que
    # SolicitudLiquidacion del gateway externo: uuid para poder reintentar
    # sin crear una Transacción nueva, intentos con tope para los
    # reintentos automáticos, error con el motivo para que un supervisor
    # pueda revisar sin adivinar.
    cobranzas_uuid = models.CharField(max_length=100, blank=True, default="")
    intentos = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True, default="")
    comprobante_pdf = models.BinaryField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(transaccion_qr__isnull=False, cobro_efectivo__isnull=True)
                    | models.Q(transaccion_qr__isnull=True, cobro_efectivo__isnull=False)
                ),
                name="factura_tiene_exactamente_un_origen",
            )
        ]

    @property
    def origen(self):
        return self.transaccion_qr or self.cobro_efectivo

    def __str__(self):
        return f"Factura {self.numero_factura or '(sin número)'} - {self.origen}"


class CajaOperacionInvalida(Exception):
    """La operación solicitada sobre una Caja no es válida en su estado, el
    rol del usuario, o el horario operativo actual."""


class Caja(models.Model):
    """Turno de un cajero: se abre al empezar a cobrar y se cierra al
    terminar. Todo cobro (QR o efectivo) debería quedar vinculado a la Caja
    abierta del cajero que lo realizó (pendiente de conectar cuando se
    implemente la Épica D/efectivo -- ver HISTORIAS_USUARIO.md).

    Reglas de negocio confirmadas por el usuario (2026-09-07):
    - Una caja cerrada no puede ser reabierta por el mismo cajero que la
      cerró -- solo un supervisor o administrador puede reabrirla.
    - No se puede abrir/reabrir una caja fuera del horario operativo salvo
      que lo haga un supervisor/administrador, indicando un motivo -- eso
      genera automáticamente un AperturaCajaFueraDeHorario (motivo, quién,
      hora y fecha)."""

    class Estado(models.TextChoices):
        ABIERTA = "abierta", "Abierta"
        CERRADA = "cerrada", "Cerrada"

    cajero = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="cajas"
    )
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.ABIERTA)
    creada_en = models.DateTimeField(auto_now_add=True)
    abierta_en = models.DateTimeField()
    abierta_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="cajas_abiertas"
    )
    cerrada_en = models.DateTimeField(null=True, blank=True)
    cerrada_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cajas_cerradas",
    )

    class Meta:
        ordering = ["-creada_en"]

    def __str__(self):
        return f"Caja#{self.pk} [{self.estado}] {self.cajero}"

    def save(self, *args, **kwargs):
        # Mismo mecanismo que TransaccionQR.save(): bloquea cambios de
        # `estado` que no pasen por abrir()/cerrar()/reabrir().
        if self.pk is not None and not getattr(self, "_en_transicion", False):
            estado_previo = Caja.objects.filter(pk=self.pk).values_list("estado", flat=True).first()
            if estado_previo is not None and estado_previo != self.estado:
                raise CajaOperacionInvalida(
                    "El estado de Caja no se puede modificar directamente con save(); "
                    "usar Caja.abrir()/cerrar()/reabrir()."
                )
        super().save(*args, **kwargs)

    @classmethod
    @transaction.atomic
    def abrir(cls, cajero, usuario, motivo: str = "") -> "Caja":
        """Abre una nueva Caja para `cajero`. Flujo normal: `usuario` es el
        propio cajero, dentro del horario operativo. Fuera de horario, solo
        un supervisor/administrador puede abrirla (nunca el cajero
        directamente), indicando `motivo` -- que queda auditado en
        AperturaCajaFueraDeHorario."""
        from apps.usuarios.models import Usuario

        if cls.objects.filter(cajero=cajero, estado=cls.Estado.ABIERTA).exists():
            raise CajaOperacionInvalida(f"{cajero} ya tiene una caja abierta.")

        fuera_de_horario = not dentro_de_horario_operativo()
        es_supervisor_o_admin = usuario.rol in (Usuario.Rol.SUPERVISOR, Usuario.Rol.ADMIN)
        if fuera_de_horario and not es_supervisor_o_admin:
            raise CajaOperacionInvalida(
                "Fuera del horario operativo, solo un supervisor o administrador puede abrir la caja."
            )
        if fuera_de_horario and not motivo.strip():
            raise CajaOperacionInvalida(
                "Abrir caja fuera del horario operativo requiere indicar un motivo."
            )

        caja = cls.objects.create(
            cajero=cajero, estado=cls.Estado.ABIERTA, abierta_en=timezone.now(), abierta_por=usuario
        )
        LogAuditoria.objects.create(
            usuario=usuario, accion="Caja: abierta", entidad_afectada=f"Caja:{caja.pk}"
        )
        if fuera_de_horario:
            AperturaCajaFueraDeHorario.objects.create(caja=caja, usuario=usuario, motivo=motivo.strip())
        return caja

    @transaction.atomic
    def cerrar(self, usuario) -> "Caja":
        if self.estado != Caja.Estado.ABIERTA:
            raise CajaOperacionInvalida("Solo se puede cerrar una caja abierta.")
        if usuario.pk != self.cajero_id:
            raise CajaOperacionInvalida("Solo el cajero dueño de la caja puede cerrarla.")

        self.estado = Caja.Estado.CERRADA
        self.cerrada_en = timezone.now()
        self.cerrada_por = usuario
        self._en_transicion = True
        try:
            self.save(update_fields=["estado", "cerrada_en", "cerrada_por"])
        finally:
            self._en_transicion = False

        LogAuditoria.objects.create(usuario=usuario, accion="Caja: cerrada", entidad_afectada=f"Caja:{self.pk}")
        return self

    @transaction.atomic
    def reabrir(self, usuario, motivo: str = "") -> "Caja":
        from apps.usuarios.models import Usuario

        if self.estado != Caja.Estado.CERRADA:
            raise CajaOperacionInvalida("Solo se puede reabrir una caja cerrada.")
        if usuario.rol not in (Usuario.Rol.SUPERVISOR, Usuario.Rol.ADMIN):
            raise CajaOperacionInvalida("Solo un supervisor o administrador puede reabrir una caja.")
        if self.cerrada_por_id == usuario.pk:
            raise CajaOperacionInvalida("El mismo usuario que cerró la caja no puede reabrirla.")

        fuera_de_horario = not dentro_de_horario_operativo()
        if fuera_de_horario and not motivo.strip():
            raise CajaOperacionInvalida(
                "Reabrir una caja fuera del horario operativo requiere indicar un motivo."
            )

        self.estado = Caja.Estado.ABIERTA
        self.abierta_en = timezone.now()
        self.abierta_por = usuario
        self.cerrada_en = None
        self.cerrada_por = None
        self._en_transicion = True
        try:
            self.save(update_fields=["estado", "abierta_en", "abierta_por", "cerrada_en", "cerrada_por"])
        finally:
            self._en_transicion = False

        LogAuditoria.objects.create(
            usuario=usuario, accion="Caja: reabierta", entidad_afectada=f"Caja:{self.pk}"
        )
        if fuera_de_horario:
            AperturaCajaFueraDeHorario.objects.create(caja=self, usuario=usuario, motivo=motivo.strip())
        return self


class AperturaCajaFueraDeHorario(models.Model):
    """Registro auditable de cada apertura/reapertura de Caja realizada
    fuera del horario operativo normal (ver Caja.abrir()/reabrir()) --
    responde a los requisitos E7/E8 de HISTORIAS_USUARIO.md: motivo, quién
    la autorizó, y hora/fecha (`creado_en`)."""

    caja = models.ForeignKey(Caja, on_delete=models.PROTECT, related_name="aperturas_fuera_de_horario")
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="aperturas_caja_fuera_de_horario",
    )
    motivo = models.TextField()
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-creado_en"]

    def __str__(self):
        return f"Apertura fuera de horario Caja#{self.caja_id} ({self.creado_en:%Y-%m-%d %H:%M})"
