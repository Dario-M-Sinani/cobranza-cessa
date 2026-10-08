from django.http import HttpResponse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from services.cobranzas_banco_client import CobranzasBancoError, get_cobranzas_banco_client
from services.deuda_client import (
    DeudaClientError,
    comprobante_pdf_siic,
    consultar_cliente_via_cobranzas,
    pagos_cliente_siic,
)

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
                "banco": datos.get("banco", ""),
            },
        )

        solicitud = liquidar_solicitud(solicitud)

        # Siempre 200, también si quedó en ERROR: el resultado de negocio va en `estado`/`error`.
        # Antes un rechazo devolvía 502, y Cloudflare (delante de test01.cessa.com.bo) reemplaza
        # cualquier 502 del origen por su propia página "error code: 502" -- cessa-laravel nunca
        # llegaba a ver el motivo real (hallazgo 2026-09-23).
        return Response(SolicitudLiquidacionSalidaSerializer(solicitud).data, status=status.HTTP_200_OK)


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


# Parámetros de `/v1/consulta/cliente` del SIIC que se reenvían; cualquier otro se descarta.
PARAMETROS_CONSULTA_CLIENTE = ("nro_cliente", "zona", "manzano", "correlativo", "ver_deuda", "ver_pagos")


class ConsultaClienteExternaView(APIView):
    """Deuda de un cliente para cessa-laravel, leída **como banco** a través de
    api-cobranzas-bancos (`GET /v1/consulta/deuda`, mismo login con el que
    después se paga). Mismos parámetros y misma respuesta que el
    `/v1/consulta/cliente` del SIIC, para que cessa-laravel solo cambie a quién
    le pregunta. Así la deuda que se cobra por QR sale del mismo SIIC donde
    `recibos-web/liquidar/` la paga (antes cessa-laravel leía SIIC prod y el
    pago iba a test: "La deuda no existe con los datos proporcionados").

    El status de api-cobranzas se reenvía tal cual (404 = no existe el abonado,
    con `{"error": ...}`). Si api-cobranzas o el SIIC no responden: 503, no 502,
    porque Cloudflare reemplaza los 502 por su propia página."""

    authentication_classes = []
    permission_classes = [TieneApiKeyServicioExterno]

    def get(self, request):
        params = {k: request.query_params[k] for k in PARAMETROS_CONSULTA_CLIENTE if k in request.query_params}
        if not params.get("nro_cliente") and not params.get("zona"):
            return Response({"error": "Falta nro_cliente (o zona/manzano/correlativo)."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            codigo, body = consultar_cliente_via_cobranzas(params)
        except DeudaClientError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(body, status=codigo)


class PagosClienteExternaView(APIView):
    """Últimos comprobantes pagados de un cliente (por cualquier canal) para "Tus últimas
    facturas" de cessa-laravel, leídos del mismo SIIC que la deuda (SIIC_DEUDA_BASE_URL). Mismo
    contrato que `/v1/clientes/{c}/pagos` del SIIC. cessa-laravel ya verificó la cuenta antes."""

    authentication_classes = []
    permission_classes = [TieneApiKeyServicioExterno]

    def get(self, request, nro_cliente):
        try:
            limit = max(1, min(int(request.query_params.get("limit", 12)), 50))
        except ValueError:
            limit = 12
        try:
            codigo, body = pagos_cliente_siic(nro_cliente, limit)
        except DeudaClientError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(body, status=codigo)


class ComprobantePdfExternaView(APIView):
    """PDF de UN comprobante (`POST /v1/comprobantes` formato pdf) del mismo SIIC que la deuda.
    Solo se acepta `{"item": {...}}` con la clave del comprobante; el formato lo fija el gateway."""

    authentication_classes = []
    permission_classes = [TieneApiKeyServicioExterno]

    def post(self, request):
        item = request.data.get("item") if isinstance(request.data, dict) else None
        if not isinstance(item, dict) or not item:
            return Response({"error": "Falta item (clave del comprobante)."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            pdf = comprobante_pdf_siic(item)
        except DeudaClientError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if pdf is None:
            return Response({"error": "El SIIC no generó el PDF de ese comprobante."}, status=status.HTTP_404_NOT_FOUND)
        return HttpResponse(pdf, content_type="application/pdf")
