"""PANEL_SOLO_CONSULTA (gateway de producción): el panel sirve para ver los pagos web y del bot
("Pagos web", Dashboard), no para cobrar. El usuario de banco del gateway prod (QRWEB) factura
de verdad en el SIIC, así que las rutas de cobro y caja del panel responden 403 aunque alguien
las llame a mano; el frontend además las oculta (claim `solo_consulta` del token)."""
from django.conf import settings
from django.http import JsonResponse

RUTAS_DE_COBRO = (
    "/api/transacciones-qr/",
    "/api/cobros-efectivo/",
    "/api/cobros-agrupados/",
    "/api/cajas/",
    "/api/deudas/consultar/",
    "/api/comprobantes/ultimo/",
)


class SoloConsultaMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.PANEL_SOLO_CONSULTA and request.path.startswith(RUTAS_DE_COBRO):
            return JsonResponse(
                {"detail": "Este panel es solo de consulta: el cobro en caja no está habilitado acá."}, status=403
            )
        return self.get_response(request)
