"""Orquestación de la liquidación de un SolicitudLiquidacion contra
api-cobranzas-bancos -- mismo flujo que `FacturacionRecibo::procesar()` de
cessa-laravel: asegurar Caja abierta -> crear Transacción (una sola vez,
reutilizando `cobranzas_uuid` si ya existe de un intento anterior) -> pagar
-> descargar comprobante. El dinero ya se cobró antes de llegar acá -- si
falla, la solicitud queda en ERROR con el motivo guardado, nunca vuelve a
PENDIENTE en silencio."""
from __future__ import annotations

import hashlib

from django.conf import settings
from django.utils import timezone

from services.cobranzas_banco_client import (
    CobranzasBancoError,
    CobranzasBancoRequestError,
    construir_detalle,
    construir_documento,
    get_cobranzas_banco_client,
)

from .models import SolicitudLiquidacion

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
        )
        try:
            cliente.pagar_transaccion(uuid, detalle, documento)
        except CobranzasBancoRequestError as exc:
            if _MENSAJE_TRANSACCION_EXPIRADA in str(exc):
                # Transacción de un día anterior: se crea una nueva y se paga con esa. Si la vieja
                # en realidad sí se había pagado (respuesta perdida), el SIIC rechaza esta con
                # "la deuda ya ha sido pagada" -- nunca hay doble cobro.
                uuid = cliente.crear_transaccion()
                solicitud.cobranzas_uuid = uuid
                solicitud.save(update_fields=["cobranzas_uuid"])
                cliente.pagar_transaccion(uuid, detalle, documento)
            # Ya se pagó en un intento anterior (ver _MENSAJE_YA_PAGADA arriba) -- no es un
            # rechazo real, solo falta completar el paso que sigue (traer el comprobante).
            elif _MENSAJE_YA_PAGADA not in str(exc):
                raise

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


def _marcar_error(solicitud: SolicitudLiquidacion, motivo: str) -> None:
    solicitud.estado = SolicitudLiquidacion.Estado.ERROR
    solicitud.intentos += 1
    solicitud.error = motivo
    solicitud.save(update_fields=["estado", "intentos", "error"])
