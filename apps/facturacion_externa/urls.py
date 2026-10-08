from django.urls import path

from .views import (
    ComprobanteJsonLiquidacionView,
    ComprobanteLiquidacionView,
    ComprobantePdfExternaView,
    ConsultaClienteExternaView,
    ConsultarLiquidacionView,
    LiquidarReciboExternoView,
    PagosClienteExternaView,
)

urlpatterns = [
    path("consulta/cliente/", ConsultaClienteExternaView.as_view(), name="consulta-cliente-externa"),
    path("consulta/clientes/<int:nro_cliente>/pagos/", PagosClienteExternaView.as_view(), name="pagos-cliente-externa"),
    path("consulta/comprobantes/pdf/", ComprobantePdfExternaView.as_view(), name="comprobante-pdf-externa"),
    path("recibos-web/liquidar/", LiquidarReciboExternoView.as_view(), name="liquidar-recibo-externo"),
    path("recibos-web/<str:alias>/", ConsultarLiquidacionView.as_view(), name="consultar-liquidacion"),
    path("recibos-web/<str:alias>/comprobante/", ComprobanteLiquidacionView.as_view(), name="comprobante-liquidacion"),
    path(
        "recibos-web/<str:alias>/comprobante-json/",
        ComprobanteJsonLiquidacionView.as_view(),
        name="comprobante-json-liquidacion",
    ),
]
