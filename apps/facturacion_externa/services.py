"""Orquestación de la liquidación de un SolicitudLiquidacion contra
api-cobranzas-bancos -- mismo flujo que `FacturacionRecibo::procesar()` de
cessa-laravel: asegurar Caja abierta -> crear Transacción (una sola vez,
reutilizando `cobranzas_uuid` si ya existe de un intento anterior) -> pagar
-> descargar comprobante. El dinero ya se cobró antes de llegar acá -- si
falla, la solicitud queda en ERROR con el motivo guardado, nunca vuelve a
PENDIENTE en silencio."""
from __future__ import annotations

import hashlib
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from services.cobranzas_banco_client import (
    CobranzasBancoError,
    CobranzasBancoRequestError,
    construir_detalle,
    construir_documento,
    get_cobranzas_banco_client,
)

from services.deuda_client import DeudaClientError, consultar_cliente_via_cobranzas

from .models import SolicitudLiquidacion

# Prefijo del error cuando la deuda cambió entre que cessa-laravel generó el QR y el pago
# (ver _motivo_deuda_cambiada). cessa-laravel puede reconocerlo para pedir regenerar el QR.
MENSAJE_DEUDA_CAMBIADA = "La deuda cambió desde que se generó el QR"

# Lo que identifica un comprobante en el SIIC (mismos campos con los que lo busca al pagar).
_CAMPOS_CLAVE_COMPROBANTE = (
    "codigo_sucursal", "nro_comprobante", "nro_suministro", "fecha", "tipo", "letra_comprobante", "nro_autorizacion",
)

# Texto exacto que devuelve api-cobranzas-bancos cuando /pagar-otro-documento se reintenta sobre
# una transacción que ya se pagó con éxito en un intento anterior (ver hallazgo de sesión
# 2026-09-22: el POST de cessa-laravel se cae en la RESPUESTA -- ej. timeout de Cloudflare entre
# test01.cessa.com.bo y este server -- pero el pago ya se había procesado acá; el reintento
# automático que sigue entonces choca con este mensaje en vez de encontrar éxito). No hay un
# código HTTP/campo separado para distinguir este caso del resto de los rechazos de negocio, así
# que se matchea el texto tal cual lo manda la API real -- mismo criterio que ya usa el resto de
# esta integración (y CessaApiService del lado de cessa-laravel) para reconocer rechazos
# específicos de estos sistemas legacy.
_MENSAJE_YA_PAGADA = 'ya ha sido pagada'

# api-cobranzas-bancos rechaza pagar una Transacción creada otro día ("La transacción ha expirado,
# ésta ha sido creada en fecha y hora ..." -- TransaccionController@pagarOtroDocumento, se valida
# antes que el estado). Un reintento al día siguiente de un intento fallido necesita una nueva.
_MENSAJE_TRANSACCION_EXPIRADA = 'ha expirado'

# api-cobranzas-bancos marca FALLIDA la Transacción apenas el SIIC rechaza un pago (por cualquier
# motivo: deuda ya pagada, caja fuera de horario, timeout...) y después rechaza todo reintento sobre
# ella con este texto, ANTES de volver a preguntarle al SIIC (TransaccionController@
# pagarOtroDocumento, chequeo de estado). Antes se reutilizaba esa misma Transacción para siempre:
# cada reintento chocaba acá, el motivo real del rechazo quedaba tapado y un problema pasajero
# nunca se recuperaba. FALLIDA = el SIIC deshizo todo el lote (una sola transacción DB2), nada quedó
# pagado con ella, así que se crea otra y se reintenta. No hay riesgo de doble cobro: si la deuda
# igual figura pagada, el SIIC rechaza con "ya ha sido pagada" (COMPLOC=999).
_MENSAJE_TRANSACCION_FALLIDA = 'ha fallado previamente'

# Largo máximo de `documento.numero` en el SIIC (`'documento.numero' => 'required|max:20'` en
# CajaController@pagarOtroDocumento, columna TCNN3051.CNN6NRO).
_MAX_NUMERO_DOCUMENTO = 20


def numero_documento_para(solicitud: SolicitudLiquidacion) -> str:
    """Número de documento (referencia del cobro) que se registra en el SIIC. Usa el número de
    orden del sistema de origen o el alias si entran en 20 caracteres; si no (ej. los alias
    `CESSA-SIM-20260922125619-MSGC` de cessa-laravel, 29), deriva uno estable del alias -- el mismo
    en cada reintento, rastreable desde el admin porque siempre se puede recalcular."""
    for candidato in (solicitud.numero_orden_originante, solicitud.alias):
        if candidato and len(candidato) <= _MAX_NUMERO_DOCUMENTO:
            return candidato
    return "CW" + hashlib.sha256(solicitud.alias.encode()).hexdigest()[: _MAX_NUMERO_DOCUMENTO - 2].upper()


def banco_id_para(solicitud: SolicitudLiquidacion) -> str:
    """banco_id del documento según el banco por el que entró la plata (`solicitud.banco`); si no
    vino o no hay uno configurado para ese banco, el COBRANZAS_BANCO_DOCUMENTO_BANCO_ID de siempre."""
    return (
        settings.COBRANZAS_BANCO_DOCUMENTO_BANCO_IDS.get(solicitud.banco or "")
        or settings.COBRANZAS_BANCO_DOCUMENTO_BANCO_ID
    )


def liquidar_solicitud(solicitud: SolicitudLiquidacion) -> SolicitudLiquidacion:
    if solicitud.estado == SolicitudLiquidacion.Estado.FACTURADO:
        return solicitud  # ya liquidada -- idempotente, no se vuelve a pagar.

    if not settings.COBRANZAS_BANCO_DOCUMENTO_ENTE_ID or not settings.COBRANZAS_BANCO_DOCUMENTO_BANCO_ID:
        _marcar_error(
            solicitud,
            "Falta configurar COBRANZAS_BANCO_DOCUMENTO_ENTE_ID/COBRANZAS_BANCO_DOCUMENTO_BANCO_ID "
            "(catálogos GET /v1/entes y GET /v1/bancos de api-cobranzas-bancos) -- sin esto no se "
            "puede armar el documento que exige /pagar-otro-documento.",
        )
        return solicitud

    cliente = get_cobranzas_banco_client()

    try:
        motivo_monto = _motivo_monto_distinto(solicitud)
        if motivo_monto:
            _marcar_error(solicitud, motivo_monto)
            return solicitud

        if _hay_que_verificar_deuda(cliente, solicitud):
            motivo = _motivo_deuda_cambiada(solicitud)
            if motivo:
                _marcar_error(solicitud, motivo)
                return solicitud

        cliente.asegurar_caja_abierta()

        uuid = solicitud.cobranzas_uuid or cliente.crear_transaccion()
        if not solicitud.cobranzas_uuid:
            solicitud.cobranzas_uuid = uuid
            solicitud.save(update_fields=["cobranzas_uuid"])

        detalle = construir_detalle(solicitud.detalle, nro_cliente_fallback=solicitud.nro_cliente)
        documento = construir_documento(
            nro_cliente=solicitud.nro_cliente,
            monto=solicitud.monto,
            moneda=solicitud.moneda,
            numero_documento=numero_documento_para(solicitud),
            fecha_pago=solicitud.fecha_pago,
            banco_id=banco_id_para(solicitud),
        )
        def pagar(uuid_actual: str) -> str:
            """Paga con la Transacción dada; si quedó FALLIDA de un intento anterior, crea otra y
            paga con esa (ver _MENSAJE_TRANSACCION_FALLIDA). Devuelve el uuid con el que se pagó."""
            try:
                cliente.pagar_transaccion(uuid_actual, detalle, documento)
                return uuid_actual
            except CobranzasBancoRequestError as exc:
                if _MENSAJE_TRANSACCION_FALLIDA not in str(exc):
                    raise
            nuevo = cliente.crear_transaccion()
            solicitud.cobranzas_uuid = nuevo
            solicitud.save(update_fields=["cobranzas_uuid"])
            cliente.pagar_transaccion(nuevo, detalle, documento)
            return nuevo

        try:
            uuid = pagar(uuid)
        except CobranzasBancoRequestError as exc:
            # Si pagar() creó una Transacción nueva, los chequeos de abajo son sobre esa.
            uuid = solicitud.cobranzas_uuid
            if _MENSAJE_TRANSACCION_EXPIRADA in str(exc):
                # Transacción de un día anterior (api-cobranzas-bancos valida la fecha antes que
                # el estado, así que no dice si ya estaba pagada). Se pregunta el estado real:
                estado = cliente.obtener_estado_transaccion(uuid)
                if estado == "EN_TRANSACCION":
                    raise CobranzasBancoRequestError(
                        f"La transacción {uuid} quedó EN_TRANSACCION (pago a medio procesar) -- "
                        "revisar a mano en api-cobranzas-bancos antes de reintentar."
                    ) from exc
                if estado != "PAGADA":
                    # Nunca se pagó: se crea una nueva y se paga con esa. Si la deuda igual figura
                    # pagada en el SIIC, rechaza con "ya ha sido pagada" -- nunca hay doble cobro.
                    uuid = cliente.crear_transaccion()
                    solicitud.cobranzas_uuid = uuid
                    solicitud.save(update_fields=["cobranzas_uuid"])
                    cliente.pagar_transaccion(uuid, detalle, documento)
                # PAGADA: ya se pagó otro día (falló después, al traer el comprobante) -- se sigue
                # directo al comprobante con esa misma transacción.
            elif _MENSAJE_YA_PAGADA not in str(exc):
                raise
            # "Ya ha sido pagada": solo es un éxito recuperable si fue ESTA transacción la que pagó
            # en un intento anterior (ver _MENSAJE_YA_PAGADA arriba). Si la transacción no quedó
            # PAGADA, la deuda la pagó otro canal/otra transacción -- es un rechazo real, y se
            # informa tal cual en vez de fallar después con "estado FALLIDA" al pedir el comprobante.
            elif cliente.obtener_estado_transaccion(uuid) not in ("PAGADA", ""):
                raise CobranzasBancoRequestError(
                    f"El SIIC rechazó el pago: alguno de los comprobantes ya figura pagado por otro "
                    f"medio (no por esta liquidación). Revisar la deuda del cliente. Detalle: {exc}"
                ) from exc

        comprobante = cliente.obtener_comprobante_pdf(uuid)

        solicitud.estado = SolicitudLiquidacion.Estado.FACTURADO
        solicitud.comprobante_pdf = comprobante
        solicitud.procesado_en = timezone.now()
        solicitud.error = ""
        solicitud.save(update_fields=["estado", "comprobante_pdf", "procesado_en", "error"])
    except CobranzasBancoError as exc:
        _marcar_error(solicitud, str(exc))
    except Exception as exc:  # noqa: BLE001 -- mismo criterio que FacturacionRecibo::procesar():
        # el dinero ya se cobró, una excepción inesperada acá (red, config,
        # contrato cambiado del lado de api-cobranzas-bancos) nunca debe
        # perderse en un 500 sin rastro.
        _marcar_error(solicitud, f"Error inesperado: {exc}")

    return solicitud


def _clave_comprobante(item: dict) -> tuple:
    def normalizar(campo):
        valor = str(item.get(campo) or "").strip()
        return "".join(c for c in valor if c.isdigit()) if campo == "fecha" else valor

    return tuple(normalizar(campo) for campo in _CAMPOS_CLAVE_COMPROBANTE)


def _motivo_monto_distinto(solicitud: SolicitudLiquidacion) -> str | None:
    """El monto cobrado (va al documento del pago en el SIIC) tiene que ser la suma de los
    comprobantes que se pagan, con las notas de crédito restando (el signo lo da
    `debito_credito`, mismo criterio que la deuda). Si no, el SIIC registraría un cobro por
    un monto y comprobantes por otro. Verificado contra las 32 solicitudes reales al
    2026-10-08: todas coinciden."""
    suma = Decimal("0")
    for item in solicitud.detalle or []:
        magnitud = abs(Decimal(str(item.get("importe") or 0)))
        suma += -magnitud if str(item.get("debito_credito", "")).upper() == "CREDITO" else magnitud
    suma = suma.quantize(Decimal("0.01"))
    if suma != solicitud.monto:
        return (
            f"El monto cobrado (Bs. {solicitud.monto}) no coincide con la suma de los comprobantes "
            f"(Bs. {suma}); no se pagó nada. Revisar el recibo en cessa-laravel."
        )
    return None


def _hay_que_verificar_deuda(cliente, solicitud: SolicitudLiquidacion) -> bool:
    """Siempre en el primer intento. En un reintento con transacción ya PAGADA no: los
    comprobantes figuran pagados justamente por esta liquidación (falló después, al traer el
    comprobante) y verificarlos la bloquearía."""
    if not solicitud.cobranzas_uuid:
        return True
    try:
        return cliente.obtener_estado_transaccion(solicitud.cobranzas_uuid) != "PAGADA"
    except CobranzasBancoError:
        return True


def _motivo_deuda_cambiada(solicitud: SolicitudLiquidacion) -> str | None:
    """Vuelve a consultar la deuda (como banco, en el mismo SIIC donde se va a pagar) y
    compara con los comprobantes que manda cessa-laravel. Devuelve el motivo si cambió, o
    None si sigue igual o no se pudo consultar (en ese caso decide el SIIC al pagar).

    El SIIC paga un comprobante solo si sigue pendiente y es el más antiguo (vencimiento,
    tipo), y la consulta ya los trae en ese orden: lo que se paga tiene que ser exactamente
    los N primeros pendientes. Si no, el pago fallaría igual, pero dejando una transacción
    FALLIDA en api-cobranzas y un mensaje del SIIC difícil de interpretar."""
    try:
        status, body = consultar_cliente_via_cobranzas({"nro_cliente": solicitud.nro_cliente, "ver_deuda": "si"})
    except DeudaClientError:
        return None
    if status == 404 or body.get("error") or not body.get("nro_cliente"):
        return f"{MENSAJE_DEUDA_CAMBIADA}: el cliente {solicitud.nro_cliente} no figura en el SIIC."

    pendientes = body.get("deuda") or []
    por_clave = {_clave_comprobante(item): item for item in pendientes}
    enviados = list(solicitud.detalle or [])

    ya_no_pendientes = [d for d in enviados if _clave_comprobante(d) not in por_clave]
    if ya_no_pendientes:
        lista = ", ".join(f"N° {d.get('nro_comprobante')} ({d.get('detalle', '').strip()})" for d in ya_no_pendientes)
        return f"{MENSAJE_DEUDA_CAMBIADA}: ya no están pendientes {lista} (pagados por otro medio o anulados)."

    importes_distintos = [
        d for d in enviados
        if abs(Decimal(str(d.get("importe") or 0))) != abs(Decimal(str(por_clave[_clave_comprobante(d)].get("importe") or 0)))
    ]
    if importes_distintos:
        lista = ", ".join(f"N° {d.get('nro_comprobante')}" for d in importes_distintos)
        return f"{MENSAJE_DEUDA_CAMBIADA}: cambió el importe de {lista}."

    mas_antiguos = {_clave_comprobante(p) for p in pendientes[: len(enviados)]}
    if mas_antiguos != {_clave_comprobante(d) for d in enviados}:
        faltan = [p for p in pendientes[: len(enviados)] if _clave_comprobante(p) not in {_clave_comprobante(d) for d in enviados}]
        lista = ", ".join(f"N° {p.get('nro_comprobante')} ({str(p.get('detalle', '')).strip()})" for p in faltan)
        return (
            f"{MENSAJE_DEUDA_CAMBIADA}: hay comprobantes más antiguos pendientes que no están en el pago "
            f"({lista}); el SIIC exige pagar del más antiguo al más nuevo."
        )
    return None


def _marcar_error(solicitud: SolicitudLiquidacion, motivo: str) -> None:
    solicitud.estado = SolicitudLiquidacion.Estado.ERROR
    solicitud.intentos += 1
    solicitud.error = motivo
    solicitud.save(update_fields=["estado", "intentos", "error"])
