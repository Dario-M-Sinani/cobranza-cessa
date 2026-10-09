"""Pantalla "Pagos web" del panel (supervisor/admin): las liquidaciones que cessa-laravel pide
al gateway (`SolicitudLiquidacion`), con su estado, el motivo si fallaron, el PDF y la opción
de reintentar. A diferencia de urls.py (X-Api-Key, para cessa-laravel), esto usa el login JWT
del panel. Todo es lectura salvo `reintentar`, que hace lo mismo que el reintento de
cessa-laravel (liquidar_solicitud, idempotente)."""

from contextlib import contextmanager
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db import connection
from django.db.models import Count, Sum
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.auditoria.models import LogAuditoria
from apps.usuarios.permissions import EsSupervisorOAdmin
from services.cobranzas_banco_client import CobranzasBancoError, get_cobranzas_banco_client

from .models import SolicitudLiquidacion
from .services import liquidar_solicitud


class LiquidacionSerializer(serializers.ModelSerializer):
    tiene_pdf = serializers.SerializerMethodField()
    cantidad_comprobantes = serializers.SerializerMethodField()

    class Meta:
        model = SolicitudLiquidacion
        fields = [
            "id", "alias", "nro_cliente", "monto", "moneda", "banco", "estado", "intentos", "error",
            "cobranzas_uuid", "fecha_pago", "recibido_en", "procesado_en", "tiene_pdf", "cantidad_comprobantes",
            "nota_descarte",
        ]
        read_only_fields = fields

    def get_tiene_pdf(self, obj):
        return obj.comprobante_pdf is not None

    def get_cantidad_comprobantes(self, obj):
        return len(obj.detalle or [])


class LiquidacionDetalleSerializer(LiquidacionSerializer):
    class Meta(LiquidacionSerializer.Meta):
        fields = LiquidacionSerializer.Meta.fields + ["detalle", "numero_orden_originante"]
        read_only_fields = fields


def _inicio_del_dia(fecha):
    return timezone.make_aware(datetime.combine(fecha, time.min))


class LiquidacionViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """GET /api/liquidaciones/?estado=error&cliente=115997&banco=bnb&desde=2026-10-01&hasta=2026-10-08
    (las 300 más nuevas que cumplan el filtro)."""

    permission_classes = [IsAuthenticated, EsSupervisorOAdmin]
    LIMITE = 300

    def get_serializer_class(self):
        return LiquidacionDetalleSerializer if self.action == "retrieve" else LiquidacionSerializer

    def get_queryset(self):
        qs = SolicitudLiquidacion.objects.defer("comprobante_pdf").order_by("-recibido_en")
        p = self.request.query_params
        if p.get("estado") in SolicitudLiquidacion.Estado.values:
            qs = qs.filter(estado=p["estado"])
        if p.get("cliente"):
            qs = qs.filter(nro_cliente=p["cliente"].strip())
        if p.get("banco"):
            qs = qs.filter(banco=p["banco"])
        for campo, filtro in (("desde", "recibido_en__gte"), ("hasta", "recibido_en__lt")):
            if p.get(campo):
                try:
                    fecha = datetime.strptime(p[campo], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if campo == "hasta":
                    fecha += timedelta(days=1)
                qs = qs.filter(**{filtro: _inicio_del_dia(fecha)})
        return qs

    def list(self, request, *args, **kwargs):
        return Response(LiquidacionSerializer(self.get_queryset()[: self.LIMITE], many=True).data)

    @action(detail=False, methods=["get"])
    def resumen(self, request):
        """Hoy: cantidad y monto por estado, y los errores pendientes de cualquier día."""
        hoy = SolicitudLiquidacion.objects.filter(recibido_en__gte=_inicio_del_dia(timezone.localdate()))
        por_estado = {
            fila["estado"]: {"cantidad": fila["cantidad"], "monto": f"{Decimal(fila['monto'] or 0):.2f}"}
            for fila in hoy.values("estado").annotate(cantidad=Count("id"), monto=Sum("monto"))
        }
        return Response({
            "hoy": {
                estado: por_estado.get(estado, {"cantidad": 0, "monto": "0.00"})
                for estado in SolicitudLiquidacion.Estado.values
            },
            "errores_abiertos": SolicitudLiquidacion.objects.filter(estado=SolicitudLiquidacion.Estado.ERROR).count(),
        })

    @action(detail=True, methods=["get"])
    def pdf(self, request, pk=None):
        solicitud = self.get_object()
        if solicitud.comprobante_pdf is None:
            return Response({"detail": "Esta liquidación todavía no tiene comprobante."}, status=status.HTTP_404_NOT_FOUND)
        respuesta = HttpResponse(bytes(solicitud.comprobante_pdf), content_type="application/pdf")
        respuesta["Content-Disposition"] = f'inline; filename="{solicitud.alias}.pdf"'
        return respuesta

    @action(detail=True, methods=["get"])
    def remoto(self, request, pk=None):
        """La transacción en api-cobranzas-bancos (estado, lote, caja, total, anulación)."""
        solicitud = self.get_object()
        if not solicitud.cobranzas_uuid:
            return Response({"detail": "Todavía no tiene transacción en api-cobranzas."}, status=status.HTTP_404_NOT_FOUND)
        try:
            r = get_cobranzas_banco_client()._request_autenticado("get", f"/v1/transacciones/{solicitud.cobranzas_uuid}")
            datos = r.json() if r.ok else {"detail": r.text[:300]}
        except (CobranzasBancoError, ValueError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        if not r.ok:
            return Response(datos, status=status.HTTP_502_BAD_GATEWAY)
        claves = ("id", "estado", "lote", "caja_codigo", "total_comprobantes", "total_pagado", "fecha_pago", "fecha_anulacion")
        return Response({clave: datos.get(clave) for clave in claves})

    @action(detail=True, methods=["post"])
    def descartar(self, request, pk=None):
        """Cierra sin factura una liquidación con error o pendiente (revisada a mano): no se
        vuelve a intentar pagar y deja de contar para alertas. Pide un motivo; queda en auditoría."""
        motivo = str(request.data.get("motivo") or "").strip()
        if len(motivo) < 5:
            return Response({"detail": "Indica el motivo (al menos 5 caracteres)."}, status=status.HTTP_400_BAD_REQUEST)
        with _lock_liquidacion(int(pk)) as obtenido:
            if not obtenido:
                return Response({"detail": "Se está procesando; espera unos segundos."}, status=status.HTTP_409_CONFLICT)
            solicitud = self.get_object()
            if solicitud.estado not in (SolicitudLiquidacion.Estado.ERROR, SolicitudLiquidacion.Estado.PENDIENTE):
                return Response({"detail": f"No se puede descartar en estado {solicitud.estado}."}, status=status.HTTP_409_CONFLICT)
            solicitud.estado = SolicitudLiquidacion.Estado.DESCARTADO
            solicitud.nota_descarte = motivo[:255]
            solicitud.save(update_fields=["estado", "nota_descarte"])
            LogAuditoria.objects.create(
                usuario=request.user, accion=f"Liquidación web: descartada -- {motivo}"[:255],
                entidad_afectada=f"SolicitudLiquidacion:{solicitud.pk}",
            )
        return Response(LiquidacionSerializer(solicitud).data)

    @action(detail=True, methods=["post"])
    def reintentar(self, request, pk=None):
        """Mismo reintento que hace cessa-laravel (liquidar_solicitud). Dos clics seguidos no
        procesan la misma liquidación a la vez: lock consultivo de Postgres por id, sin dejar
        una transacción de base abierta durante las llamadas a api-cobranzas (cada paso de
        liquidar_solicitud se guarda apenas ocurre, como siempre)."""
        solicitud = self.get_object()
        if solicitud.estado == SolicitudLiquidacion.Estado.DESCARTADO:
            return Response({"detail": "Está descartada: no se reintenta."}, status=status.HTTP_409_CONFLICT)
        if solicitud.estado == SolicitudLiquidacion.Estado.FACTURADO:
            return Response({"detail": "Ya está facturada."}, status=status.HTTP_409_CONFLICT)
        with _lock_liquidacion(solicitud.pk) as obtenido:
            if not obtenido:
                return Response({"detail": "Ya se está procesando; espera unos segundos."}, status=status.HTTP_409_CONFLICT)
            solicitud.refresh_from_db()
            if solicitud.estado == SolicitudLiquidacion.Estado.FACTURADO:
                return Response({"detail": "Ya está facturada."}, status=status.HTTP_409_CONFLICT)
            LogAuditoria.objects.create(
                usuario=request.user, accion="Liquidación web: reintento manual",
                entidad_afectada=f"SolicitudLiquidacion:{solicitud.pk}",
            )
            solicitud = liquidar_solicitud(solicitud)
        return Response(LiquidacionSerializer(solicitud).data)


# Espacio propio para que el id no choque con otros locks consultivos de la base.
_ESPACIO_LOCK = 7341


@contextmanager
def _lock_liquidacion(pk: int):
    """pg_try_advisory_lock: no espera; si otro proceso la tiene, devuelve False. En SQLite
    (tests) no hay locks entre procesos: siempre True."""
    if connection.vendor != "postgresql":
        yield True
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [_ESPACIO_LOCK, pk])
        obtenido = cursor.fetchone()[0]
    try:
        yield obtenido
    finally:
        if obtenido:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [_ESPACIO_LOCK, pk])
