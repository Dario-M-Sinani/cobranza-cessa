from rest_framework import permissions

from .models import Usuario


class EsCajeraActiva(permissions.BasePermission):
    """Generar un QR de cobro es la única acción reservada a este rol, y solo
    si la cajera está habilitada operativamente (`activo=True`)."""

    message = "Solo una cajera activa puede generar un QR de cobro."

    def has_permission(self, request, view):
        usuario = request.user
        return bool(
            usuario
            and usuario.is_authenticated
            and usuario.rol == Usuario.Rol.CAJERA
            and usuario.activo
        )


class EsSupervisorOAdmin(permissions.BasePermission):
    message = "Requiere rol de supervisor o administrador."

    def has_permission(self, request, view):
        usuario = request.user
        return bool(
            usuario
            and usuario.is_authenticated
            and usuario.rol in (Usuario.Rol.SUPERVISOR, Usuario.Rol.ADMIN)
        )


class EsAdmin(permissions.BasePermission):
    message = "Requiere rol de administrador."

    def has_permission(self, request, view):
        usuario = request.user
        return bool(usuario and usuario.is_authenticated and usuario.rol == Usuario.Rol.ADMIN)


class PuedeVerCajeros(permissions.BasePermission):
    """Supervisor y admin ven todas las entidades; el responsable de una entidad, solo la suya."""

    message = "Requiere rol de supervisor, administrador o responsable de entidad."

    def has_permission(self, request, view):
        usuario = request.user
        return bool(
            usuario
            and usuario.is_authenticated
            and usuario.rol in (Usuario.Rol.SUPERVISOR, Usuario.Rol.ADMIN, Usuario.Rol.ENTIDAD)
        )


def entidad_forzada(usuario) -> int | None:
    """La entidad a la que queda limitado el usuario (None = puede ver todas)."""
    if usuario.rol == Usuario.Rol.ENTIDAD:
        return int(usuario.entidad_financiera_id or 0)
    return None
