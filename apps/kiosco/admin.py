from django.contrib import admin

from .models import EnvioFactura, PagoKiosco


class EnvioFacturaInline(admin.TabularInline):
    model = EnvioFactura
    extra = 0
    readonly_fields = ("canal", "destino", "registrado", "exitoso", "error", "enviado_en")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(PagoKiosco)
class PagoKioscoAdmin(admin.ModelAdmin):
    list_display = ("alias", "kiosco", "nro_cliente", "monto", "estado", "creado_en", "pagado_en", "nro_factura")
    list_filter = ("estado", "kiosco", "creado_en")
    search_fields = ("alias", "nro_cliente")
    # Lo crea y lo mueve solo apps.kiosco.services: de solo lectura, como SolicitudLiquidacion.
    readonly_fields = [f.name for f in PagoKiosco._meta.fields]
    inlines = [EnvioFacturaInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
