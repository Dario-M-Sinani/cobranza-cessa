"""Login del panel contra la Nest de cobranzas (CAJAS_POR_CAJERA), como la app Java de cobranzas.

La cajera escribe su usuario y clave de la Nest (los mismos que el SIIC: el alta crea los dos). Acá:
1. `grant_type=password` en `/oauth/token` con el cliente OAuth del gateway (COBRANZAS_BANCO_CLIENT_*).
2. `/v1/usuarios/existe` para traer nombre, rol, entidad financiera y si está habilitado.
3. Se crea o actualiza el usuario local (sin clave local) y se guardan sus tokens cifrados.

La clave nunca se guarda: para seguir operando se usa el refresh token. Si la Nest la rechaza (clave
cambiada, usuario deshabilitado), la cajera vuelve a iniciar sesión.
"""
from __future__ import annotations

import base64
import hashlib
import logging

import requests
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib.auth.backends import BaseBackend
from rest_framework.exceptions import AuthenticationFailed

from .models import SesionNest, Usuario

logger = logging.getLogger(__name__)

ROLES = {
    "CAJA": Usuario.Rol.CAJERA,
    "COBRANZA": Usuario.Rol.SUPERVISOR,
    "SISTEMAS": Usuario.Rol.ADMIN,
}


class NestNoDisponible(Exception):
    """La Nest no respondió o respondió algo inesperado (no es un problema de la clave)."""


class SesionNestVencida(Exception):
    """La Nest ya no acepta los tokens del usuario: tiene que volver a iniciar sesión."""


def _fernet() -> Fernet:
    clave = settings.CREDENCIALES_NEST_CLAVE
    if not clave:
        clave = base64.urlsafe_b64encode(hashlib.sha256(f"sesion-nest:{settings.SECRET_KEY}".encode()).digest())
    return Fernet(clave)


def _cifrar(texto: str) -> str:
    return _fernet().encrypt(texto.encode()).decode() if texto else ""


def _descifrar(texto: str) -> str:
    if not texto:
        return ""
    try:
        return _fernet().decrypt(texto.encode()).decode()
    except InvalidToken:
        return ""  # cambió la clave de cifrado: se trata como sesión vencida.


def _base_url() -> str:
    return settings.COBRANZAS_BANCO_BASE_URL.rstrip("/")


def _verify():
    from services.cobranzas_banco_client import verificacion_tls

    return verificacion_tls()


def _pedir_token(cuerpo: dict) -> requests.Response:
    try:
        return requests.post(
            f"{_base_url()}/oauth/token",
            json={
                "client_id": settings.COBRANZAS_BANCO_CLIENT_ID,
                "client_secret": settings.COBRANZAS_BANCO_CLIENT_SECRET,
                "scope": "",
                **cuerpo,
            },
            timeout=15,
            verify=_verify(),
        )
    except requests.RequestException as exc:
        raise NestNoDisponible(str(exc)) from exc


def guardar_sesion(usuario: Usuario, datos_token: dict) -> None:
    SesionNest.objects.update_or_create(
        usuario=usuario,
        defaults={
            "access_cifrado": _cifrar(str(datos_token.get("access_token") or "")),
            "refresh_cifrado": _cifrar(str(datos_token.get("refresh_token") or "")),
        },
    )


def token_de(usuario: Usuario) -> str:
    sesion = SesionNest.objects.filter(usuario=usuario).first()
    token = _descifrar(sesion.access_cifrado) if sesion else ""
    if not token:
        raise SesionNestVencida(f"{usuario.username} no tiene sesión en la Nest de cobranzas: que vuelva a iniciar sesión.")
    return token


def refrescar(usuario: Usuario) -> str:
    """Pide un token nuevo con el refresh token. La Nest invalida el refresh usado (rota)."""
    sesion = SesionNest.objects.filter(usuario=usuario).first()
    refresh = _descifrar(sesion.refresh_cifrado) if sesion else ""
    if not refresh:
        raise SesionNestVencida(f"La sesión de {usuario.username} en la Nest venció: que vuelva a iniciar sesión.")
    respuesta = _pedir_token({"grant_type": "refresh_token", "refresh_token": refresh})
    if respuesta.status_code in (400, 401):
        sesion.delete()
        raise SesionNestVencida(f"La sesión de {usuario.username} en la Nest venció: que vuelva a iniciar sesión.")
    if not respuesta.ok:
        raise NestNoDisponible(f"refresh: HTTP {respuesta.status_code}")
    datos = respuesta.json() or {}
    guardar_sesion(usuario, datos)
    return str(datos.get("access_token") or "")


class NestBackend(BaseBackend):
    """Backend de Django para el login (lo usa el TokenObtainPair de simplejwt)."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not settings.CAJAS_POR_CAJERA or not username or not password:
            return None
        username = username.strip().upper()
        # Un usuario local (admin de emergencia, demos) se sigue validando con su clave local.
        if Usuario.objects.filter(username__iexact=username, origen=Usuario.Origen.LOCAL).exists():
            return None

        try:
            respuesta = _pedir_token({"grant_type": "password", "username": username, "password": password})
        except NestNoDisponible as exc:
            logger.warning("Login Nest de %s: sin respuesta (%s)", username, exc)
            raise AuthenticationFailed("No se pudo conectar con el sistema de cobranzas. Intenta de nuevo en un momento.")
        if respuesta.status_code in (400, 401):
            return None  # clave incorrecta, usuario inexistente o deshabilitado: mismo mensaje que siempre.
        if not respuesta.ok:
            logger.warning("Login Nest de %s: HTTP %s %s", username, respuesta.status_code, respuesta.text[:300])
            raise AuthenticationFailed("El sistema de cobranzas respondió con un error. Intenta de nuevo en un momento.")
        datos_token = respuesta.json() or {}

        try:
            r = requests.get(
                f"{_base_url()}/v1/usuarios/existe",
                params={"username": username},
                headers={"Authorization": f"Bearer {datos_token.get('access_token')}"},
                timeout=15,
                verify=_verify(),
            )
        except requests.RequestException as exc:
            raise AuthenticationFailed("No se pudo conectar con el sistema de cobranzas.") from exc
        if not r.ok:
            raise AuthenticationFailed("No se pudieron leer los datos del usuario en el sistema de cobranzas.")
        datos = r.json() or {}

        rol = ROLES.get(str(datos.get("rol") or ""))
        if rol is None:
            raise AuthenticationFailed(f"El rol {datos.get('rol')} todavía no tiene acceso a este panel.")

        usuario, _ = Usuario.objects.get_or_create(
            username=username, defaults={"origen": Usuario.Origen.NEST}
        )
        usuario.origen = Usuario.Origen.NEST
        usuario.first_name = str(datos.get("nombre") or "")[:150]
        usuario.last_name = ""
        usuario.rol = rol
        usuario.activo = bool(datos.get("habilitado", True))
        usuario.is_active = True
        usuario.entidad_financiera_id = datos.get("entidad_financiera_id") or None
        usuario.set_unusable_password()
        usuario.save()
        guardar_sesion(usuario, datos_token)
        return usuario

    def get_user(self, user_id):
        return Usuario.objects.filter(pk=user_id).first()
