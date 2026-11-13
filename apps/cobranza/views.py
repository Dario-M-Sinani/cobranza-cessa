import csv
import re
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.clientes.models import Cliente
from apps.usuarios.models import Usuario
from apps.usuarios.permissions import EsCajeraActiva, EsSupervisorOAdmin
from apps.auditoria.models import LogAuditoria
from services import siic_historial
from services.cobranzas_banco_client import CajeraCobranzasClient, CobranzasBancoError
from services.deuda_client import ClienteNoEncontradoError, DeudaClientError, get_deuda_client

from .models import (
    AperturaCajaFueraDeHorario,
    Caja,
    CajaOperacionInvalida,
    CobroAgrupado,
    CobroEfectivo,
    Deuda,
    Factura,
    ImpresionComprobante,
    TransaccionQR,
    TransicionEstadoInvalida,
)
from .serializers import (
    AbrirCajaSerializer,
    AperturaCajaFueraDeHorarioSerializer,
    CajaSerializer,
    CobroAgrupadoSerializer,
    CobroEfectivoSerializer,
    ConsultarDeudaSerializer,
    DeudaSerializer,
    FacturaSerializer,
    GenerarTransaccionQRSerializer,
    ReabrirCajaSerializer,
    RegistrarCobroAgrupadoSerializer,
    RegistrarCobroEfectivoSerializer,
    TransaccionQRSerializer,
)
from .services import (
    DeudaSinSaldoError,
    ErrorFacturacion,
    ErrorGeneracionQR,
    MontoInvalidoError,
    MontoRecibidoInsuficienteError,
    TransaccionEnCursoError,
    generar_transaccion_qr,
    registrar_cobro_efectivo,
    registrar_cobro_efectivo_agrupado,
)
from .services import reintentar_facturacion as reintentar_facturacion_servicio
from . import anulacion, cierre, formas_pago, proformas
from .reportes import resumen_caja, resumen_dashboard


def _parsear_fecha(valor):
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except ValueError:
        return None


def _money(valor) -> str:
    # SQLite (usado solo en dev/tests -- Postgres es la base real) no
    # conserva la escala de DecimalField a través de un SUM agregado, así
    # que se normaliza acá para que la API siempre devuelva 2 decimales
    # sin importar el motor.
    return str((valor or Decimal("0.00")).quantize(Decimal("0.01")))


class DashboardResumenView(APIView):
    """Resumen del día (o del rango `?desde=YYYY-MM-DD&hasta=YYYY-MM-DD`)
    para los dashboards de supervisor/admin -- Épicas G y H de
    HISTORIAS_USUARIO.md. Mismo endpoint para ambos roles: la diferencia de
    "detalle" entre supervisor y administrador la decide el frontend (ver
    nota de alcance en reportes.py sobre por qué no hay filtro de sucursal
    todavía)."""

    permission_classes = [IsAuthenticated, EsSupervisorOAdmin]

    def get(self, request):
        hoy = timezone.localdate()
        desde = _parsear_fecha(request.query_params.get("desde")) or hoy
        hasta = _parsear_fecha(request.query_params.get("hasta")) or hoy
        if desde > hasta:
            return Response({"detail": "'desde' no puede ser posterior a 'hasta'."}, status=status.HTTP_400_BAD_REQUEST)

        resumen = resumen_dashboard(desde=desde, hasta=hasta)

        return Response({
            "desde": resumen["desde"].isoformat(),
            "hasta": resumen["hasta"].isoformat(),
            "monto_total": _money(resumen["monto_total"]),
            "cantidad_cobros": resumen["cantidad_cobros"],
            "por_forma_pago": {
                "qr": {
                    "cantidad": resumen["por_forma_pago"]["qr"]["cantidad"],
                    "monto": _money(resumen["por_forma_pago"]["qr"]["monto"]),
                },
                "efectivo": {
                    "cantidad": resumen["por_forma_pago"]["efectivo"]["cantidad"],
                    "monto": _money(resumen["por_forma_pago"]["efectivo"]["monto"]),
                },
            },
            "facturas_registradas": resumen["facturas_registradas"],
            "cajas": CajaSerializer(resumen["cajas"], many=True).data,
            "por_cajero": [
                {
                    "cajero": fila["cajero"],
                    "cantidad_cobros": fila["cantidad_cobros"],
                    "monto_total": _money(fila["monto_total"]),
                }
                for fila in resumen["por_cajero"]
            ],
        })


class ConsultarDeudaView(APIView):
    """Consulta la deuda de un cliente contra la fuente configurada (hoy: el
    intermedio del SIIC) y guarda un nuevo snapshot de Deuda. Cada consulta
    crea un registro nuevo -- nunca se actualiza uno existente -- para
    preservar el histórico y para que el monto_snapshot de una TransaccionQR
    generada después siga siendo el de ese momento."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        entrada = ConsultarDeudaSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        codigo_externo = entrada.validated_data["codigo_externo"]
        cuenta = re.fullmatch(r"(\d{2})-?(\d{3})-?(\d{5})", codigo_externo)

        try:
            if cuenta:
                resultado = get_deuda_client().consultar_por_cuenta(*cuenta.groups())
                codigo_externo = resultado.codigo_externo_cliente
            else:
                resultado = get_deuda_client().consultar_deuda(codigo_externo)
        except ClienteNoEncontradoError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except DeudaClientError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        cliente, _ = Cliente.objects.update_or_create(
            codigo_externo=codigo_externo,
            defaults={"nombre": resultado.nombre_cliente or codigo_externo},
        )
        items = [asdict(item) for item in resultado.items]
        # Consumo (kWh) para mostrar al cajero; best effort, no frena el cobro si el SIIC falla.
        # construir_detalle() arma el detalle de pago con campos explícitos: esta clave extra
        # no viaja a api-cobranzas.
        tabla_consumos = siic_historial.consumos(codigo_externo) if items else {}
        for item in items:
            datos = siic_historial.consumo_de(item, tabla_consumos)
            item["consumo_kwh"] = datos["consumo_kwh"] if datos else None
        deuda = Deuda.objects.create(
            cliente=cliente,
            monto=resultado.monto_total,
            # Snapshot crudo por ítem -- lo necesita enviar_factura_a_siic()
            # para armar el "detalle" real que exige api-cobranzas-bancos.
            items_snapshot=items,
            datos_cliente=resultado.datos_cliente,
        )

        return Response(DeudaSerializer(deuda).data, status=status.HTTP_201_CREATED)


class TransaccionQRViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Sin update/destroy: el estado solo cambia vía las acciones de abajo,
    que a su vez delegan en TransaccionQR.transicionar_estado()."""

    serializer_class = TransaccionQRSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = TransaccionQR.objects.select_related("deuda__cliente", "usuario").order_by("-creado_en")
        if self.request.user.rol == Usuario.Rol.CAJERA:
            qs = qs.filter(usuario=self.request.user)
        return qs

    def get_permissions(self):
        if self.action == "create":
            return [IsAuthenticated(), EsCajeraActiva()]
        if self.action == "reintentar_facturacion":
            return [IsAuthenticated(), EsSupervisorOAdmin()]
        return super().get_permissions()

    def create(self, request, *args, **kwargs):
        entrada = GenerarTransaccionQRSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        deuda = entrada.validated_data["deuda"]
        monto = entrada.validated_data.get("monto")
        cantidad = entrada.validated_data.get("cantidad_comprobantes")

        try:
            transaccion = generar_transaccion_qr(
                deuda=deuda, usuario=request.user, monto=monto, cantidad_comprobantes=cantidad
            )
        except DeudaSinSaldoError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except MontoInvalidoError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except TransaccionEnCursoError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except ErrorGeneracionQR as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response(TransaccionQRSerializer(transaccion).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancelar(self, request, pk=None):
        transaccion = self.get_object()
        try:
            transaccion.transicionar_estado(TransaccionQR.Estado.CANCELADO, usuario=request.user)
        except TransicionEstadoInvalida as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(TransaccionQRSerializer(transaccion).data)

    @action(detail=True, methods=["post"])
    def reintentar_facturacion(self, request, pk=None):
        transaccion = self.get_object()
        try:
            factura = reintentar_facturacion_servicio(transaccion)
        except ErrorFacturacion as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(FacturaSerializer(factura).data)

    @action(detail=False, methods=["get"], permission_classes=[IsAuthenticated, EsSupervisorOAdmin])
    def exportar_csv(self, request):
        """Exporta el mismo alcance que get_queryset() (todas las
        transacciones, para supervisor/admin -- este action ya está
        restringido a esos roles). `?estado=` filtra opcionalmente."""
        qs = self.get_queryset()
        estado = request.query_params.get("estado")
        if estado:
            qs = qs.filter(estado=estado)

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="transacciones_qr.csv"'

        writer = csv.writer(response)
        writer.writerow([
            "id", "cliente", "codigo_cliente", "monto", "estado", "cajera",
            "numero_factura", "estado_factura", "creado_en",
        ])
        for transaccion in qs:
            factura = getattr(transaccion, "factura", None)
            writer.writerow([
                transaccion.id,
                transaccion.deuda.cliente.nombre,
                transaccion.deuda.cliente.codigo_externo,
                transaccion.monto_snapshot,
                transaccion.estado,
                transaccion.usuario.username,
                factura.numero_factura if factura else "",
                factura.estado_envio if factura else "",
                transaccion.creado_en.strftime("%Y-%m-%d %H:%M"),
            ])

        return response


class FacturaViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = FacturaSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = Factura.objects.select_related(
            "transaccion_qr__usuario", "cobro_efectivo__usuario"
        ).order_by("-id")
        if self.request.user.rol == Usuario.Rol.CAJERA:
            # Bug real corregido 2026-09-07: filtrar solo por
            # transaccion_qr__usuario dejaba a una cajera sin ver sus
            # propias facturas de efectivo (esa Factura tiene
            # transaccion_qr=None, así que ese filtro nunca matcheaba).
            qs = qs.filter(
                Q(transaccion_qr__usuario=self.request.user)
                | Q(cobro_efectivo__usuario=self.request.user)
            )
        return qs

    @action(detail=True, methods=["get"])
    def comprobante(self, request, pk=None):
        factura = self.get_object()
        if factura.comprobante_pdf is None:
            return Response(
                {"detail": "Todavía no hay comprobante para esta factura."}, status=status.HTTP_404_NOT_FOUND
            )
        return HttpResponse(bytes(factura.comprobante_pdf), content_type="application/pdf")


class CobroEfectivoViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Sin update/destroy: un cobro en efectivo se confirma al instante, no
    tiene estados intermedios que cambiar (a diferencia de TransaccionQR)."""

    serializer_class = CobroEfectivoSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = CobroEfectivo.objects.select_related("deuda__cliente", "usuario", "caja").order_by("-creado_en")
        if self.request.user.rol == Usuario.Rol.CAJERA:
            qs = qs.filter(usuario=self.request.user)
        return qs

    def get_permissions(self):
        if self.action == "create":
            return [IsAuthenticated(), EsCajeraActiva()]
        return super().get_permissions()

    def create(self, request, *args, **kwargs):
        entrada = RegistrarCobroEfectivoSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        deuda = entrada.validated_data["deuda"]
        monto_recibido = entrada.validated_data["monto_recibido"]
        monto_a_cobrar = entrada.validated_data.get("monto_a_cobrar")
        cantidad = entrada.validated_data.get("cantidad_comprobantes")
        datos_documento = entrada.validated_data.get("documento")
        try:
            documento = formas_pago.validar_documento(request.user, datos_documento) if datos_documento else None
        except formas_pago.FormaPagoInvalida as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            cobro = registrar_cobro_efectivo(
                deuda=deuda,
                usuario=request.user,
                monto_recibido=monto_recibido,
                monto_a_cobrar=monto_a_cobrar,
                cantidad_comprobantes=cantidad,
                documento=documento,
            )
        except DeudaSinSaldoError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except MontoInvalidoError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except TransaccionEnCursoError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except MontoRecibidoInsuficienteError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(CobroEfectivoSerializer(cobro).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"], url_path="formas-pago")
    def formas_pago(self, request):
        """Tipos de documento (cheque, depósito, tarjeta...) y bancos del SIIC para cobrar sin billetes.
        `disponible: false` si el usuario no tiene caja del SIIC (la pestaña no se muestra)."""
        try:
            return Response(formas_pago.catalogo(request.user))
        except formas_pago.FormaPagoInvalida as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class CajaViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Sin update/destroy genérico: el estado solo cambia vía Caja.abrir()/
    cerrar()/reabrir(), disparados por create()/cerrar()/reabrir() de abajo."""

    serializer_class = CajaSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = Caja.objects.select_related("cajero", "abierta_por", "cerrada_por").order_by("-creada_en")
        if self.request.user.rol == Usuario.Rol.CAJERA:
            qs = qs.filter(cajero=self.request.user)
        # Nota: supervisor y admin ven todas las cajas por ahora. El filtro
        # de "sus cajas" por sucursal para el supervisor queda pendiente --
        # ver REQUISITOS_COBRANZA.md pregunta 8 (no existe todavía el
        # vínculo supervisor↔sucursal/cajero en el modelo de Usuario).
        return qs

    def get_permissions(self):
        if self.action in ("reabrir", "aperturas_fuera_de_horario", "resumen", "reporte_cierre"):
            return [IsAuthenticated(), EsSupervisorOAdmin()]
        return super().get_permissions()

    def create(self, request, *args, **kwargs):
        """Abre una caja. Una cajera activa abre la suya propia (no manda
        `cajero_id`); un supervisor/admin puede abrir la de un cajero
        puntual (p. ej. fuera de horario, con `motivo` obligatorio en ese
        caso -- ver Caja.abrir())."""
        usuario = request.user
        entrada = AbrirCajaSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        cajero_id = entrada.validated_data.get("cajero_id")

        if usuario.rol == Usuario.Rol.CAJERA:
            if not usuario.activo:
                return Response({"detail": "Cajero inactivo."}, status=status.HTTP_403_FORBIDDEN)
            cajero = usuario
        elif usuario.rol in (Usuario.Rol.SUPERVISOR, Usuario.Rol.ADMIN):
            if cajero_id is None:
                return Response({"detail": "cajero_id es requerido."}, status=status.HTTP_400_BAD_REQUEST)
            try:
                cajero = Usuario.objects.get(pk=cajero_id, rol=Usuario.Rol.CAJERA)
            except Usuario.DoesNotExist:
                return Response({"detail": "Cajero no encontrado."}, status=status.HTTP_404_NOT_FOUND)
        else:
            return Response({"detail": "Rol no autorizado para abrir caja."}, status=status.HTTP_403_FORBIDDEN)

        try:
            caja = Caja.abrir(
                cajero=cajero,
                usuario=usuario,
                motivo=entrada.validated_data["motivo"],
                agencia_sigla=entrada.validated_data["agencia_sigla"],
                monto_inicial=entrada.validated_data["monto_inicial"],
            )
        except CajaOperacionInvalida as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(CajaSerializer(caja).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cerrar(self, request, pk=None):
        """Una caja del SIIC (CAJAS_POR_CAJERA) se cierra primero allá, con el arqueo que declaró la
        cajera (ver apps/cobranza/cierre.py); una caja solo local, como siempre."""
        caja = self.get_object()
        try:
            if caja.caja_codigo:
                cierre.cerrar_en_siic(caja, request.user, str(request.data.get("descripcion") or ""))
            else:
                caja.cerrar(usuario=request.user)
        except CajaOperacionInvalida as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(CajaSerializer(caja).data)

    @action(detail=True, methods=["get"], url_path="cierre")
    def ver_cierre(self, request, pk=None):
        """Arqueo de la caja del SIIC: documentos del día, cortes contados y lo que falta para cerrar.
        Recaudado y diferencia solo para supervisor/admin."""
        caja = self.get_object()
        ver_totales = request.user.rol in (Usuario.Rol.SUPERVISOR, Usuario.Rol.ADMIN)
        try:
            return Response(cierre.estado_cierre(caja, ver_totales))
        except CajaOperacionInvalida as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["put"])
    def efectivo(self, request, pk=None):
        """Efectivo contado en Bs por cortes: {"cortes": {"200": 3, "100": 0, ..., "010": 5}}."""
        caja = self.get_object()
        cortes = request.data.get("cortes")
        if not isinstance(cortes, dict):
            return Response({"detail": "Faltan los cortes."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response(cierre.declarar_efectivo(caja, request.user, cortes))
        except CajaOperacionInvalida as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"])
    def documentos(self, request, pk=None):
        """Agrega un documento recibido a mano (cheque, depósito, tarjeta...); con `indice`, lo corrige."""
        caja = self.get_object()
        try:
            return Response(cierre.guardar_documento(caja, request.user, request.data), status=status.HTTP_201_CREATED)
        except (CajaOperacionInvalida, TypeError, ValueError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["get"], url_path="reporte-cierre")
    def reporte_cierre(self, request, pk=None):
        caja = self.get_object()
        try:
            pdf = cierre.reporte_cierre(caja)
        except CajaOperacionInvalida as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        respuesta = HttpResponse(pdf, content_type="application/pdf")
        respuesta["Content-Disposition"] = f'inline; filename="cierre-caja-{caja.caja_codigo}-{caja.pk}.pdf"'
        return respuesta

    @action(detail=True, methods=["post"])
    def reabrir(self, request, pk=None):
        caja = self.get_object()
        entrada = ReabrirCajaSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        try:
            caja.reabrir(usuario=request.user, motivo=entrada.validated_data["motivo"])
        except CajaOperacionInvalida as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(CajaSerializer(caja).data)

    @action(detail=False, methods=["get"])
    def agencias(self, request):
        """Agencias donde puede abrir caja (CAJAS_POR_CAJERA): las de la entidad financiera de la
        cajera (`?cajero_id=` cuando abre un supervisor por ella). Vacío si la opción está apagada
        o el usuario es local: la caja se abre sin agencia, como antes."""
        cajero = request.user
        if request.user.rol != Usuario.Rol.CAJERA and request.query_params.get("cajero_id"):
            cajero = Usuario.objects.filter(pk=request.query_params["cajero_id"], rol=Usuario.Rol.CAJERA).first()
            if cajero is None:
                return Response({"detail": "Cajero no encontrado."}, status=status.HTTP_404_NOT_FOUND)
        if not settings.CAJAS_POR_CAJERA or cajero.origen != Usuario.Origen.NEST or not cajero.entidad_financiera_id:
            return Response({"requiere_agencia": False, "agencias": []})
        try:
            agencias = CajeraCobranzasClient(cajero).agencias(cajero.entidad_financiera_id)
        except CobranzasBancoError as exc:
            return Response({"detail": f"No se pudieron leer las agencias: {exc}"}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({
            "requiere_agencia": True,
            "agencias": [
                {"sigla": str(a.get("sigla_agencia") or "").strip(), "descripcion": str(a.get("descripcion") or "").strip()}
                for a in agencias
                if str(a.get("sigla_agencia") or "").strip()
            ],
        })

    @action(detail=False, methods=["get"])
    def aperturas_fuera_de_horario(self, request):
        """Listado de aperturas/reaperturas de caja fuera de horario, con
        motivo y quién la realizó -- respuesta a E8 de HISTORIAS_USUARIO.md."""
        qs = AperturaCajaFueraDeHorario.objects.select_related("caja", "usuario").order_by("-creado_en")
        return Response(AperturaCajaFueraDeHorarioSerializer(qs, many=True).data)

    @action(detail=True, methods=["get"])
    def resumen(self, request, pk=None):
        """Corte de caja: total cobrado (QR pagado + efectivo) en este
        turno, desglose por forma de pago, facturas registradas, y cuántos
        QR quedaron generados sin confirmar todavía. Sirve tanto para una
        caja ya cerrada como para una abierta (total corrido). Exclusivo de
        supervisor/admin (ver `get_permissions()`) -- a pedido del usuario
        2026-09-07, el cajero no debe poder ver el total recaudado, ni el
        de su propia caja."""
        caja = self.get_object()
        resumen = resumen_caja(caja)

        return Response({
            "monto_total": _money(resumen["monto_total"]),
            "cantidad_cobros": resumen["cantidad_cobros"],
            "por_forma_pago": {
                "qr": {
                    "cantidad": resumen["por_forma_pago"]["qr"]["cantidad"],
                    "monto": _money(resumen["por_forma_pago"]["qr"]["monto"]),
                },
                "efectivo": {
                    "cantidad": resumen["por_forma_pago"]["efectivo"]["cantidad"],
                    "monto": _money(resumen["por_forma_pago"]["efectivo"]["monto"]),
                },
            },
            "facturas_registradas": resumen["facturas_registradas"],
            "qr_pendientes_de_confirmar": resumen["qr_pendientes_de_confirmar"],
            # Caja del SIIC ya cerrada: arqueo, recaudado y diferencia que devolvió el cierre.
            "cierre_siic": caja.cierre_siic.get("datos") if caja.cierre_siic else None,
        })


def _solo_digitos(codigo: str) -> str | None:
    codigo = (codigo or "").strip()
    return codigo if codigo.isdigit() and len(codigo) <= 10 else None


class FacturasPagadasView(APIView):
    """Todas las facturas ya pagadas del cliente (por cualquier canal), de la más nueva a
    la más vieja, leídas del SIIC (services/siic_historial.py). Solo lectura."""

    permission_classes = [IsAuthenticated]

    def get(self, request, codigo):
        if not _solo_digitos(codigo):
            return Response({"detail": "Número de cliente inválido."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            items = siic_historial.facturas_pagadas(codigo)
        except siic_historial.SiicHistorialError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        tabla_consumos = siic_historial.consumos(codigo) if items else {}
        for item in items:
            periodo = siic_historial.periodo_de_detalle(item.get("detalle", ""))
            datos = siic_historial.consumo_de(item, tabla_consumos, periodo) if periodo else None
            item["consumo_kwh"] = datos["consumo_kwh"] if datos else None
        return Response({"items": items})


class FacturaPagadaPdfView(APIView):
    """PDF de una factura ya pagada (reimpresión). El body es la clave del comprobante tal
    como vino en FacturasPagadasView; tiene que ser del mismo cliente de la URL."""

    permission_classes = [IsAuthenticated]

    def post(self, request, codigo):
        if not _solo_digitos(codigo):
            return Response({"detail": "Número de cliente inválido."}, status=status.HTTP_400_BAD_REQUEST)
        clave = request.data if isinstance(request.data, dict) else {}
        if any(campo not in clave for campo in siic_historial.CAMPOS_CLAVE):
            return Response({"detail": "Falta la clave completa del comprobante."}, status=status.HTTP_400_BAD_REQUEST)
        if str(clave.get("nro_cliente")).strip() != codigo:
            return Response({"detail": "El comprobante no es de este cliente."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            pdf = siic_historial.factura_pdf(clave)
        except siic_historial.SiicHistorialError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        LogAuditoria.objects.create(
            usuario=request.user,
            accion="Factura SIIC: reimpresión",
            entidad_afectada=f"Cliente:{codigo} Comprobante:{clave.get('nro_comprobante')}",
        )
        respuesta = HttpResponse(pdf, content_type="application/pdf")
        respuesta["Content-Disposition"] = f'inline; filename="factura-{codigo}-{clave.get("nro_comprobante")}.pdf"'
        return respuesta


class CobroAgrupadoViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Un pago en efectivo que cubre deudas de varios clientes. POST con
    `{"monto_recibido": "...", "selecciones": [{"deuda_id": 1, "cantidad_comprobantes": 2}, ...]}`."""

    serializer_class = CobroAgrupadoSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = CobroAgrupado.objects.select_related("usuario", "caja").prefetch_related(
            "cobros__deuda__cliente", "cobros__usuario", "cobros__caja"
        )
        if self.request.user.rol == Usuario.Rol.CAJERA:
            qs = qs.filter(usuario=self.request.user)
        return qs

    def get_permissions(self):
        if self.action == "create":
            return [IsAuthenticated(), EsCajeraActiva()]
        return super().get_permissions()

    def create(self, request, *args, **kwargs):
        entrada = RegistrarCobroAgrupadoSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data
        selecciones = [(s["deuda"], s.get("cantidad_comprobantes")) for s in datos["selecciones"]]
        try:
            documento = formas_pago.validar_documento(request.user, datos["documento"]) if datos.get("documento") else None
        except formas_pago.FormaPagoInvalida as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        try:
            grupo = registrar_cobro_efectivo_agrupado(
                usuario=request.user, monto_recibido=datos["monto_recibido"], selecciones=selecciones, documento=documento
            )
        except (DeudaSinSaldoError, MontoInvalidoError, MontoRecibidoInsuficienteError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except TransaccionEnCursoError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(CobroAgrupadoSerializer(self.get_queryset().get(pk=grupo.pk)).data, status=status.HTTP_201_CREATED)


class UltimoComprobanteView(APIView):
    """El último cobro confirmado del usuario, para reimprimir su comprobante (tecla F9 del
    panel): efectivo individual, efectivo de varios clientes (grupo) o QR ya pagado.
    Responde `{"tipo": "efectivo"|"grupo"|"qr", "id": N}` o 404 si todavía no cobró nada."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        candidatos = []
        efectivo = CobroEfectivo.objects.filter(usuario=request.user).order_by("-creado_en").first()
        if efectivo:
            tipo, pk = ("grupo", efectivo.grupo_id) if efectivo.grupo_id else ("efectivo", efectivo.pk)
            candidatos.append((efectivo.creado_en, tipo, pk))
        qr = (
            TransaccionQR.objects.filter(usuario=request.user, estado=TransaccionQR.Estado.PAGADO)
            .order_by("-actualizado_en")
            .first()
        )
        if qr:
            candidatos.append((qr.actualizado_en, "qr", qr.pk))
        if not candidatos:
            return Response({"detail": "Todavía no hay cobros registrados con tu usuario."}, status=status.HTTP_404_NOT_FOUND)
        _, tipo, pk = max(candidatos)
        return Response({"tipo": tipo, "id": pk})


class RegistrarImpresionView(APIView):
    """Se llama justo antes de imprimir el comprobante interno de un cobro.
    Body: `{"tipo": "efectivo"|"qr"|"grupo", "id": N}`.

    - Primera impresión: la puede hacer quien ve el cobro (la cajera, el suyo).
    - Reimpresión: solo supervisor/admin, queda en Auditoría y el ticket sale marcado.
    - La cajera que intenta reimprimir recibe 403.
    Un cobro en efectivo que es parte de un grupo cuenta como el grupo (un solo comprobante).
    Responde `{"tipo", "id", "es_reimpresion", "impresiones"}`."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        tipo = str(request.data.get("tipo", ""))
        try:
            pk = int(request.data.get("id"))
        except (TypeError, ValueError):
            return Response({"detail": "Comprobante no válido."}, status=status.HTTP_400_BAD_REQUEST)

        modelos = {"efectivo": CobroEfectivo, "qr": TransaccionQR, "grupo": CobroAgrupado}
        if tipo not in modelos:
            return Response({"detail": "Comprobante no válido."}, status=status.HTTP_400_BAD_REQUEST)
        qs = modelos[tipo].objects.all()
        if request.user.rol == Usuario.Rol.CAJERA:
            qs = qs.filter(usuario=request.user)
        objeto = qs.filter(pk=pk).first()
        if objeto is None:
            return Response({"detail": "Comprobante no encontrado."}, status=status.HTTP_404_NOT_FOUND)
        if tipo == "qr" and objeto.estado != TransaccionQR.Estado.PAGADO:
            return Response({"detail": "El QR todavía no fue pagado."}, status=status.HTTP_400_BAD_REQUEST)
        if tipo == "efectivo" and objeto.grupo_id:
            tipo, pk = "grupo", objeto.grupo_id

        with transaction.atomic():
            # Bloquea las impresiones previas de este comprobante: dos clics seguidos no
            # pueden contar los dos como "primera".
            previas = list(
                ImpresionComprobante.objects.select_for_update().filter(tipo=tipo, objeto_id=pk).values_list("pk", flat=True)
            )
            es_reimpresion = bool(previas)
            if es_reimpresion and request.user.rol not in (Usuario.Rol.SUPERVISOR, Usuario.Rol.ADMIN):
                return Response(
                    {"detail": "Este comprobante ya se imprimió. Para una reimpresión pide a un supervisor."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            ImpresionComprobante.objects.create(tipo=tipo, objeto_id=pk, usuario=request.user, es_reimpresion=es_reimpresion)
            if es_reimpresion:
                LogAuditoria.objects.create(
                    usuario=request.user,
                    accion="Comprobante: reimpresión",
                    entidad_afectada=f"{ImpresionComprobante.Tipo(tipo).label}:{pk}",
                )
        return Response({"tipo": tipo, "id": pk, "es_reimpresion": es_reimpresion, "impresiones": len(previas) + 1})


def _forma_pago(cobro) -> str:
    """"Efectivo", o el documento con que se pagó ("Cheque", "Deposito"...)."""
    return (cobro.documento.get("ente") or "").capitalize() or "Efectivo"


class AnularComprobanteView(APIView):
    """`POST /api/comprobantes/anular/ {"tipo": "efectivo"|"qr"|"grupo", "id": N, "motivo": "..."}`.
    Supervisor/admin, solo el mismo día (ver apps/cobranza/anulacion.py)."""

    permission_classes = [IsAuthenticated, EsSupervisorOAdmin]

    def post(self, request):
        try:
            objeto_id = int(request.data.get("id"))
        except (TypeError, ValueError):
            return Response({"detail": "Falta el comprobante."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            facturas = anulacion.anular_comprobante(
                str(request.data.get("tipo") or ""), objeto_id, request.user, str(request.data.get("motivo") or "")
            )
        except anulacion.AnulacionInvalida as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"anuladas": len(facturas), "facturas": [f.numero_factura or f.pk for f in facturas]})


class ComprobantesDelDiaView(APIView):
    """Cobros del panel de un día, para que supervisor/admin encuentre un comprobante y lo
    reimprima. `GET /api/comprobantes/?fecha=AAAA-MM-DD` (por defecto hoy, hora de Bolivia).

    Efectivo individual, efectivo de varios clientes (una fila por grupo) y QR pagados (por la
    hora en que se confirmó el pago), del más nuevo al más viejo, con cuántas veces se imprimió."""

    permission_classes = [IsAuthenticated, EsSupervisorOAdmin]

    @staticmethod
    def _factura(cobro) -> str:
        factura = getattr(cobro, "factura", None)
        if factura is None:
            return "sin factura"
        if factura.estado_envio == Factura.EstadoEnvio.ANULADO:
            return f"{factura.numero_factura} (anulada)".strip() if factura.numero_factura else "Anulada"
        return factura.numero_factura or factura.get_estado_envio_display()

    @staticmethod
    def _cliente(deuda) -> dict:
        return {"codigo": deuda.cliente.codigo_externo, "nombre": deuda.cliente.nombre}

    def get(self, request):
        texto = request.query_params.get("fecha")
        try:
            fecha = datetime.strptime(texto, "%Y-%m-%d").date() if texto else timezone.localdate()
        except ValueError:
            return Response({"detail": "Fecha inválida (AAAA-MM-DD)."}, status=status.HTTP_400_BAD_REQUEST)

        filas = []
        efectivos = (
            CobroEfectivo.objects.filter(creado_en__date=fecha, grupo__isnull=True)
            .select_related("usuario", "deuda__cliente", "factura")
        )
        for c in efectivos:
            filas.append({
                "tipo": "efectivo", "id": c.pk, "numero": f"COMP-EF-{c.pk}", "fecha": c.creado_en,
                "forma_pago": _forma_pago(c), "cajero": c.usuario.username, "monto": c.monto_snapshot,
                "clientes": [self._cliente(c.deuda)], "facturas": [self._factura(c)],
            })
        grupos = CobroAgrupado.objects.filter(creado_en__date=fecha).select_related("usuario").prefetch_related(
            "cobros__deuda__cliente", "cobros__factura"
        )
        for g in grupos:
            cobros = sorted(g.cobros.all(), key=lambda c: c.pk)  # en el orden en que se cobraron
            filas.append({
                "tipo": "grupo", "id": g.pk, "numero": f"COMP-GR-{g.pk}", "fecha": g.creado_en,
                "forma_pago": _forma_pago(cobros[0]) if cobros else "Efectivo", "cajero": g.usuario.username, "monto": g.monto_total,
                "clientes": [self._cliente(c.deuda) for c in cobros], "facturas": [self._factura(c) for c in cobros],
            })
        qrs = TransaccionQR.objects.filter(
            estado=TransaccionQR.Estado.PAGADO, actualizado_en__date=fecha
        ).select_related("usuario", "deuda__cliente", "factura")
        for t in qrs:
            filas.append({
                "tipo": "qr", "id": t.pk, "numero": f"COMP-QR-{t.pk}", "fecha": t.actualizado_en,
                "forma_pago": "QR", "cajero": t.usuario.username, "monto": t.monto_snapshot,
                "clientes": [self._cliente(t.deuda)], "facturas": [self._factura(t)],
            })

        impresiones = {}
        for imp in ImpresionComprobante.objects.filter(
            Q(tipo="efectivo", objeto_id__in=[f["id"] for f in filas if f["tipo"] == "efectivo"])
            | Q(tipo="grupo", objeto_id__in=[f["id"] for f in filas if f["tipo"] == "grupo"])
            | Q(tipo="qr", objeto_id__in=[f["id"] for f in filas if f["tipo"] == "qr"])
        ).order_by("creado_en"):
            cantidad, _ = impresiones.get((imp.tipo, imp.objeto_id), (0, None))
            impresiones[(imp.tipo, imp.objeto_id)] = (cantidad + 1, imp.creado_en)

        anuladas = {}
        for factura in Factura.objects.filter(
            Q(cobro_efectivo__creado_en__date=fecha) | Q(transaccion_qr__actualizado_en__date=fecha),
            estado_envio=Factura.EstadoEnvio.ANULADO,
        ).select_related("anulada_por", "cobro_efectivo"):
            if factura.transaccion_qr_id:
                clave = ("qr", factura.transaccion_qr_id)
            elif factura.cobro_efectivo.grupo_id:
                clave = ("grupo", factura.cobro_efectivo.grupo_id)
            else:
                clave = ("efectivo", factura.cobro_efectivo_id)
            anuladas[clave] = {
                "por": factura.anulada_por.username if factura.anulada_por else "",
                "en": factura.anulada_en,
                "motivo": factura.motivo_anulacion,
            }

        hoy = timezone.localdate()
        for f in filas:
            f["impresiones"], f["ultima_impresion"] = impresiones.get((f["tipo"], f["id"]), (0, None))
            f["anulacion"] = anuladas.get((f["tipo"], f["id"]))
            f["se_puede_anular"] = f["anulacion"] is None and fecha == hoy
            f["monto"] = f"{f['monto']:.2f}"
        filas.sort(key=lambda f: f["fecha"], reverse=True)
        return Response({"fecha": fecha.isoformat(), "comprobantes": filas})


class ProformaView(APIView):
    """`GET /api/proformas/<numero>/?anio=&mes=`: clientes de la proforma con su deuda hasta ese mes
    (ver apps/cobranza/proformas.py). Para sumarlos al cobro de varios clientes."""

    permission_classes = [IsAuthenticated]

    def get(self, request, numero):
        hoy = timezone.localdate()
        try:
            anio = int(request.query_params.get("anio") or hoy.year)
            mes = int(request.query_params.get("mes") or hoy.month)
            return Response(proformas.consultar(request.user, int(numero), anio, mes))
        except ValueError:
            return Response({"detail": "Año o mes inválido."}, status=status.HTTP_400_BAD_REQUEST)
        except proformas.ProformaError as exc:
            return Response({"detail": str(exc)}, status=exc.status)
