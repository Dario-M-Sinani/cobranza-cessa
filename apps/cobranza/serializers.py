from rest_framework import serializers

from apps.clientes.serializers import ClienteSerializer

from .models import AperturaCajaFueraDeHorario, Caja, CobroEfectivo, Deuda, Factura, TransaccionQR


class DeudaSerializer(serializers.ModelSerializer):
    cliente = ClienteSerializer(read_only=True)

    class Meta:
        model = Deuda
        fields = ["id", "cliente", "monto", "fecha_consulta"]
        read_only_fields = fields


class ConsultarDeudaSerializer(serializers.Serializer):
    codigo_externo = serializers.CharField(max_length=50)


class FacturaSerializer(serializers.ModelSerializer):
    comprobante_disponible = serializers.SerializerMethodField()

    class Meta:
        model = Factura
        fields = [
            "id", "transaccion_qr", "cobro_efectivo", "numero_factura", "estado_envio", "emitida_en",
            "error", "intentos", "comprobante_disponible",
        ]
        read_only_fields = fields

    def get_comprobante_disponible(self, obj):
        return obj.comprobante_pdf is not None


class TransaccionQRSerializer(serializers.ModelSerializer):
    deuda = DeudaSerializer(read_only=True)
    usuario = serializers.StringRelatedField(read_only=True)
    caja = serializers.PrimaryKeyRelatedField(read_only=True)
    # `factura` puede no existir todavía (relación 0..1): DRF resuelve la
    # ausencia (Factura.DoesNotExist) como null automáticamente.
    factura = FacturaSerializer(read_only=True)
    # No es un campo del modelo: viene poblado solo en la respuesta de
    # generar_transaccion_qr() (atributo transitorio, MC4 no permite volver
    # a pedir la imagen después). En list/retrieve normales da null.
    imagen_qr_base64 = serializers.SerializerMethodField()

    def get_imagen_qr_base64(self, obj):
        return getattr(obj, "imagen_qr_base64", None)

    class Meta:
        model = TransaccionQR
        fields = [
            "id", "deuda", "usuario", "caja", "id_operacion_mc4", "monto_snapshot",
            "estado", "creado_en", "actualizado_en", "factura", "imagen_qr_base64",
        ]
        read_only_fields = [campo for campo in fields if campo != "imagen_qr_base64"]


class GenerarTransaccionQRSerializer(serializers.Serializer):
    deuda_id = serializers.PrimaryKeyRelatedField(queryset=Deuda.objects.all(), source="deuda")
    # Adelanto opcional: si no se manda, se cobra la deuda completa.
    monto = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)


class CobroEfectivoSerializer(serializers.ModelSerializer):
    deuda = DeudaSerializer(read_only=True)
    usuario = serializers.StringRelatedField(read_only=True)
    caja = serializers.PrimaryKeyRelatedField(read_only=True)
    factura = FacturaSerializer(read_only=True)

    class Meta:
        model = CobroEfectivo
        fields = [
            "id", "deuda", "usuario", "caja", "monto_snapshot", "monto_recibido",
            "vuelto", "creado_en", "factura",
        ]
        read_only_fields = fields


class RegistrarCobroEfectivoSerializer(serializers.Serializer):
    deuda_id = serializers.PrimaryKeyRelatedField(queryset=Deuda.objects.all(), source="deuda")
    monto_recibido = serializers.DecimalField(max_digits=12, decimal_places=2)
    # Adelanto opcional: si no se manda, se cobra la deuda completa.
    monto_a_cobrar = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)


class CajaSerializer(serializers.ModelSerializer):
    cajero = serializers.StringRelatedField(read_only=True)
    abierta_por = serializers.StringRelatedField(read_only=True)
    cerrada_por = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = Caja
        fields = [
            "id", "cajero", "estado", "creada_en", "abierta_en", "abierta_por",
            "cerrada_en", "cerrada_por",
        ]
        read_only_fields = fields


class AbrirCajaSerializer(serializers.Serializer):
    # `cajero_id` es requerido solo cuando abre un supervisor/admin en
    # nombre de un cajero (p. ej. fuera de horario); una cajera abriendo su
    # propia caja no lo manda -- resuelto y validado por rol en
    # CajaViewSet.create(), no acá, para no acoplar este serializer a qué
    # rol está autenticado.
    cajero_id = serializers.IntegerField(required=False)
    motivo = serializers.CharField(required=False, allow_blank=True, default="")


class ReabrirCajaSerializer(serializers.Serializer):
    motivo = serializers.CharField(required=False, allow_blank=True, default="")


class AperturaCajaFueraDeHorarioSerializer(serializers.ModelSerializer):
    caja = serializers.PrimaryKeyRelatedField(read_only=True)
    usuario = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = AperturaCajaFueraDeHorario
        fields = ["id", "caja", "usuario", "motivo", "creado_en"]
        read_only_fields = fields
