import hmac

from django.conf import settings
from rest_framework import permissions


class TieneApiKeyServicioExterno(permissions.BasePermission):
    """Auth servidor-a-servidor para cessa-laravel -- deliberadamente no es
    el JWT de cajera/supervisor (esto no lo llama una persona autenticada,
    lo llama otro backend). Header `X-Api-Key` comparado con
    `hmac.compare_digest` contra `settings.API_KEY_CESSA_LARAVEL`."""

    message = "API key inválida o no configurada."

    def has_permission(self, request, view):
        api_key_esperada = settings.API_KEY_CESSA_LARAVEL
        api_key_recibida = request.headers.get("X-Api-Key", "")
        return bool(api_key_esperada) and hmac.compare_digest(api_key_esperada, api_key_recibida)
