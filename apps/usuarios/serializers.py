from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import Usuario
from .utils import invalidar_tokens_de


class TokenObtainPairConRolSerializer(TokenObtainPairSerializer):
    """Agrega `rol`/`activo`/`username` al access token: el frontend los lee
    decodificando el JWT, sin pegarle a otro endpoint solo para saber qué rol
    tiene el usuario logueado."""

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["rol"] = user.rol
        token["activo"] = user.activo
        token["username"] = user.username
        return token


class UsuarioSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, min_length=8)

    class Meta:
        model = Usuario
        fields = [
            "id", "username", "first_name", "last_name", "email",
            "rol", "activo", "is_active", "password",
        ]
        read_only_fields = ["id"]

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        usuario = Usuario(**validated_data)
        if password:
            usuario.set_password(password)
        else:
            usuario.set_unusable_password()
        usuario.save()
        return usuario

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for atributo, valor in validated_data.items():
            setattr(instance, atributo, valor)
        if password:
            instance.set_password(password)
        instance.save()
        if password:
            # Reseteo forzado por admin (A4): la sesión vieja del usuario no
            # debe poder seguir renovando su access token con la contraseña
            # que ya no vale.
            invalidar_tokens_de(instance)
        return instance


class ResetearPasswordCajeroSerializer(serializers.Serializer):
    """Reseteo de contraseña de un cajero por su supervisor (o admin) --
    ver §6.1 de REQUISITOS_COBRANZA.md: el supervisor gestiona la
    contraseña de sus cajeros."""

    username = serializers.CharField()
    password = serializers.CharField(write_only=True, min_length=8)
