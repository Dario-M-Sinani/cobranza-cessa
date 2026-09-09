from rest_framework import serializers

from .models import Cliente


class ClienteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Cliente
        fields = ["id", "codigo_externo", "nombre", "nit_ci"]
        read_only_fields = fields
