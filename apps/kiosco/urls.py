from django.urls import path

from .views import (
    AvisosView,
    CancelarQrView,
    ClienteView,
    ComprobanteView,
    EnviarCorreoView,
    EnviarWhatsappView,
    EstadoQrView,
    FacturaPdfView,
    FotoView,
    GaleriaView,
    GenerarQrView,
    ReciboView,
)

# Bajo /api/kiosco/ y NO bajo /api/externo/: test01 publica /prod/api/externo/ a internet.
urlpatterns = [
    path("avisos/", AvisosView.as_view(), name="kiosco-avisos"),
    path("galeria/", GaleriaView.as_view(), name="kiosco-galeria"),
    path("galeria/<int:id_foto>/", FotoView.as_view(), name="kiosco-foto"),
    path("clientes/<str:nro_cliente>/", ClienteView.as_view(), name="kiosco-cliente"),
    path("qr/", GenerarQrView.as_view(), name="kiosco-generar-qr"),
    path("qr/<str:alias>/", EstadoQrView.as_view(), name="kiosco-estado-qr"),
    path("qr/<str:alias>/cancelar/", CancelarQrView.as_view(), name="kiosco-cancelar-qr"),
    path("qr/<str:alias>/recibo/", ReciboView.as_view(), name="kiosco-recibo"),
    path("qr/<str:alias>/comprobante/", ComprobanteView.as_view(), name="kiosco-comprobante"),
    path("qr/<str:alias>/factura.pdf", FacturaPdfView.as_view(), name="kiosco-factura-pdf"),
    path("qr/<str:alias>/enviar-correo/", EnviarCorreoView.as_view(), name="kiosco-enviar-correo"),
    path("qr/<str:alias>/enviar-whatsapp/", EnviarWhatsappView.as_view(), name="kiosco-enviar-whatsapp"),
]
