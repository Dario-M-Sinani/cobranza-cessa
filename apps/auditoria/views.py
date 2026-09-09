from rest_framework import mixins, viewsets

from apps.usuarios.permissions import EsAdmin

from .models import LogAuditoria
from .serializers import LogAuditoriaSerializer


class LogAuditoriaViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Solo lectura, exclusivo de administrador -- ver H4 en
    HISTORIAS_USUARIO.md. Antes de esto el log solo se podía revisar desde
    el Django admin."""

    queryset = LogAuditoria.objects.select_related("usuario").order_by("-creado_en")
    serializer_class = LogAuditoriaSerializer
    permission_classes = [EsAdmin]
