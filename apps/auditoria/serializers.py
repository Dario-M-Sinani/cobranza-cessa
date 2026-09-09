from rest_framework import serializers

from .models import LogAuditoria


class LogAuditoriaSerializer(serializers.ModelSerializer):
    usuario = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = LogAuditoria
        fields = ["id", "usuario", "accion", "entidad_afectada", "creado_en"]
        read_only_fields = fields
