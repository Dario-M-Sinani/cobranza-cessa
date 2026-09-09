from django.contrib import admin

from .models import AperturaCajaFueraDeHorario, Caja, CobroEfectivo, Deuda, Factura, TransaccionQR


@admin.register(Deuda)
class DeudaAdmin(admin.ModelAdmin):
    list_display = ("cliente", "monto", "fecha_consulta")
    list_filter = ("fecha_consulta",)
    search_fields = ("cliente__nombre", "cliente__codigo_externo")


@admin.register(TransaccionQR)
class TransaccionQRAdmin(admin.ModelAdmin):
    list_display = ("id", "deuda", "usuario", "monto_snapshot", "estado", "creado_en")
    list_filter = ("estado", "creado_en")
    search_fields = ("id_operacion_mc4", "deuda__cliente__nombre", "deuda__cliente__codigo_externo")
    # El estado solo cambia vía TransaccionQR.transicionar_estado(), nunca a
    # mano desde el admin -- se deja de solo lectura para reforzarlo.
    readonly_fields = ("deuda", "usuario", "monto_snapshot", "estado", "creado_en", "actualizado_en")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CobroEfectivo)
class CobroEfectivoAdmin(admin.ModelAdmin):
    list_display = ("id", "deuda", "usuario", "caja", "monto_snapshot", "monto_recibido", "vuelto", "creado_en")
    list_filter = ("creado_en",)
    search_fields = ("deuda__cliente__nombre", "deuda__cliente__codigo_externo", "usuario__username")
    # No tiene máquina de estados como TransaccionQR/Caja, pero igual se
    # crea únicamente vía registrar_cobro_efectivo() -- de solo lectura acá.
    readonly_fields = ("deuda", "usuario", "caja", "monto_snapshot", "monto_recibido", "vuelto", "creado_en")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Factura)
class FacturaAdmin(admin.ModelAdmin):
    list_display = (
        "numero_factura", "transaccion_qr", "cobro_efectivo", "estado_envio", "intentos", "emitida_en",
    )
    list_filter = ("estado_envio",)
    search_fields = ("numero_factura", "cobranzas_uuid")
    # El envío real es responsabilidad de apps.cobranza.services.enviar_factura_a_siic()
    # (tarea periódica o "Reintentar Facturación") -- de solo lectura acá, mismo
    # criterio que SolicitudLiquidacionAdmin del gateway externo.
    readonly_fields = (
        "transaccion_qr", "cobro_efectivo", "numero_factura", "estado_envio", "emitida_en",
        "cobranzas_uuid", "intentos", "error",
    )
    exclude = ("comprobante_pdf",)

    def has_add_permission(self, request):
        return False


@admin.register(Caja)
class CajaAdmin(admin.ModelAdmin):
    list_display = ("id", "cajero", "estado", "abierta_en", "abierta_por", "cerrada_en", "cerrada_por")
    list_filter = ("estado", "abierta_en")
    search_fields = ("cajero__username", "cajero__first_name", "cajero__last_name")
    # El estado solo cambia vía Caja.abrir()/cerrar()/reabrir(), nunca a
    # mano desde el admin -- se deja de solo lectura para reforzarlo.
    readonly_fields = (
        "cajero", "estado", "creada_en", "abierta_en", "abierta_por", "cerrada_en", "cerrada_por",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AperturaCajaFueraDeHorario)
class AperturaCajaFueraDeHorarioAdmin(admin.ModelAdmin):
    list_display = ("caja", "usuario", "creado_en")
    search_fields = ("caja__cajero__username", "usuario__username", "motivo")
    readonly_fields = ("caja", "usuario", "motivo", "creado_en")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
