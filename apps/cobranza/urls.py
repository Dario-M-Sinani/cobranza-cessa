from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    CajaViewSet,
    CobroEfectivoViewSet,
    ConsultarDeudaView,
    DashboardResumenView,
    FacturaViewSet,
    TransaccionQRViewSet,
)

router = DefaultRouter()
router.register("transacciones-qr", TransaccionQRViewSet, basename="transaccion-qr")
router.register("cobros-efectivo", CobroEfectivoViewSet, basename="cobro-efectivo")
router.register("facturas", FacturaViewSet, basename="factura")
router.register("cajas", CajaViewSet, basename="caja")

urlpatterns = [
    path("deudas/consultar/", ConsultarDeudaView.as_view(), name="consultar-deuda"),
    path("dashboard/resumen/", DashboardResumenView.as_view(), name="dashboard-resumen"),
] + router.urls
