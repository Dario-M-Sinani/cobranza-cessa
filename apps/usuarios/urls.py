from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    CajerosView,
    CajeroView,
    CambiarContrasenaView,
    TransaccionesCajeroView,
    TransaccionNestView,
    TransaccionesEntidadView,
    FacturasTransaccionView,
    UsuarioViewSet,
)

router = DefaultRouter()
router.register("usuarios", UsuarioViewSet, basename="usuario")

urlpatterns = router.urls + [
    path("cambiar-contrasena/", CambiarContrasenaView.as_view(), name="cambiar-contrasena"),
    path("cajeros/", CajerosView.as_view(), name="cajeros"),
    path("cajeros/<str:clave>/", CajeroView.as_view(), name="cajero"),
    path("cajeros/<str:clave>/transacciones/", TransaccionesCajeroView.as_view(), name="cajero-transacciones"),
    path("transacciones-siic/", TransaccionesEntidadView.as_view(), name="transacciones-siic"),
    path("transacciones-siic/<str:uuid>/", TransaccionNestView.as_view(), name="transaccion-siic"),
    path("transacciones-siic/<str:uuid>/facturas/", FacturasTransaccionView.as_view(), name="transaccion-siic-facturas"),
    path("transacciones-siic/<str:uuid>/anular/", TransaccionNestView.as_view(), name="transaccion-siic-anular"),
]
