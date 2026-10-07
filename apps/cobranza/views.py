import csv
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

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
from services.deuda_client import ClienteNoEncontradoError, DeudaClientError, get_deuda_client

from .models import (
    AperturaCajaFueraDeHorario,
    Caja,
    CajaOperacionInvalida,
    CobroAgrupado,
    CobroEfectivo,
    Deuda,
    Factura,
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

        try:
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

        try:
            cobro = registrar_cobro_efectivo(
                deuda=deuda,
                usuario=request.user,
                monto_recibido=monto_recibido,
                monto_a_cobrar=monto_a_cobrar,
                cantidad_comprobantes=cantidad,
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
        if self.action in ("reabrir", "aperturas_fuera_de_horario", "resumen"):
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
            caja = Caja.abrir(cajero=cajero, usuario=usuario, motivo=entrada.validated_data["motivo"])
        except CajaOperacionInvalida as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(CajaSerializer(caja).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cerrar(self, request, pk=None):
        caja = self.get_object()
        try:
            caja.cerrar(usuario=request.user)
        except CajaOperacionInvalida as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(CajaSerializer(caja).data)

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
            grupo = registrar_cobro_efectivo_agrupado(
                usuario=request.user, monto_recibido=datos["monto_recibido"], selecciones=selecciones
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
