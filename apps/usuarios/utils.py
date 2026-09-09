"""Utilidades compartidas de autenticación."""
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken


def invalidar_tokens_de(usuario) -> None:
    """Blacklistea todos los refresh tokens emitidos para `usuario` -- se usa
    después de un cambio de contraseña (propio o forzado por admin), para
    que una sesión abierta con la contraseña vieja no pueda seguir
    renovando su access token."""
    for token in OutstandingToken.objects.filter(user=usuario):
        BlacklistedToken.objects.get_or_create(token=token)
