from django.http import HttpResponse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from services.cobranzas_banco_client import CobranzasBancoError, get_cobranzas_banco_client

from .models import SolicitudLiquidacion
from .permissions import TieneApiKeyServicioExterno
from .serializers import SolicitudLiquidacionEntradaSerializer, SolicitudLiquidacionSalidaSerializer
from .services import liquidar_solicitud


class LiquidarReciboExternoView(APIView):
    """cessa-laravel llama acá cuando un Recibo de su pago QR web queda
    Pagado, para que este backend (que sí tiene red hacia api-cobranzas-
    bancos) lo registre como factura real. Idempotente por `alias`: un mismo
    aviso reenviado (reintento de red del lado de cessa-laravel) no crea una
    segunda solicitud ni se vuelve a pagar si ya quedó FACTURADO."""

    authentication_classes = []
    permission_classes = [TieneApiKeyServicioExterno]

    def post(self, request):
        entrada = SolicitudLiquidacionEntradaSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data

        solicitud, _creada = SolicitudLiquidacion.objects.get_or_create(
            alias=datos["alias"],
            defaults={
                "nro_cliente": datos["nro_cliente"],
                "monto": datos["monto"],
                "moneda": datos["moneda"],
                "detalle": datos["detalle"],
                "fecha_pago": datos["fecha_pago"],
                "numero_orden_originante": datos.get("numero_orden_originante", ""),
            },
        )

        solicitud = liquidar_solicitud(solicitud)

        codigo = status.HTTP_200_OK if solicitud.estado == SolicitudLiquidacion.Estado.FACTURADO else status.HTTP_502_BAD_GATEWAY
        return Response(SolicitudLiquidacionSalidaSerializer(solicitud).data, status=codigo)


class ConsultarLiquidacionView(APIView):
    """Permite a cessa-laravel volver a preguntar el estado de una
    liquidación ya enviada (ej. si el primer POST se cayó en la respuesta
    pero sí llegó a procesarse acá)."""

    authentication_classes = []
    permission_classes = [TieneApiKeyServicioExterno]

    def get(self, request, alias):
        try:
            solicitud = SolicitudLiquidacion.objects.get(alias=alias)
        except SolicitudLiquidacion.DoesNotExist:
            return Response({"detail": "No encontrado."}, status=status.HTTP_404_NOT_FOUND)

        return Response(SolicitudLiquidacionSalidaSerializer(solicitud).data)


class ComprobanteLiquidacionView(APIView):
    authentication_classes = []
    permission_classes = [TieneApiKeyServicioExterno]

    def get(self, request, alias):
        try:
            solicitud = SolicitudLiquidacion.objects.get(alias=alias)
        except SolicitudLiquidacion.DoesNotExist:
            return Response({"detail": "No encontrado."}, status=status.HTTP_404_NOT_FOUND)

        if solicitud.comprobante_pdf is None:
            return Response({"detail": "Todavía no hay comprobante para esta liquidación."}, status=status.HTTP_404_NOT_FOUND)

        return HttpResponse(bytes(solicitud.comprobante_pdf), content_type="application/pdf")


class ComprobanteJsonLiquidacionView(APIView):
    """Datos estructurados del comprobante (nro_factura, cliente, detalle,
    etc.) para que cessa-laravel arme su ticket imprimible
    (ComprobanteTicketController) -- a diferencia del PDF, nunca se persiste
    acá: se reenvía en vivo contra api-cobranzas-bancos en cada pedido."""

    authentication_classes = []
    permission_classes = [TieneApiKeyServicioExterno]

    def get(self, request, alias):
        try:
            solicitud = SolicitudLiquidacion.objects.get(alias=alias)
        except SolicitudLiquidacion.DoesNotExist:
            return Response({"detail": "No encontrado."}, status=status.HTTP_404_NOT_FOUND)

        if not solicitud.cobranzas_uuid:
            return Response(
                {"detail": "Todavía no hay transacción en api-cobranzas-bancos para esta liquidación."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            documento = get_cobranzas_banco_client().obtener_comprobante_json(solicitud.cobranzas_uuid)
        except CobranzasBancoError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response(documento)
