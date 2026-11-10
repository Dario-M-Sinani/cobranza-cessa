import hmac
import ipaddress

from django.conf import settings
from rest_framework import permissions
from rest_framework.throttling import SimpleRateThrottle

_LOOPBACK = ("127.0.0.1", "::1")


def ip_cliente(request) -> str:
    """IP real de quien llama. gunicorn solo escucha en 127.0.0.1, así que todo llega por nginx,
    que pone X-Real-IP; desde test01 (internet) esa es la IP pública y no pasa KIOSCO_REDES."""
    remota = request.META.get("REMOTE_ADDR", "")
    if remota in _LOOPBACK:
        return request.META.get("HTTP_X_REAL_IP", "").strip() or remota
    return remota


def ip_permitida(ip: str) -> bool:
    try:
        direccion = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(direccion in ipaddress.ip_network(red, strict=False) for red in settings.KIOSCO_REDES)


def kiosco_de_token(token: str) -> str | None:
    """Nombre de la pantalla dueña del token (KIOSCO_TOKENS = "nombre=token,..."), o None."""
    if not token:
        return None
    for nombre, esperado in settings.KIOSCO_TOKENS.items():
        if esperado and hmac.compare_digest(esperado.encode(), token.encode()):
            return nombre
    return None


class EsKiosco(permissions.BasePermission):
    """Solo pantallas de la red interna con su token (header X-Kiosco-Token). Las dos cosas: el
    token solo no basta si alguien lo copia fuera de la red, y la red sola no dice qué pantalla es."""

    message = "Pantalla no autorizada."

    def has_permission(self, request, view):
        if not ip_permitida(ip_cliente(request)):
            return False
        nombre = kiosco_de_token(request.headers.get("X-Kiosco-Token", ""))
        if not nombre:
            return False
        request.kiosco = nombre
        return True


class ConsultasPorKiosco(SimpleRateThrottle):
    """Tope de consultas de deuda por pantalla (KIOSCO_CONSULTAS_POR_MINUTO): la pantalla
    muestra el nombre con solo el N° de cliente, así que no se deja recorrer números en serie."""

    scope = "kiosco_consulta"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": getattr(request, "kiosco", ip_cliente(request))}
