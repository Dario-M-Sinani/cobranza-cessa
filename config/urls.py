from django.contrib import admin
from django.urls import include, path
from rest_framework_simplejwt.views import TokenRefreshView

from apps.usuarios.views import TokenObtainPairConRolView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/token/", TokenObtainPairConRolView.as_view(), name="token_obtain_pair"),
    path("api/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/", include("apps.usuarios.urls")),
    path("api/", include("apps.cobranza.urls")),
    path("api/", include("apps.auditoria.urls")),
    path("api/externo/", include("apps.facturacion_externa.urls")),
]
