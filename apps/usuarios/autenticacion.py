"""JWT del panel con un límite para el responsable de una entidad financiera (rol `entidad`, el
ENTIDAD_FINANCIERA de la Nest): como en la app Java, solo consulta a los cajeros de su banco y sus
transacciones del SIIC (detalle, facturas y anular las del día). Nada de cobrar, caja, QR,
comprobantes, pagos web ni auditoría.

Se controla acá, en un solo lugar, y no vista por vista: muchas vistas solo piden estar autenticado
y distinguen "cajera" de "el resto"; un rol nuevo pasaría como supervisor sin darse cuenta.
"""
from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.authentication import JWTAuthentication

from .models import Usuario

RUTAS_DE_ENTIDAD = (
    "/api/cajeros/",
    "/api/transacciones-siic/",
    "/api/cambiar-contrasena/",
)


class JWTConLimiteDeEntidad(JWTAuthentication):
    def authenticate(self, request):
        resultado = super().authenticate(request)
        if resultado is not None:
            usuario, _ = resultado
            if usuario.rol == Usuario.Rol.ENTIDAD and not request.path.startswith(RUTAS_DE_ENTIDAD):
                raise PermissionDenied("Tu usuario es de consulta de tu entidad: solo ve cajeros y sus transacciones.")
        return resultado
