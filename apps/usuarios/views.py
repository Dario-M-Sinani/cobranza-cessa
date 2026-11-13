from datetime import date

from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from . import cajeros
from .models import Usuario
from .permissions import EsAdmin, EsSupervisorOAdmin
from .serializers import ResetearPasswordCajeroSerializer, TokenObtainPairConRolSerializer, UsuarioSerializer
from .utils import invalidar_tokens_de


class UsuarioViewSet(viewsets.ModelViewSet):
    """Gestión de usuarios -- exclusiva de admin, salvo
    `resetear_password_cajero` (abierta también a supervisor, ver abajo)."""

    queryset = Usuario.objects.all().order_by("username")
    serializer_class = UsuarioSerializer
    permission_classes = [EsAdmin]

    def get_permissions(self):
        if self.action == "resetear_password_cajero":
            return [IsAuthenticated(), EsSupervisorOAdmin()]
        return super().get_permissions()

    @action(detail=False, methods=["post"], url_path="resetear-password-cajero")
    def resetear_password_cajero(self, request):
        """Un supervisor (o admin) resetea la contraseña de un cajero por
        `username` -- nunca la de otro supervisor/admin, aunque quien llame
        sea admin (para eso admin ya tiene el reset genérico vía
        PATCH /usuarios/{id}/). Invalida los refresh tokens del cajero."""
        entrada = ResetearPasswordCajeroSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)

        try:
            cajero = Usuario.objects.get(
                username=entrada.validated_data["username"], rol=Usuario.Rol.CAJERA
            )
        except Usuario.DoesNotExist:
            return Response({"detail": "No existe un cajero con ese usuario."}, status=status.HTTP_404_NOT_FOUND)

        cajero.set_password(entrada.validated_data["password"])
        cajero.save(update_fields=["password"])
        invalidar_tokens_de(cajero)

        return Response(status=status.HTTP_204_NO_CONTENT)


class TokenObtainPairConRolView(TokenObtainPairView):
    serializer_class = TokenObtainPairConRolSerializer


class CajerosView(APIView):
    """`GET /api/cajeros/?entidad_financiera_id=` (supervisor/admin): usuarios de la Nest de todas las
    entidades. `POST /api/cajeros/` (admin): alta doble Nest + SIIC. Ver apps/usuarios/cajeros.py."""

    def get_permissions(self):
        return [IsAuthenticated(), EsAdmin() if self.request.method == "POST" else EsSupervisorOAdmin()]

    def get(self, request):
        try:
            entidad = int(request.query_params.get("entidad_financiera_id") or 0) or None
            return Response({"cajeros": cajeros.listar(request.user, entidad), "entidades": cajeros.entidades(request.user)})
        except (TypeError, ValueError):
            return Response({"detail": "Entidad inválida."}, status=status.HTTP_400_BAD_REQUEST)
        except cajeros.CajerosError as exc:
            return Response({"detail": str(exc)}, status=exc.status)

    def post(self, request):
        try:
            return Response(cajeros.crear(request.user, request.data), status=status.HTTP_201_CREATED)
        except cajeros.CajerosError as exc:
            return Response({"detail": str(exc)}, status=exc.status)


class CajeroView(APIView):
    """`GET /api/cajeros/<id>/` (supervisor/admin): ficha con horario y días.
    `PATCH /api/cajeros/<username>/` (admin): cambios en la Nest y en el SIIC."""

    def get_permissions(self):
        return [IsAuthenticated(), EsAdmin() if self.request.method == "PATCH" else EsSupervisorOAdmin()]

    def get(self, request, clave):
        if not clave.isdigit():
            return Response({"detail": "Cajero inválido."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response(cajeros.ficha(request.user, int(clave)))
        except cajeros.CajerosError as exc:
            return Response({"detail": str(exc)}, status=exc.status)

    def patch(self, request, clave):
        try:
            return Response(cajeros.actualizar(request.user, clave.upper(), request.data))
        except cajeros.CajerosError as exc:
            return Response({"detail": str(exc)}, status=exc.status)


class CambiarContrasenaView(APIView):
    """`POST /api/cambiar-contrasena/ {"actual", "nueva"}`: cualquier usuario cambia la suya. Si vino de
    la Nest se cambia allá (es la misma clave del SIIC con la que entra); si es local, acá."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        actual = str(request.data.get("actual") or "")
        nueva = str(request.data.get("nueva") or "")
        usuario = request.user
        if len(nueva) < (6 if usuario.origen == Usuario.Origen.NEST else 8):
            minimo = 6 if usuario.origen == Usuario.Origen.NEST else 8
            return Response({"detail": f"La contraseña nueva tiene que tener al menos {minimo} caracteres."}, status=400)
        if nueva == actual:
            return Response({"detail": "La contraseña nueva es igual a la actual."}, status=400)
        if usuario.origen == Usuario.Origen.NEST:
            try:
                cajeros.cambiar_contrasena(usuario, actual, nueva)
            except cajeros.CajerosError as exc:
                return Response({"detail": str(exc)}, status=exc.status)
        else:
            if not usuario.check_password(actual):
                return Response({"detail": "La contraseña actual no es correcta."}, status=400)
            usuario.set_password(nueva)
            usuario.save(update_fields=["password"])
            invalidar_tokens_de(usuario)
        return Response({"detail": "Contraseña cambiada."})


class TransaccionesCajeroView(APIView):
    """`GET /api/cajeros/<id>/transacciones/?desde=AAAA-MM-DD&hasta=AAAA-MM-DD` (supervisor/admin)."""

    permission_classes = [IsAuthenticated, EsSupervisorOAdmin]

    def get(self, request, clave):
        try:
            hoy = timezone.localdate()
            desde = date.fromisoformat(request.query_params.get("desde") or hoy.isoformat())
            hasta = date.fromisoformat(request.query_params.get("hasta") or desde.isoformat())
        except ValueError:
            return Response({"detail": "Fechas inválidas (AAAA-MM-DD)."}, status=400)
        if not clave.isdigit():
            return Response({"detail": "Cajero inválido."}, status=400)
        try:
            return Response({"transacciones": cajeros.transacciones(request.user, int(clave), desde, hasta)})
        except cajeros.CajerosError as exc:
            return Response({"detail": str(exc)}, status=exc.status)


class TransaccionNestView(APIView):
    """`GET /api/transacciones-siic/<uuid>/`: detalle. `POST .../anular/ {"motivo"}`: anula (solo hoy)."""

    permission_classes = [IsAuthenticated, EsSupervisorOAdmin]

    def get(self, request, uuid):
        try:
            return Response(cajeros.detalle_transaccion(request.user, uuid))
        except cajeros.CajerosError as exc:
            return Response({"detail": str(exc)}, status=exc.status)

    def post(self, request, uuid):
        try:
            return Response(cajeros.anular_transaccion(request.user, uuid, str(request.data.get("motivo") or "")))
        except cajeros.CajerosError as exc:
            return Response({"detail": str(exc)}, status=exc.status)
