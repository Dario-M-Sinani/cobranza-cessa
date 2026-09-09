"""Orquestación de dominio para cobranza: combina los modelos con los
clientes externos de `services/` (MC4/SIP y consulta de deuda). Las vistas
DRF llaman a estas funciones, nunca a `services.mc4_client` directamente."""
from __future__ import annotations

from decimal import Decimal
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from apps.auditoria.models import LogAuditoria
from services.cobranzas_banco_client import CobranzasBancoError, construir_detalle, get_cobranzas_banco_client
from services.mc4_client import EstadoPagoMC4, MC4ClientError, SolicitudQR, get_mc4_client

from .models import Caja, CobroEfectivo, Deuda, Factura, TransaccionQR

QR_VENCIMIENTO_DIAS = 1  # SIP solo acepta fecha (no hora) de vencimiento.

# Tope de reintentos automáticos (tarea periódica) antes de dejar de insistir
# solo y requerir revisión manual -- mismo criterio y mismo número que
# FacturacionRecibo::MAX_INTENTOS_AUTOMATICOS del lado cessa-laravel. El
# reintento manual (endpoint "reintentar_facturacion") no tiene este tope.
MAX_INTENTOS_AUTOMATICOS_FACTURACION = 5


class ErrorGeneracionQR(Exception):
    """La pasarela MC4/SIP no pudo generar el QR."""


class TransaccionEnCursoError(Exception):
    """El cliente ya tiene una TransaccionQR generada/pendiente sin resolver."""


class ErrorFacturacion(Exception):
    """No se puede facturar la transacción en su estado actual."""


class MontoRecibidoInsuficienteError(Exception):
    """El monto recibido en efectivo es menor a la deuda que se está cobrando."""


class DeudaSinSaldoError(Exception):
    """La deuda consultada no tiene saldo pendiente (monto <= 0) -- no hay
    nada que cobrar por QR ni en efectivo."""


class MontoInvalidoError(Exception):
    """El monto a cobrar (adelanto) no es válido: tiene que ser mayor a 0 y
    no puede superar el total de la deuda consultada."""


def _alias(transaccion: TransaccionQR) -> str:
    return f"txqr-{transaccion.pk}"


def _validar_monto_a_cobrar(deuda: Deuda, monto: Decimal | None) -> Decimal:
    """Resuelve y valida el monto a cobrar: por defecto (sin adelanto) es el
    total de la deuda; si se pide un adelanto, tiene que ser positivo y no
    superar ese total -- este sistema no lleva un saldo vivo propio, solo
    registra localmente cuánto se cobró en este momento (la próxima consulta
    de deuda vuelve a traer lo que diga SIIC, este cobro no se le informa)."""
    if deuda.monto <= 0:
        raise DeudaSinSaldoError(f"El cliente {deuda.cliente} no tiene deuda pendiente para cobrar.")

    if monto is None:
        return deuda.monto

    if monto <= 0 or monto > deuda.monto:
        raise MontoInvalidoError(
            f"El monto a cobrar (Bs. {monto}) tiene que ser mayor a 0 y no puede superar "
            f"la deuda total (Bs. {deuda.monto})."
        )
    return monto


@transaction.atomic
def generar_transaccion_qr(deuda: Deuda, usuario, monto: Decimal | None = None) -> TransaccionQR:
    """Crea la TransaccionQR y genera el QR contra MC4/SIP. Si la pasarela
    falla, la transacción queda registrada en ERROR (nunca a medio crear).

    `monto`: adelanto opcional -- si no se manda, se cobra la deuda
    completa (comportamiento de siempre). Se vincula a la Caja abierta del
    cajero si tiene una (igual que `registrar_cobro_efectivo`), para que el
    resumen de cierre de caja (`reportes.resumen_caja`) incluya también los
    QR generados en el turno, no solo el efectivo."""
    monto_a_cobrar = _validar_monto_a_cobrar(deuda, monto)

    ya_en_curso = TransaccionQR.objects.filter(
        deuda__cliente=deuda.cliente,
        estado__in=[TransaccionQR.Estado.GENERADO, TransaccionQR.Estado.PENDIENTE_CONFIRMACION],
    ).exists()
    if ya_en_curso:
        raise TransaccionEnCursoError(f"El cliente {deuda.cliente} ya tiene un QR de cobro pendiente.")

    caja_abierta = Caja.objects.filter(cajero=usuario, estado=Caja.Estado.ABIERTA).first()
    transaccion = TransaccionQR.objects.create(
        deuda=deuda, usuario=usuario, monto_snapshot=monto_a_cobrar, caja=caja_abierta
    )

    solicitud = SolicitudQR(
        alias=_alias(transaccion),
        monto=transaccion.monto_snapshot,
        moneda="BOB",
        descripcion=f"Cobranza CESSA - {deuda.cliente.nombre}",
        fecha_vencimiento=date.today() + timedelta(days=QR_VENCIMIENTO_DIAS),
    )

    try:
        resultado = get_mc4_client().generar_qr(solicitud)
    except MC4ClientError as exc:
        transaccion.transicionar_estado(TransaccionQR.Estado.ERROR, usuario=usuario)
        raise ErrorGeneracionQR(str(exc)) from exc

    transaccion.id_operacion_mc4 = resultado.id_transaccion
    transaccion.save(update_fields=["id_operacion_mc4"])
    transaccion.transicionar_estado(TransaccionQR.Estado.PENDIENTE_CONFIRMACION, usuario=usuario)

    # MC4 solo devuelve la imagen del QR en este momento (generación); no hay
    # forma de volver a pedirla después, y no se persiste -- es de un solo
    # uso. Queda como atributo transitorio para que el serializer la incluya
    # únicamente en la respuesta de esta llamada.
    transaccion.imagen_qr_base64 = resultado.imagen_qr_base64

    return transaccion


# Traduce el estado remoto de MC4/SIP al estado local de TransaccionQR.
# "INHABILITADO" mapea a CANCELADO: en SIP significa que el QR fue dado de
# baja antes de cobrarse, lo más cercano semánticamente a una cancelación.
_MAPA_ESTADOS_MC4 = {
    EstadoPagoMC4.PAGADO: TransaccionQR.Estado.PAGADO,
    EstadoPagoMC4.EXPIRADO: TransaccionQR.Estado.VENCIDO,
    EstadoPagoMC4.INHABILITADO: TransaccionQR.Estado.CANCELADO,
    EstadoPagoMC4.ERROR: TransaccionQR.Estado.ERROR,
}


@transaction.atomic
def verificar_pago(transaccion: TransaccionQR) -> TransaccionQR:
    """Consulta el estado real en MC4/SIP de una transacción pendiente y
    aplica la transición correspondiente. Si queda PAGADO, crea la Factura
    en la misma transacción atómica -- pago y facturación nunca quedan
    desincronizados por una falla a mitad de camino."""
    if transaccion.estado != TransaccionQR.Estado.PENDIENTE_CONFIRMACION:
        return transaccion

    try:
        estado_remoto = get_mc4_client().consultar_estado(alias=_alias(transaccion))
    except MC4ClientError:
        return transaccion  # se reintenta en la siguiente corrida periódica

    nuevo_estado = _MAPA_ESTADOS_MC4.get(estado_remoto.estado)
    if nuevo_estado is None:
        return transaccion  # sigue pendiente del lado de MC4

    transaccion.transicionar_estado(nuevo_estado)

    if nuevo_estado == TransaccionQR.Estado.PAGADO:
        crear_factura(transaccion)

    return transaccion


def crear_factura(transaccion: TransaccionQR) -> Factura:
    factura, creada = Factura.objects.get_or_create(transaccion_qr=transaccion)
    if creada:
        LogAuditoria.objects.create(
            accion="Factura: creada tras confirmación de pago",
            entidad_afectada=f"Factura:{factura.pk}",
        )
    return factura


@transaction.atomic
def registrar_cobro_efectivo(
    deuda: Deuda, usuario, monto_recibido: Decimal, monto_a_cobrar: Decimal | None = None
) -> CobroEfectivo:
    """Registra un cobro en efectivo. A diferencia del QR no depende de una
    pasarela externa: se confirma al instante, así que la Factura se crea
    en la misma transacción atómica (nunca queda un cobro sin su Factura
    por una falla a mitad de camino).

    `monto_a_cobrar`: adelanto opcional -- si no se manda, se cobra la deuda
    completa (comportamiento de siempre) y `monto_recibido` debe cubrirla
    (el excedente es vuelto). Con adelanto, `monto_recibido` debe cubrir ese
    monto parcial, no la deuda total.

    Se vincula a la Caja abierta del cajero si tiene una (Épica E) -- no es
    obligatorio todavía: si el cajero no abrió caja, el cobro igual se
    registra con `caja=None`. Endurecer esto a "obligatorio" queda como
    decisión pendiente de negocio, no técnica."""
    monto_final = _validar_monto_a_cobrar(deuda, monto_a_cobrar)

    ya_en_curso = TransaccionQR.objects.filter(
        deuda__cliente=deuda.cliente,
        estado__in=[TransaccionQR.Estado.GENERADO, TransaccionQR.Estado.PENDIENTE_CONFIRMACION],
    ).exists()
    if ya_en_curso:
        raise TransaccionEnCursoError(f"El cliente {deuda.cliente} ya tiene un QR de cobro pendiente.")

    if monto_recibido < monto_final:
        raise MontoRecibidoInsuficienteError(
            f"El monto recibido (Bs. {monto_recibido}) es menor al monto a cobrar (Bs. {monto_final})."
        )

    caja_abierta = Caja.objects.filter(cajero=usuario, estado=Caja.Estado.ABIERTA).first()

    cobro = CobroEfectivo.objects.create(
        deuda=deuda,
        usuario=usuario,
        caja=caja_abierta,
        monto_snapshot=monto_final,
        monto_recibido=monto_recibido,
        vuelto=monto_recibido - monto_final,
    )
    LogAuditoria.objects.create(
        usuario=usuario, accion="CobroEfectivo: registrado", entidad_afectada=f"CobroEfectivo:{cobro.pk}"
    )

    factura, creada = Factura.objects.get_or_create(cobro_efectivo=cobro)
    if creada:
        LogAuditoria.objects.create(
            accion="Factura: creada tras cobro en efectivo", entidad_afectada=f"Factura:{factura.pk}"
        )

    return cobro


def reintentar_facturacion(transaccion: TransaccionQR) -> Factura:
    """Usada por supervisor/admin cuando una Factura quedó en error o nunca
    se creó pese a que la transacción sí está pagada. A diferencia del
    reintento automático (tarea periódica, con tope de intentos), este
    intenta enviar de inmediato -- mismo criterio que el botón "Reintentar
    Facturación" de cessa-laravel: se puede usar las veces que hagan falta."""
    if transaccion.estado != TransaccionQR.Estado.PAGADO:
        raise ErrorFacturacion("Solo se puede (re)facturar una transacción en estado 'pagado'.")

    factura = crear_factura(transaccion)
    return enviar_factura_a_siic(factura)


def enviar_factura_a_siic(factura: Factura) -> Factura:
    """Registra una Factura ya creada (cobro en efectivo o QR ya confirmado)
    como factura real en api-cobranzas-bancos, usando la Caja/Cajero
    compartido de este panel (ver services/cobranzas_banco_client.py):
    asegurar Caja abierta -> crear Transacción (reutilizando `cobranzas_uuid`
    si ya existe de un intento previo) -> pagar con `/pagar` a secas (el
    dinero entró por esta misma Caja, no por un canal externo como el QR web
    de cessa-laravel -- ver `pagar_transaccion_propia`) -> guardar
    comprobante.

    El cobro ya se confirmó antes de llegar acá -- si esto falla, la
    Factura queda en ERROR con el motivo, nunca se pierde el hecho de que
    el cliente ya pagó."""
    if factura.estado_envio == Factura.EstadoEnvio.ENVIADO:
        return factura  # ya se envió -- idempotente, no se vuelve a pagar.

    deuda = factura.origen.deuda
    if not deuda.items_snapshot:
        _marcar_error_envio(
            factura,
            "La deuda no tiene guardado el detalle de SIIC (items_snapshot) -- no se puede "
            "facturar. Revisar manualmente contra SIIC.",
        )
        return factura

    cliente = get_cobranzas_banco_client()

    try:
        cliente.asegurar_caja_abierta()

        uuid = factura.cobranzas_uuid or cliente.crear_transaccion()
        if not factura.cobranzas_uuid:
            factura.cobranzas_uuid = uuid
            factura.save(update_fields=["cobranzas_uuid"])

        detalle = construir_detalle(deuda.items_snapshot, nro_cliente_fallback=deuda.cliente.codigo_externo)
        cliente.pagar_transaccion_propia(uuid, detalle)

        comprobante_pdf = cliente.obtener_comprobante_pdf(uuid)

        numero_factura = ""
        try:
            documento = cliente.obtener_comprobante_json(uuid)
            numero_factura = str(documento.get("nro_factura") or "")
        except CobranzasBancoError:
            pass  # el comprobante en PDF ya se guardó -- el número es solo un extra, no bloquea.

        factura.estado_envio = Factura.EstadoEnvio.ENVIADO
        factura.numero_factura = numero_factura
        factura.comprobante_pdf = comprobante_pdf
        factura.emitida_en = timezone.now()
        factura.error = ""
        factura.save(
            update_fields=["estado_envio", "numero_factura", "comprobante_pdf", "emitida_en", "error"]
        )
        LogAuditoria.objects.create(
            accion="Factura: enviada a api-cobranzas-bancos", entidad_afectada=f"Factura:{factura.pk}"
        )
    except CobranzasBancoError as exc:
        _marcar_error_envio(factura, str(exc))
    except Exception as exc:  # noqa: BLE001 -- el cobro ya se confirmó, una excepción
        # inesperada acá (red caída, config mal puesta, un cambio de contrato del lado de
        # api-cobranzas-bancos) nunca debe perder de vista que el cliente ya pagó.
        _marcar_error_envio(factura, f"Error inesperado: {exc}")

    return factura


def _marcar_error_envio(factura: Factura, motivo: str) -> None:
    factura.estado_envio = Factura.EstadoEnvio.ERROR
    factura.intentos += 1
    factura.error = motivo
    factura.save(update_fields=["estado_envio", "intentos", "error"])
    LogAuditoria.objects.create(
        accion=f"Factura: error de facturación -- {motivo}"[:255],
        entidad_afectada=f"Factura:{factura.pk}",
    )
