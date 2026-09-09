from django.contrib import admin

from .models import SolicitudLiquidacion


@admin.register(SolicitudLiquidacion)
class SolicitudLiquidacionAdmin(admin.ModelAdmin):
    list_display = ("alias", "nro_cliente", "monto", "estado", "intentos", "recibido_en", "procesado_en")
    list_filter = ("estado", "origen", "recibido_en")
    search_fields = ("alias", "nro_cliente", "cobranzas_uuid")
    # Llega y se procesa únicamente vía LiquidarReciboExternoView ->
    # liquidar_solicitud() -- de solo lectura acá, igual que Factura/Caja.
    readonly_fields = (
        "alias", "origen", "nro_cliente", "monto", "moneda", "detalle", "fecha_pago",
        "numero_orden_originante", "estado", "cobranzas_uuid", "intentos", "error",
        "recibido_en", "procesado_en",
    )
    exclude = ("comprobante_pdf",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
