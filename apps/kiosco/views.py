import base64
from functools import lru_cache

from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import render
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.facturacion_externa.reporte import LOGO

from . import avisos, galeria, services
from .models import PagoKiosco
from .permissions import ConsultasPorKiosco, EsKiosco, ip_cliente, ip_permitida


@lru_cache(maxsize=1)
def _logo_base64() -> str:
    try:
        return base64.b64encode(LOGO.read_bytes()).decode()
    except OSError:
        return ""


def pantalla(request):
    """La página del kiosco (HTML + JS sin build). El token de la pantalla llega una sola vez en
    `?k=...`, la página lo guarda en el navegador y lo manda en cada llamada a /api/kiosco/."""
    if not ip_permitida(ip_cliente(request)):
        return HttpResponseForbidden("Solo red interna.")
    return render(request, "kiosco/pantalla.html", {"logo": _logo_base64()})


class _KioscoView(APIView):
    authentication_classes = []
    permission_classes = [EsKiosco]

    def _pago(self, request, alias):
        return PagoKiosco.objects.select_related("solicitud").filter(alias=alias, kiosco=request.kiosco).first()


class AvisosView(_KioscoView):
    """GET /api/kiosco/avisos/ -- cortes programados y de emergencia de cessa.com.bo para la
    barra superior (los lee el gateway; la pantalla no tiene internet)."""

    def get(self, request):
        return Response({"avisos": avisos.avisos()})


class GaleriaView(_KioscoView):
    """GET /api/kiosco/galeria/ -- las fotos del carrusel de la web (protector de pantalla)."""

    def get(self, request):
        return Response({"fotos": [{"id": f["id"]} for f in galeria.fotos()]})


class FotoView(_KioscoView):
    """GET /api/kiosco/galeria/<id>/ -- una foto del carrusel (la baja el gateway de la web)."""

    def get(self, request, id_foto):
        guardada = galeria.foto(id_foto)
        if guardada is None:
            return Response({"error": "No encontrada."}, status=status.HTTP_404_NOT_FOUND)
        contenido, tipo = guardada
        respuesta = HttpResponse(contenido, content_type=tipo)
        respuesta["Cache-Control"] = "private, max-age=3600"
        return respuesta


class ClienteView(_KioscoView):
    """GET /api/kiosco/clientes/<nro>/ -- nombre, deuda (del más antiguo al más reciente) y las
    cantidades de meses que se pueden pagar."""

    throttle_classes = [ConsultasPorKiosco]

    def get(self, request, nro_cliente):
        nro = services.normalizar_nro_cliente(nro_cliente)
        if not nro:
            return Response({"error": "El número de cliente tiene solo números (hasta 10)."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            cliente = services.consultar_cliente(nro)
        except services.KioscoError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if cliente is None:
            return Response({"error": "No encontramos ese número de cliente."}, status=status.HTTP_404_NOT_FOUND)
        return Response(services.resumen_para_pantalla(cliente))


class GenerarQrView(_KioscoView):
    """POST /api/kiosco/qr/ {nro_cliente, cantidad} -- QR de los `cantidad` meses más antiguos."""

    def post(self, request):
        datos = request.data if isinstance(request.data, dict) else {}
        nro = services.normalizar_nro_cliente(datos.get("nro_cliente"))
        try:
            cantidad = int(datos.get("cantidad"))
        except (TypeError, ValueError):
            cantidad = 0
        if not nro or cantidad < 1:
            return Response({"error": "Faltan el número de cliente o los meses a pagar."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            pago, imagen = services.generar_qr(request.kiosco, nro, cantidad)
        except services.KioscoError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({**services.estado_para_pantalla(pago), "qr": imagen}, status=status.HTTP_201_CREATED)


class EstadoQrView(_KioscoView):
    """GET /api/kiosco/qr/<alias>/ -- la pantalla pregunta cada pocos segundos."""

    def get(self, request, alias):
        pago = self._pago(request, alias)
        if pago is None:
            return Response({"error": "No encontrado."}, status=status.HTTP_404_NOT_FOUND)
        return Response(services.estado_para_pantalla(services.actualizar(pago)))


class ComprobanteView(_KioscoView):
    """GET /api/kiosco/qr/<alias>/comprobante/ -- datos del ticket que la pantalla manda a su
    impresora (térmica o carta, sin diálogo: Chrome con --kiosk-printing)."""

    def get(self, request, alias):
        pago = self._pago(request, alias)
        if pago is None:
            return Response({"error": "No encontrado."}, status=status.HTTP_404_NOT_FOUND)
        datos = services.comprobante_para_ticket(pago)
        if datos is None:
            return Response({"error": "Todavía no hay factura para este pago."}, status=status.HTTP_409_CONFLICT)
        return Response(datos)


class ReciboView(_KioscoView):
    """GET /api/kiosco/qr/<alias>/recibo/ -- el recibo de pago que imprime la pantalla (térmica o
    carta, sin diálogo). La factura no se imprime: el cliente la descarga en la app o la web."""

    def get(self, request, alias):
        pago = self._pago(request, alias)
        if pago is None:
            return Response({"error": "No encontrado."}, status=status.HTTP_404_NOT_FOUND)
        datos = services.recibo_de_pago(pago)
        if datos is None:
            return Response({"error": "El pago todavía no está confirmado."}, status=status.HTTP_409_CONFLICT)
        return Response(datos)


class FacturaPdfView(_KioscoView):
    """GET /api/kiosco/qr/<alias>/factura.pdf -- la factura oficial (PDF del SIIC, tamaño carta)
    para las pantallas con impresora de hojas; las térmicas imprimen el ticket de /comprobante/."""

    def get(self, request, alias):
        pago = self._pago(request, alias)
        if pago is None:
            return Response({"error": "No encontrado."}, status=status.HTTP_404_NOT_FOUND)
        pdf = services.factura_pdf(pago)
        if pdf is None:
            return Response({"error": "Todavía no hay factura para este pago."}, status=status.HTTP_409_CONFLICT)
        return HttpResponse(pdf, content_type="application/pdf")


class EnviarCorreoView(_KioscoView):
    """POST /api/kiosco/qr/<alias>/enviar-correo/ {"destino": "registrado"} o {"correo": "..."}
    -- manda la factura oficial (PDF) por correo."""

    def post(self, request, alias):
        pago = self._pago(request, alias)
        if pago is None:
            return Response({"error": "No encontrado."}, status=status.HTTP_404_NOT_FOUND)
        datos = request.data if isinstance(request.data, dict) else {}
        correo = None if datos.get("destino") == "registrado" else str(datos.get("correo") or "")
        try:
            enviado_a = services.enviar_factura_por_correo(pago, correo)
        except services.KioscoError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({"enviado_a": enviado_a})


class EnviarWhatsappView(_KioscoView):
    """POST /api/kiosco/qr/<alias>/enviar-whatsapp/ {"destino": "registrado"} o {"celular": "..."}
    -- manda por WhatsApp los datos de la factura y el link del SIAT."""

    def post(self, request, alias):
        pago = self._pago(request, alias)
        if pago is None:
            return Response({"error": "No encontrado."}, status=status.HTTP_404_NOT_FOUND)
        datos = request.data if isinstance(request.data, dict) else {}
        celular = None if datos.get("destino") == "registrado" else str(datos.get("celular") or "")
        try:
            enviado_a = services.enviar_factura_por_whatsapp(pago, celular)
        except services.KioscoError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({"enviado_a": enviado_a})


class CancelarQrView(_KioscoView):
    """POST /api/kiosco/qr/<alias>/cancelar/ -- el cliente tocó Cancelar o se fue (inactividad)."""

    def post(self, request, alias):
        pago = self._pago(request, alias)
        if pago is None:
            return Response({"error": "No encontrado."}, status=status.HTTP_404_NOT_FOUND)
        if pago.estado == PagoKiosco.Estado.PENDIENTE:
            pago = services.dar_de_baja(pago, PagoKiosco.Estado.CANCELADO)
        return Response(services.estado_para_pantalla(pago))
