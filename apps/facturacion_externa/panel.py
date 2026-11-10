"""Pantalla "Pagos web" del panel (supervisor/admin): las liquidaciones que cessa-laravel pide
al gateway (`SolicitudLiquidacion`), con su estado, el motivo si fallaron, el PDF y la opción
de reintentar. A diferencia de urls.py (X-Api-Key, para cessa-laravel), esto usa el login JWT
del panel. Todo es lectura salvo `reintentar`, que hace lo mismo que el reintento de
cessa-laravel (liquidar_solicitud, idempotente)."""

from contextlib import contextmanager
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db import connection
from django.db.models import Count, Exists, OuterRef, Q, Sum
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.auditoria.models import LogAuditoria
from apps.usuarios.permissions import EsSupervisorOAdmin
from services.cobranzas_banco_client import CobranzasBancoError, get_cobranzas_banco_client

from .models import QrGenerado, SolicitudLiquidacion
from .services import liquidar_solicitud

# Estados de la lista que no son de una liquidación sino de un QR que no se pagó (QrGenerado).
ESTADOS_QR = ("expirado", "anulado", "vigente")


class LiquidacionSerializer(serializers.ModelSerializer):
    tipo = serializers.SerializerMethodField()
    tiene_pdf = serializers.SerializerMethodField()
    cantidad_comprobantes = serializers.SerializerMethodField()

    class Meta:
        model = SolicitudLiquidacion
        fields = [
            "id", "tipo", "alias", "origen", "nro_cliente", "monto", "moneda", "banco", "estado", "intentos", "error",
            "cobranzas_uuid", "fecha_pago", "recibido_en", "procesado_en", "tiene_pdf", "cantidad_comprobantes",
            "nota_descarte",
        ]
        read_only_fields = fields

    def get_tipo(self, obj):
        return "pago"

    def get_tiene_pdf(self, obj):
        return obj.comprobante_pdf is not None

    def get_cantidad_comprobantes(self, obj):
        return len(obj.detalle or [])


class LiquidacionDetalleSerializer(LiquidacionSerializer):
    class Meta(LiquidacionSerializer.Meta):
        fields = LiquidacionSerializer.Meta.fields + ["detalle", "numero_orden_originante"]
        read_only_fields = fields

    def to_representation(self, obj):
        """Lo mismo que el reporte Excel (reporte.py): glosa, descripción, fecha de creación,
        pagador y documento (del aviso de SIP) y quién lo generó."""
        from . import reporte

        datos = super().to_representation(obj)
        fila = reporte.filas([obj])[0]
        creado = reporte.creado_en(obj.alias)
        datos.update({
            "generado_por": reporte.GENERADO_POR.get(obj.origen, obj.origen),
            "glosa": reporte.glosa(len(obj.detalle or []), obj.nro_cliente, obj.monto),
            "descripcion": reporte.descripcion(obj),
            "creado_en": creado.isoformat() if creado else None,
            "pagador": fila.pagador,
            "documento_pagador": fila.documento_pagador,
            "numero_orden_originante": fila.nro_orden,
        })
        # Pagos del cajero automático (apps.kiosco): en qué pantalla y a dónde se mandó la factura.
        pago = getattr(obj, "pago_kiosco", None)
        if pago is not None:
            datos["cajero"] = {
                "pantalla": pago.kiosco,
                "nombre_cliente": pago.nombre_cliente,
                "nro_factura": pago.nro_factura,
                "envios": [
                    {"canal": e.get_canal_display(), "destino": e.destino, "registrado": e.registrado,
                     "exitoso": e.exitoso, "error": e.error, "enviado_en": e.enviado_en.isoformat()}
                    for e in pago.envios.all()
                ],
            }
        return datos


class QrSinPagarSerializer(serializers.ModelSerializer):
    """Un QR que no se pagó, con la misma forma que una liquidación en la lista (tipo "qr") y el
    detalle ya incluido: no tiene reintento, PDF ni transacción en api-cobranzas."""

    tipo = serializers.SerializerMethodField()
    estado = serializers.SerializerMethodField()
    cantidad_comprobantes = serializers.SerializerMethodField()
    recibido_en = serializers.DateTimeField(source="creado_en")

    class Meta:
        model = QrGenerado
        fields = [
            "id", "tipo", "alias", "origen", "nro_cliente", "monto", "banco", "estado", "observacion",
            "recibido_en", "creado_en", "vence_en", "cantidad_comprobantes", "detalle",
        ]
        read_only_fields = fields

    def get_tipo(self, obj):
        return "qr"

    def get_estado(self, obj):
        return obj.estado_actual()

    def get_cantidad_comprobantes(self, obj):
        return len(obj.detalle or [])

    def to_representation(self, obj):
        from . import reporte

        datos = super().to_representation(obj)
        datos.update({
            "moneda": "BOB", "intentos": 0, "error": "", "nota_descarte": "", "cobranzas_uuid": "",
            "fecha_pago": None, "procesado_en": None, "tiene_pdf": False,
            "generado_por": reporte.GENERADO_POR.get(obj.origen, obj.origen),
            "glosa": reporte.glosa(len(obj.detalle or []), obj.nro_cliente, obj.monto),
            "descripcion": reporte.descripcion(obj),
        })
        return datos


def _inicio_del_dia(fecha):
    return timezone.make_aware(datetime.combine(fecha, time.min))


def qr_sin_pagar(params, ahora=None):
    """QR generados que no tienen liquidación (no se pagaron), con los filtros de la lista.
    `estado` vacío = todos; "expirado" incluye los vigentes que ya pasaron su vencimiento."""
    ahora = ahora or timezone.now()
    pagados = SolicitudLiquidacion.objects.filter(alias=OuterRef("alias"))
    qs = QrGenerado.objects.annotate(pagado=Exists(pagados)).filter(pagado=False).order_by("-creado_en")
    vencido = Q(estado=QrGenerado.Estado.VIGENTE, vence_en__lte=ahora)
    estado = params.get("estado")
    if estado == "expirado":
        qs = qs.filter(Q(estado=QrGenerado.Estado.EXPIRADO) | vencido)
    elif estado == "vigente":
        qs = qs.filter(estado=QrGenerado.Estado.VIGENTE).exclude(vencido)
    elif estado == "anulado":
        qs = qs.filter(estado=QrGenerado.Estado.ANULADO)
    if params.get("cliente"):
        qs = qs.filter(nro_cliente=params["cliente"].strip())
    if params.get("banco"):
        qs = qs.filter(banco=params["banco"])
    if params.get("origen") in SolicitudLiquidacion.Origen.values:
        qs = qs.filter(origen=params["origen"])
    for campo, filtro in (("desde", "creado_en__gte"), ("hasta", "creado_en__lt")):
        if params.get(campo):
            try:
                fecha = datetime.strptime(params[campo], "%Y-%m-%d").date()
            except ValueError:
                continue
            if campo == "hasta":
                fecha += timedelta(days=1)
            qs = qs.filter(**{filtro: _inicio_del_dia(fecha)})
    return qs


class LiquidacionViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """GET /api/liquidaciones/?estado=error&cliente=115997&banco=bnb&origen=bot_telegram&desde=2026-10-01&hasta=2026-10-08
    (las 300 más nuevas que cumplan el filtro)."""

    permission_classes = [IsAuthenticated, EsSupervisorOAdmin]
    LIMITE = 300
    LIMITE_EXPORTAR = 5000

    def get_serializer_class(self):
        return LiquidacionDetalleSerializer if self.action == "retrieve" else LiquidacionSerializer

    def get_queryset(self):
        qs = SolicitudLiquidacion.objects.defer("comprobante_pdf").order_by("-recibido_en")
        p = self.request.query_params
        if self.action != "retrieve" and p.get("estado") in ESTADOS_QR:
            return qs.none()  # pidieron solo QR sin pagar
        if p.get("estado") in SolicitudLiquidacion.Estado.values:
            qs = qs.filter(estado=p["estado"])
        if p.get("cliente"):
            qs = qs.filter(nro_cliente=p["cliente"].strip())
        if p.get("banco"):
            qs = qs.filter(banco=p["banco"])
        if p.get("origen") in SolicitudLiquidacion.Origen.values:
            qs = qs.filter(origen=p["origen"])
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

    def _qr_sin_pagar(self):
        """Los QR sin pagar entran en "Todos" y en sus propias pestañas, no en las de liquidación."""
        estado = self.request.query_params.get("estado") or ""
        if estado and estado not in ESTADOS_QR:
            return QrGenerado.objects.none()
        return qr_sin_pagar(self.request.query_params)

    def list(self, request, *args, **kwargs):
        filas = LiquidacionSerializer(self.get_queryset()[: self.LIMITE], many=True).data
        filas += QrSinPagarSerializer(self._qr_sin_pagar()[: self.LIMITE], many=True).data
        filas.sort(key=lambda f: f["recibido_en"] or "", reverse=True)
        return Response(filas[: self.LIMITE])

    @action(detail=False, methods=["get"])
    def exportar(self, request):
        """Excel del reporte de cobros (reporte.py) con los mismos filtros que la lista, hasta
        LIMITE_EXPORTAR filas (la lista muestra 300). Incluye los QR expirados/anulados."""
        from . import reporte

        solicitudes = self.get_queryset()[: self.LIMITE_EXPORTAR]
        qrs = self._qr_sin_pagar()[: self.LIMITE_EXPORTAR]
        contenido = reporte.excel(solicitudes, generado_por=request.user.get_username(), qrs=qrs)
        nombre = f"cobros-qr-{timezone.localtime():%Y-%m-%d-%H%M%S}.xlsx"
        respuesta = HttpResponse(
            contenido, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        respuesta["Content-Disposition"] = f'attachment; filename="{nombre}"'
        return respuesta

    @action(detail=False, methods=["get"])
    def resumen(self, request):
        """Hoy: cantidad y monto por estado, y los errores pendientes de cualquier día."""
        hoy = SolicitudLiquidacion.objects.filter(recibido_en__gte=_inicio_del_dia(timezone.localdate()))
        por_estado = {
            fila["estado"]: {"cantidad": fila["cantidad"], "monto": f"{Decimal(fila['monto'] or 0):.2f}"}
            for fila in hoy.values("estado").annotate(cantidad=Count("id"), monto=Sum("monto"))
        }
        desde = timezone.localdate().isoformat()
        qr_hoy = {}
        for estado in ESTADOS_QR:
            totales = qr_sin_pagar({"estado": estado, "desde": desde}).aggregate(cantidad=Count("id"), monto=Sum("monto"))
            qr_hoy[estado] = {"cantidad": totales["cantidad"], "monto": f"{Decimal(totales['monto'] or 0):.2f}"}
        return Response({
            "hoy": {
                estado: por_estado.get(estado, {"cantidad": 0, "monto": "0.00"})
                for estado in SolicitudLiquidacion.Estado.values
            },
            "qr_hoy": qr_hoy,
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
