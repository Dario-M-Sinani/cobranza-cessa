from django.contrib import admin
from django.urls import include, path
from rest_framework_simplejwt.views import TokenRefreshView

from apps.kiosco import editor as editor_kiosco
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
    path("kiosco/editor/", editor_kiosco.pagina, name="kiosco-editor"),
    path("kiosco/editor/entrar/", editor_kiosco.Entrar.as_view(), name="kiosco-editor-entrar"),
    path("kiosco/editor/salir/", editor_kiosco.Salir.as_view(), name="kiosco-editor-salir"),
    path("kiosco/editor/previa/<int:pk>/", editor_kiosco.ventana_previa, name="kiosco-editor-previa"),
]
