from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    CajaViewSet,
    CobroEfectivoViewSet,
    ConsultarDeudaView,
    FacturaPagadaPdfView,
    FacturasPagadasView,
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
    path("clientes/<str:codigo>/facturas-pagadas/", FacturasPagadasView.as_view(), name="facturas-pagadas"),
    path("clientes/<str:codigo>/facturas-pagadas/pdf/", FacturaPagadaPdfView.as_view(), name="factura-pagada-pdf"),
    path("dashboard/resumen/", DashboardResumenView.as_view(), name="dashboard-resumen"),
] + router.urls
