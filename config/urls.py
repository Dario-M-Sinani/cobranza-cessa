from django.contrib import admin
from django.urls import include, path
from rest_framework_simplejwt.views import TokenRefreshView

from apps.kiosco.views import pantalla as pantalla_kiosco
from apps.usuarios.views import TokenObtainPairConRolView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/token/", TokenObtainPairConRolView.as_view(), name="token_obtain_pair"),
    path("api/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/", include("apps.usuarios.urls")),
    path("api/", include("apps.cobranza.urls")),
    path("api/", include("apps.auditoria.urls")),
    path("api/", include("apps.facturacion_externa.urls_panel")),
    path("api/externo/", include("apps.facturacion_externa.urls")),
    path("api/kiosco/", include("apps.kiosco.urls")),
    path("kiosco/", pantalla_kiosco, name="kiosco-pantalla"),
]
