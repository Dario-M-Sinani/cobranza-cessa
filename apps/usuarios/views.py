from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView

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
