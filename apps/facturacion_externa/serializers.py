from rest_framework import serializers

from .models import SolicitudLiquidacion


class ItemDeudaEntradaSerializer(serializers.Serializer):
    """Snapshot crudo de un ítem de deuda tal como lo guarda cessa-laravel en
    `Recibo::debt_items` -- se valida solo lo mínimo indispensable para
    facturar (ver services.cobranzas_banco_client.construir_detalle); el
    resto de campos que exige api-cobranzas-bancos viajan igual pero sin
    validación estricta de forma, porque cessa-laravel ya los valida/arma
    contra SIIC antes de mandarlos."""

    nro_comprobante = serializers.CharField(allow_blank=True, required=False)
    importe = serializers.DecimalField(max_digits=14, decimal_places=2)
    debito_credito = serializers.CharField()

    def to_internal_value(self, data):
        # Valida los campos declarados arriba pero conserva todos los que
        # vengan en el payload crudo (codigo_sucursal, fecha, tipo, etc.) --
        # cessa-laravel ya los validó/armó contra SIIC, este backend no debe
        # exigir que además calcen con un shape estricto acá.
        validado = super().to_internal_value(data)
        crudo = dict(data)
        crudo.update(validado)
        return crudo


class SolicitudLiquidacionEntradaSerializer(serializers.Serializer):
    alias = serializers.CharField(max_length=100)
    nro_cliente = serializers.CharField(max_length=20)
    monto = serializers.DecimalField(max_digits=12, decimal_places=2)
    moneda = serializers.CharField(max_length=3, default="BOB")
    detalle = ItemDeudaEntradaSerializer(many=True)
    fecha_pago = serializers.DateTimeField()
    numero_orden_originante = serializers.CharField(max_length=50, allow_blank=True, required=False, default="")


class SolicitudLiquidacionSalidaSerializer(serializers.ModelSerializer):
    comprobante_disponible = serializers.SerializerMethodField()

    class Meta:
        model = SolicitudLiquidacion
        fields = [
            "alias", "estado", "cobranzas_uuid", "error", "intentos",
            "recibido_en", "procesado_en", "comprobante_disponible",
        ]
        read_only_fields = fields

    def get_comprobante_disponible(self, obj):
        return obj.comprobante_pdf is not None
