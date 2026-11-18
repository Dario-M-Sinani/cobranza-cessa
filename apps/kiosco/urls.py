from django.urls import path

from . import editor

from .views import (
    AvisosView,
    CancelarQrView,
    ClienteView,
    ComprobanteView,
    ContenidoView,
    EnviarCorreoView,
    EnviarWhatsappView,
    EstadoQrView,
    EstadoView,
    FacturaPdfView,
    FotoView,
    GaleriaView,
    GenerarQrView,
    ReciboView,
    TecnicoView,
    VideoView,
)

# Bajo /api/kiosco/ y NO bajo /api/externo/: test01 publica /prod/api/externo/ a internet.
urlpatterns = [
    path("estado/", EstadoView.as_view(), name="kiosco-estado"),
    path("tecnico/", TecnicoView.as_view(), name="kiosco-tecnico"),
    path("avisos/", AvisosView.as_view(), name="kiosco-avisos"),
    path("contenido/", ContenidoView.as_view(), name="kiosco-contenido"),
    # Editor de la pantalla (/kiosco/editor/): sesión de Django, red interna.
    path("editor/", editor.DatosView.as_view(), name="kiosco-editor-datos"),
    path("editor/disenos/", editor.DisenosView.as_view(), name="kiosco-editor-disenos"),
    path("editor/disenos/<int:pk>/", editor.DisenoView.as_view(), name="kiosco-editor-diseno"),
    path("editor/pantallas/<str:nombre>/", editor.PantallaView.as_view(), name="kiosco-editor-pantalla"),
    path("editor/videos/", editor.VideosView.as_view(), name="kiosco-editor-videos"),
    path("editor/videos/<int:pk>/", editor.VideoView.as_view(), name="kiosco-editor-video"),
    path("videos/<int:id_video>/", VideoView.as_view(), name="kiosco-video"),
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
