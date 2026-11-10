"""Cobro por QR desde las pantallas de autoservicio. Mismas reglas que la web
(`PagoQrController::generar` de cessa-laravel) y el bot (`pago.py`):

- Se pagan siempre los N comprobantes MÁS ANTIGUOS, en el orden en que los devuelve el SIIC
  (el mismo orden con el que `_motivo_deuda_cambiada` verifica al facturar).
- El subtotal tiene que ser > 0 (las conciliaciones restan) y <= Bs 50.000.
- Glosa de 30 caracteres para el banco, con el mismo formato que la web.
- Sin QR entre las 23:59 y las 00:00 (corte diario del sistema de cobros).
- El QR vale KIOSCO_MINUTOS_QR minutos; después se da de baja en SIP.

El pago se detecta por tres caminos, el primero que llegue: el aviso de SIP (sip_callback.py),
la pantalla preguntando por su QR, o la tarea periódica (si la pantalla se cerró). Los tres
terminan en `registrar_pago`, que es idempotente."""
from __future__ import annotations

import logging
import secrets
import string
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.facturacion_externa.alertas import es_de_caja, es_definitivo
from apps.facturacion_externa.models import AvisoSip, SolicitudLiquidacion
from apps.facturacion_externa.panel import _lock_liquidacion
from apps.facturacion_externa.services import liquidar_solicitud
from services.cobranzas_banco_client import CobranzasBancoError, get_cobranzas_banco_client
from services.deuda_client import DeudaClientError, consultar_cliente_via_cobranzas
from services.mc4_client import EstadoPagoMC4, MC4ClientError, SolicitudQR, get_mc4_client

from .models import EnvioFactura, PagoKiosco

logger = logging.getLogger(__name__)

LIMITE_MONTO_QR = Decimal("50000")
SEGUNDOS_ENTRE_CONSULTAS_SIP = 3
MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio",
         "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]


class KioscoError(Exception):
    """Motivo que se muestra tal cual en la pantalla."""


# ---------------------------------------------------------------- deuda

def normalizar_nro_cliente(texto) -> str | None:
    digitos = "".join(c for c in str(texto or "") if c.isdigit())
    return digitos if 1 <= len(digitos) <= 10 else None


def _texto(valor) -> str:
    return str(valor or "").strip()


def _reparar_encoding(valor: str) -> str:
    """Algunos nombres vienen del SIIC con UTF-8 doblemente codificado ("SIÃ\\x91ANI"): mismo
    arreglo que CessaApiService::fixEncoding y el bot. Si no se puede deshacer, ya estaba bien."""
    try:
        return valor.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return valor


def importe_firmado(item: dict) -> Decimal:
    """Mismo criterio que la web y el bot: el signo sale de debito_credito, no de `importe`."""
    try:
        magnitud = abs(Decimal(str(item.get("importe") or 0)))
    except InvalidOperation:
        magnitud = Decimal("0")
    return -magnitud if _texto(item.get("debito_credito")).upper() == "CREDITO" else magnitud


def periodo(item: dict) -> str:
    try:
        return f"{MESES[int(item.get('mes')) - 1]} {int(item.get('anio'))}"
    except (TypeError, ValueError, IndexError):
        return _reparar_encoding(_texto(item.get("detalle")))


def consultar_cliente(nro_cliente: str) -> dict | None:
    """{nro_cliente, nombre, items} como banco (mismo SIIC donde se paga), o None si no existe."""
    try:
        codigo, body = consultar_cliente_via_cobranzas({"nro_cliente": nro_cliente, "ver_deuda": "si"})
    except DeudaClientError as exc:
        logger.error("kiosco.consulta_fallida nro_cliente=%s error=%s", nro_cliente, exc)
        raise KioscoError("No pudimos consultar la deuda en este momento. Intenta de nuevo en unos minutos.") from exc
    if codigo == 404 or body.get("error") or not body.get("nro_cliente"):
        return None
    return {
        "nro_cliente": _texto(body.get("nro_cliente")),
        "nombre": _reparar_encoding(_texto(body.get("nombre"))),
        "items": [i for i in body.get("deuda") or [] if isinstance(i, dict)],
    }


def monto_de(items: list[dict]) -> Decimal:
    return sum((importe_firmado(i) for i in items), Decimal("0")).quantize(Decimal("0.01"))


def cantidades_cobrables(items: list[dict]) -> list[int]:
    """Cada prefijo (los N más antiguos) cobrable: subtotal > 0 y dentro del tope."""
    return [n for n in range(1, len(items) + 1) if Decimal("0") < monto_de(items[:n]) <= LIMITE_MONTO_QR]


def resumen_para_pantalla(cliente: dict) -> dict:
    items = cliente["items"]
    return {
        "nro_cliente": cliente["nro_cliente"],
        "nombre": cliente["nombre"],
        "total": str(monto_de(items)),
        "deuda": [
            {
                "periodo": periodo(i),
                "detalle": _reparar_encoding(_texto(i.get("detalle"))),
                "importe": str(importe_firmado(i)),
            }
            for i in items
        ],
        # La pantalla solo deja elegir "hasta qué mes": cada cantidad es un prefijo de `deuda`.
        "cantidades": [{"cantidad": n, "monto": str(monto_de(items[:n]))} for n in cantidades_cobrables(items)],
    }


# ---------------------------------------------------------------- QR

def glosa(cantidad: int, nro_cliente: str, monto: Decimal) -> str:
    """Mismo formato que la web ("CESSA 2 comp 102500 B278.80"); SIP corta a 30."""
    con_prefijo = f"CESSA {cantidad} comp {nro_cliente} B{monto:.2f}"
    return con_prefijo if len(con_prefijo) <= 30 else f"{cantidad} comp {nro_cliente} B{monto:.2f}"


def nuevo_alias(ahora) -> str:
    sufijo = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    return f"{SolicitudLiquidacion.PREFIJO_KIOSCO}{timezone.localtime(ahora):%Y%m%d%H%M%S}-{sufijo}"


def generar_qr(kiosco: str, nro_cliente: str, cantidad: int) -> tuple[PagoKiosco, str]:
    """Vuelve a consultar la deuda (nunca se confía en lo que manda la pantalla), arma el cobro de
    los `cantidad` comprobantes más antiguos y pide el QR a SIP. Devuelve (pago, imagen base64)."""
    ahora = timezone.now()
    local = timezone.localtime(ahora)
    if local.hour == 23 and local.minute == 59:
        raise KioscoError("Entre las 23:59 y las 00:00 no se pueden generar QR (cierre diario). Intenta en un minuto.")

    cliente = consultar_cliente(nro_cliente)
    if cliente is None:
        raise KioscoError("No encontramos ese número de cliente.")
    items = cliente["items"]
    if cantidad not in cantidades_cobrables(items):
        raise KioscoError("La deuda cambió mientras elegías. Vuelve a consultar.")

    seleccion = items[:cantidad]
    monto = monto_de(seleccion)
    alias = nuevo_alias(ahora)
    texto_glosa = glosa(cantidad, cliente["nro_cliente"], monto)
    try:
        resultado = get_mc4_client().generar_qr(SolicitudQR(
            alias=alias, monto=monto, moneda="BOB", descripcion=texto_glosa,
            fecha_vencimiento=local.date(), callback_url=settings.SIP_CALLBACK_URL,
        ))
    except MC4ClientError as exc:
        logger.error("kiosco.qr_fallido kiosco=%s nro_cliente=%s error=%s", kiosco, nro_cliente, exc)
        raise KioscoError("El banco no respondió al generar el QR. Intenta de nuevo.") from exc

    pago = PagoKiosco.objects.create(
        alias=alias, kiosco=kiosco, nro_cliente=cliente["nro_cliente"], nombre_cliente=cliente["nombre"][:200],
        detalle=[{**i, "importe_firmado": float(importe_firmado(i))} for i in seleccion],
        monto=monto, glosa=texto_glosa, vence_en=ahora + timedelta(minutes=settings.KIOSCO_MINUTOS_QR),
    )
    logger.info("kiosco.qr_generado alias=%s kiosco=%s nro_cliente=%s monto=%s", alias, kiosco, pago.nro_cliente, monto)
    return pago, resultado.imagen_qr_base64


# ---------------------------------------------------------------- estado del pago

def actualizar(pago: PagoKiosco) -> PagoKiosco:
    """Lo que hace la pantalla mientras muestra el QR: consulta a SIP (como mucho cada
    SEGUNDOS_ENTRE_CONSULTAS_SIP) y, si ya venció, lo da de baja."""
    if pago.estado != PagoKiosco.Estado.PENDIENTE:
        return pago
    ahora = timezone.now()
    reciente = pago.consultado_sip_en and (ahora - pago.consultado_sip_en).total_seconds() < SEGUNDOS_ENTRE_CONSULTAS_SIP
    if reciente and ahora < pago.vence_en:
        return pago
    return consultar_sip(pago)


def consultar_sip(pago: PagoKiosco) -> PagoKiosco:
    """Pregunta a SIP por el QR. Pagado → se registra y factura; vencido → se da de baja."""
    try:
        estado = get_mc4_client().consultar_estado(pago.alias)
    except MC4ClientError as exc:
        logger.warning("kiosco.estado_sip_fallido alias=%s error=%s", pago.alias, exc)
        if timezone.now() >= pago.vence_en + timedelta(days=1):
            # SIP no lo reconoce desde hace un día (su QR vence a fin del día): no se consulta más.
            PagoKiosco.objects.filter(pk=pago.pk, estado=PagoKiosco.Estado.PENDIENTE).update(
                estado=PagoKiosco.Estado.VENCIDO
            )
            pago.refresh_from_db()
        return pago
    PagoKiosco.objects.filter(pk=pago.pk).update(consultado_sip_en=timezone.now())

    if estado.estado == EstadoPagoMC4.PAGADO:
        registrar_pago(pago, estado)
    elif pago.estado == PagoKiosco.Estado.PENDIENTE and timezone.now() >= pago.vence_en:
        dar_de_baja(pago, PagoKiosco.Estado.VENCIDO)
    pago.refresh_from_db()
    return pago


def dar_de_baja(pago: PagoKiosco, estado_final: str) -> PagoKiosco:
    """Inhabilita el QR en SIP y, si de verdad nadie pagó, lo cierra. Se vuelve a consultar
    después de inhabilitar: un pago justo en ese instante se registra igual."""
    try:
        get_mc4_client().inhabilitar_qr(pago.alias)
    except MC4ClientError as exc:
        # Si SIP ya lo había dado de baja (o ya está pagado) también responde con error.
        logger.info("kiosco.inhabilitar_fallido alias=%s error=%s", pago.alias, exc)
    try:
        estado = get_mc4_client().consultar_estado(pago.alias)
    except MC4ClientError as exc:
        # Sin confirmación no se cierra: la tarea periódica lo vuelve a intentar.
        logger.warning("kiosco.estado_sip_fallido alias=%s error=%s", pago.alias, exc)
        return pago
    if estado.estado == EstadoPagoMC4.PAGADO:
        registrar_pago(pago, estado)
    else:
        PagoKiosco.objects.filter(pk=pago.pk, estado=PagoKiosco.Estado.PENDIENTE).update(estado=estado_final)
    pago.refresh_from_db()
    return pago


def registrar_pago(pago: PagoKiosco, estado_sip) -> None:
    """Pago confirmado por SIP: crea su SolicitudLiquidacion y factura. Idempotente: si otro
    camino (aviso, pantalla, tarea) ya lo registró, no se crea nada de nuevo. Acepta también un
    QR ya vencido o cancelado: si la plata entró, se factura igual."""
    ahora = timezone.now()
    with transaction.atomic():
        actualizado = PagoKiosco.objects.filter(pk=pago.pk).exclude(estado=PagoKiosco.Estado.PAGADO).update(
            estado=PagoKiosco.Estado.PAGADO, pagado_en=ahora, numero_orden=_texto(estado_sip.numero_orden_originante)[:50],
        )
        if not actualizado:
            return
        pago.refresh_from_db()
        solicitud, _ = SolicitudLiquidacion.objects.get_or_create(alias=pago.alias, defaults={
            "origen": SolicitudLiquidacion.Origen.KIOSCO,
            "nro_cliente": pago.nro_cliente,
            "monto": pago.monto,
            "moneda": "BOB",
            "detalle": pago.detalle,
            "fecha_pago": ahora,
            "numero_orden_originante": pago.numero_orden,
            "banco": "sip_bisa",
        })
        PagoKiosco.objects.filter(pk=pago.pk).update(solicitud=solicitud)
        # Respaldo del pagador para el reporte, por si el aviso de SIP no llega (como el bot).
        if estado_sip.nombre_pagador and not AvisoSip.objects.filter(alias=pago.alias).exists():
            AvisoSip.objects.create(alias=pago.alias, datos={
                "alias": pago.alias,
                "nombreCliente": estado_sip.nombre_pagador,
                "documentoCliente": estado_sip.documento_pagador,
                "numeroOrdenOriginante": pago.numero_orden,
                "_fuente": "kiosco",
            })
    logger.info("kiosco.pagado alias=%s monto=%s", pago.alias, pago.monto)
    facturar(solicitud)


def facturar(solicitud: SolicitudLiquidacion) -> SolicitudLiquidacion:
    """liquidar_solicitud con el mismo lock que el panel y el reintento nocturno: si otro proceso
    ya la está facturando, no se hace nada (ese proceso deja el resultado)."""
    with _lock_liquidacion(solicitud.pk) as obtenido:
        if not obtenido:
            return solicitud
        solicitud.refresh_from_db()
        if solicitud.estado in (SolicitudLiquidacion.Estado.FACTURADO, SolicitudLiquidacion.Estado.DESCARTADO):
            return solicitud
        return liquidar_solicitud(solicitud)


def se_reintenta_solo(solicitud: SolicitudLiquidacion) -> bool:
    """Mismas reglas que la web y el bot: hasta ALERTAS_INTENTOS_MAXIMOS intentos; un rechazo
    definitivo no se reintenta; la caja del SIIC cerrada o fuera de horario no gasta intentos."""
    if solicitud.estado != SolicitudLiquidacion.Estado.ERROR:
        return False
    if es_definitivo(solicitud.error):
        return False
    return es_de_caja(solicitud.error) or solicitud.intentos < settings.ALERTAS_INTENTOS_MAXIMOS


# ---------------------------------------------------------------- lo que ve la pantalla

def _nro_factura(pago: PagoKiosco) -> str:
    if pago.nro_factura or not pago.solicitud.cobranzas_uuid:
        return pago.nro_factura
    try:
        comprobante = get_cobranzas_banco_client().obtener_comprobante_json(pago.solicitud.cobranzas_uuid)
    except CobranzasBancoError as exc:
        logger.warning("kiosco.comprobante_json_fallido alias=%s error=%s", pago.alias, exc)
        return ""
    nro = _texto(comprobante.get("nro_factura"))[:50]
    if nro:
        PagoKiosco.objects.filter(pk=pago.pk).update(nro_factura=nro)
        pago.nro_factura = nro
    return nro


NIT_CESSA = "1000831029"


def _qr_svg(texto: str) -> str:
    """QR como data URI SVG (nítido en térmica y en láser). La pantalla no tiene internet."""
    import segno

    return segno.make(texto, error="m").svg_data_uri(scale=4, border=2)


def comprobante_para_ticket(pago: PagoKiosco) -> dict | None:
    """Datos del ticket que imprime la pantalla, con los mismos campos y el mismo respaldo que
    ComprobanteTicketController de cessa-laravel: el comprobante oficial de api-cobranzas si se
    puede leer, si no lo que guardó el kiosco. None si todavía no está facturado."""
    solicitud = pago.solicitud
    if pago.estado != PagoKiosco.Estado.PAGADO or solicitud is None or solicitud.estado != SolicitudLiquidacion.Estado.FACTURADO:
        return None

    doc = {}
    if solicitud.cobranzas_uuid:
        try:
            doc = get_cobranzas_banco_client().obtener_comprobante_json(solicitud.cobranzas_uuid) or {}
        except CobranzasBancoError as exc:
            logger.warning("kiosco.comprobante_json_fallido alias=%s error=%s", pago.alias, exc)

    def texto(clave, defecto=""):
        valor = doc.get(clave)
        return _reparar_encoding(_texto(valor)) if valor not in (None, "") else defecto

    if doc.get("detalle"):
        detalle = [
            {
                "descripcion": _reparar_encoding(_texto(i.get("descripcion") or i.get("detalle") or "Concepto")),
                "subtotal": str(i.get("subtotal") if i.get("subtotal") is not None else i.get("precio_unitario") or 0),
            }
            for i in doc["detalle"] if isinstance(i, dict)
        ]
    else:
        detalle = [{"descripcion": periodo(i), "subtotal": str(importe_firmado(i))} for i in pago.detalle]

    nro_factura = texto("nro_factura", pago.nro_factura)
    cuf = texto("codigo_autorizacion")
    url_qr = texto("url_sfe")
    if not url_qr and cuf and nro_factura:
        url_qr = f"https://siat.impuestos.gob.bo/consulta/QR?nit={NIT_CESSA}&cuf={cuf}&numero={nro_factura}&t=2"

    pagado = timezone.localtime(pago.pagado_en or solicitud.fecha_pago)
    return {
        "alias": pago.alias,
        "periodo": texto("periodo", ", ".join(dict.fromkeys(periodo(i) for i in pago.detalle))),
        "total_pagar": str(doc.get("total_pagar") or pago.monto),
        "total_pagar_literal": texto("total_pagar_literal"),
        "cliente_nombre": texto("cliente_nombre", pago.nombre_cliente),
        "cliente_codigo": texto("cliente_codigo", pago.nro_cliente),
        "nro_medidor": texto("nro_medidor"),
        "consumo_kwh": texto("consumo"),
        "detalle": detalle,
        "nro_factura": nro_factura,
        "codigo_autorizacion": cuf,
        "fecha_pago": texto("fecha_cancelacion", f"{pagado:%d/%m/%Y %H:%M}"),
        "cajero": texto("cajero", "CESSA VIRTUAL"),
        "caja": texto("caja", "KIOSCO"),
        "kiosco": pago.kiosco,
        "url_siat": url_qr,
        "qr_siat": _qr_svg(url_qr) if url_qr else "",
    }


def factura_pdf(pago: PagoKiosco) -> bytes | None:
    """PDF oficial de la factura: el que se guardó al facturar o, si falta, se pide de nuevo a
    api-cobranzas. None si todavía no está facturado."""
    solicitud = pago.solicitud
    if pago.estado != PagoKiosco.Estado.PAGADO or solicitud is None or solicitud.estado != SolicitudLiquidacion.Estado.FACTURADO:
        return None
    if solicitud.comprobante_pdf:
        return bytes(solicitud.comprobante_pdf)
    try:
        pdf = get_cobranzas_banco_client().obtener_comprobante_pdf(solicitud.cobranzas_uuid)
    except CobranzasBancoError as exc:
        logger.warning("kiosco.factura_pdf_fallida alias=%s error=%s", pago.alias, exc)
        return None
    SolicitudLiquidacion.objects.filter(pk=solicitud.pk).update(comprobante_pdf=pdf)
    return pdf


# ---------------------------------------------------------------- envío por correo

MAX_CORREOS_POR_PAGO = 3


def enmascarar_correo(correo: str) -> str:
    """"juan.perez@gmail.com" -> "j*********@gmail.com": la pantalla nunca muestra el correo entero."""
    usuario, _, dominio = (correo or "").partition("@")
    if not usuario or not dominio:
        return ""
    return f"{usuario[0]}{'*' * max(3, len(usuario) - 1)}@{dominio}"


def contacto_registrado(nro_cliente: str) -> dict:
    """Correo (verificado con código) y celular que el cliente registró en "Actualizar Datos" de
    cessa-laravel. {} si no hay, si no está configurado o si la web no responde: el kiosco
    sigue funcionando y el cliente puede escribir otro correo."""
    if not settings.KIOSCO_CONTACTO_URL or not settings.API_KEY_CESSA_LARAVEL:
        return {}
    url = settings.KIOSCO_CONTACTO_URL.replace("{nro_cliente}", nro_cliente)
    try:
        respuesta = requests.get(url, timeout=8, headers={"X-Api-Key": settings.API_KEY_CESSA_LARAVEL, "Accept": "application/json"})
        if respuesta.status_code == 404:
            return {}
        respuesta.raise_for_status()
        datos = respuesta.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("kiosco.contacto_no_leido nro_cliente=%s error=%s", nro_cliente, exc)
        return {}
    return {k: _texto(datos.get(k)) for k in ("correo", "celular") if isinstance(datos, dict) and _texto(datos.get(k))}


def _registrar_envio(pago: PagoKiosco, canal: str, destino: str, registrado: bool, error: str = "") -> None:
    EnvioFactura.objects.create(
        pago=pago, canal=canal, destino=destino[:254], registrado=registrado, exitoso=not error, error=error[:300]
    )


def enviar_factura_por_correo(pago: PagoKiosco, correo: str | None) -> str:
    """Manda la factura oficial (PDF) al correo dado o, si es None, al registrado en la web.
    Devuelve el correo enmascarado; lanza KioscoError con el motivo para la pantalla."""
    from django.core.exceptions import ValidationError
    from django.core.mail import EmailMessage
    from django.core.validators import validate_email

    if pago.correos_enviados >= MAX_CORREOS_POR_PAGO:
        raise KioscoError("Ya enviamos esta factura varias veces. Si no te llegó, pídela en ventanilla.")
    registrado = correo is None
    if correo is None:
        correo = contacto_registrado(pago.nro_cliente).get("correo", "")
        if not correo:
            raise KioscoError("No encontramos un correo registrado. Escribe tu correo.")
    correo = correo.strip().lower()
    try:
        validate_email(correo)
    except ValidationError as exc:
        raise KioscoError("Ese correo no parece válido. Revísalo.") from exc
    pdf = factura_pdf(pago)
    if pdf is None:
        raise KioscoError("Tu factura todavía no está lista. Intenta en un momento.")

    nro = pago.nro_factura or _nro_factura(pago)
    cuerpo = (
        "*** CORREO AUTOMÁTICO: POR FAVOR NO RESPONDAS A ESTE MENSAJE ***\n\n"
        f"Hola {pago.nombre_cliente or ''},\n\n"
        f"Gracias por tu pago. Adjuntamos tu factura{' N° ' + nro if nro else ''} "
        f"del cliente {pago.nro_cliente} por Bs {pago.monto:.2f}, pagada con QR en un cajero automático de CESSA.\n\n"
        "Esta cuenta no recibe mensajes. Para consultas: Atención al Cliente (591-4) 64-51200, "
        "línea gratuita 176, o en nuestras oficinas.\n\n"
        "Compañía Eléctrica Sucre S.A."
    )
    mensaje = EmailMessage(
        f"Tu factura CESSA - Cliente N° {pago.nro_cliente} (correo automático, no responder)", cuerpo, None, [correo]
    )
    mensaje.attach(f"factura-cessa-{nro or pago.alias}.pdf", pdf, "application/pdf")
    try:
        mensaje.send(fail_silently=False)
    except Exception as exc:  # noqa: BLE001 -- SMTP caído: el pago ya está, solo falla el envío
        logger.error("kiosco.correo_fallido alias=%s error=%s", pago.alias, exc)
        _registrar_envio(pago, EnvioFactura.Canal.CORREO, correo, registrado, error=str(exc))
        raise KioscoError("No pudimos enviar el correo ahora. Intenta de nuevo o pide tu factura en ventanilla.") from exc
    PagoKiosco.objects.filter(pk=pago.pk).update(correos_enviados=pago.correos_enviados + 1)
    _registrar_envio(pago, EnvioFactura.Canal.CORREO, correo, registrado)
    logger.info("kiosco.correo_enviado alias=%s a=%s", pago.alias, enmascarar_correo(correo))
    return enmascarar_correo(correo)


# ---------------------------------------------------------------- envío por WhatsApp

MAX_WHATSAPP_POR_PAGO = 3


def normalizar_celular(texto) -> str | None:
    """Celular de Bolivia con código de país, solo dígitos: "71234567" -> "59171234567"."""
    digitos = "".join(c for c in str(texto or "") if c.isdigit())
    if len(digitos) == 11 and digitos.startswith("591"):
        digitos = digitos[3:]
    if len(digitos) == 8 and digitos[0] in "67":
        return "591" + digitos
    return None


def enmascarar_celular(celular: str) -> str:
    """"59171234567" o "71234567" -> "7*****67"."""
    digitos = "".join(c for c in str(celular or "") if c.isdigit())[-8:]
    return f"{digitos[0]}{'*' * (len(digitos) - 3)}{digitos[-2:]}" if len(digitos) == 8 else ""


def enviar_factura_por_whatsapp(pago: PagoKiosco, celular: str | None) -> str:
    """Manda por WhatsApp los datos de la factura y el link de verificación del SIAT (la API de
    notificaciones de CESSA solo envía texto, sin PDF). `celular` None = el registrado en la web.
    Devuelve el número enmascarado; lanza KioscoError con el motivo para la pantalla."""
    if not settings.KIOSCO_WHATSAPP_URL or not settings.KIOSCO_WHATSAPP_API_KEY:
        raise KioscoError("El envío por WhatsApp no está disponible en esta pantalla.")
    if pago.whatsapp_enviados >= MAX_WHATSAPP_POR_PAGO:
        raise KioscoError("Ya enviamos esta factura varias veces. Si no te llegó, pídela en ventanilla.")
    registrado = celular is None
    if celular is None:
        celular = contacto_registrado(pago.nro_cliente).get("celular", "")
        if not celular:
            raise KioscoError("No encontramos un celular registrado. Escribe tu número.")
    numero = normalizar_celular(celular)
    if not numero:
        raise KioscoError("Ese número no parece un celular válido (8 dígitos, empieza con 6 o 7).")
    datos = comprobante_para_ticket(pago)
    if datos is None:
        raise KioscoError("Tu factura todavía no está lista. Intenta en un momento.")

    lineas = [
        "*CESSA - Comprobante de tu pago*",
        "",
        f"Cliente: {datos['cliente_codigo']} - {datos['cliente_nombre']}",
        f"Factura N°: {datos['nro_factura']}" if datos["nro_factura"] else "",
        f"Período: {datos['periodo']}",
        f"Monto pagado: Bs {Decimal(datos['total_pagar']):.2f}",
        f"Fecha de pago: {datos['fecha_pago']}",
    ]
    if datos["url_siat"]:
        lineas += ["", "Verifica tu factura en Impuestos Nacionales (SIAT):", datos["url_siat"]]
    lineas += ["", "_Mensaje automático: por favor no respondas a este número._"]
    texto = "\n".join(linea for linea in lineas if linea is not None)

    enviados = pago.whatsapp_enviados
    try:
        respuesta = requests.post(
            settings.KIOSCO_WHATSAPP_URL, timeout=15,
            headers={"x-api-key": settings.KIOSCO_WHATSAPP_API_KEY, "Content-Type": "application/json"},
            json={"telefono": numero, "mensaje": texto, "id_evento": f"{pago.alias}-wpp-{enviados + 1}"},
        )
        cuerpo = respuesta.json() if respuesta.content else {}
        if not respuesta.ok or not cuerpo.get("ok"):
            raise ValueError(f"HTTP {respuesta.status_code}: {str(cuerpo)[:200]}")
    except (requests.RequestException, ValueError) as exc:
        logger.error("kiosco.whatsapp_fallido alias=%s error=%s", pago.alias, exc)
        _registrar_envio(pago, EnvioFactura.Canal.WHATSAPP, numero, registrado, error=str(exc))
        raise KioscoError("No pudimos enviar el WhatsApp ahora. Intenta de nuevo o pide tu factura en ventanilla.") from exc
    PagoKiosco.objects.filter(pk=pago.pk).update(whatsapp_enviados=enviados + 1)
    _registrar_envio(pago, EnvioFactura.Canal.WHATSAPP, numero, registrado)
    logger.info("kiosco.whatsapp_enviado alias=%s a=%s", pago.alias, enmascarar_celular(numero))
    return enmascarar_celular(numero)


def recibo_de_pago(pago: PagoKiosco) -> dict | None:
    """Datos del recibo que imprime la pantalla apenas SIP confirma el pago (no es la factura: esa
    se emite sola y el cliente la descarga en la app o la web). Sale de lo guardado en el pago,
    así que no depende del SIIC. None si el pago no está confirmado."""
    if pago.estado != PagoKiosco.Estado.PAGADO:
        return None
    pagado = timezone.localtime(pago.pagado_en or pago.creado_en)
    return {
        "nro_cliente": pago.nro_cliente,
        "nombre": pago.nombre_cliente,
        "meses": [
            {
                "periodo": periodo(i),
                "detalle": _reparar_encoding(_texto(i.get("detalle"))),
                "importe": str(importe_firmado(i)),
            }
            for i in pago.detalle or []
        ],
        "total": str(pago.monto),
        "fecha_pago": pagado.strftime("%d/%m/%Y %H:%M"),
        "codigo": pago.alias,
        "numero_orden": pago.numero_orden,
        "cajero": pago.kiosco,
    }


def estado_para_pantalla(pago: PagoKiosco) -> dict:
    """pendiente | vencido | cancelado | facturando | facturado | sin_factura."""
    datos = {
        "alias": pago.alias,
        "monto": str(pago.monto),
        "vence_en": pago.vence_en.isoformat(),
        "segundos_restantes": max(0, int((pago.vence_en - timezone.now()).total_seconds())),
    }
    if pago.estado != PagoKiosco.Estado.PAGADO:
        return {**datos, "estado": pago.estado}

    solicitud = pago.solicitud
    if solicitud is None or solicitud.estado == SolicitudLiquidacion.Estado.PENDIENTE:
        return {**datos, "estado": "facturando"}
    if solicitud.estado == SolicitudLiquidacion.Estado.FACTURADO:
        return {**datos, "estado": "facturado", "nro_factura": _nro_factura(pago)}
    # ERROR o DESCARTADO: el pago está registrado; la factura la sigue el equipo (alertas, panel).
    return {**datos, "estado": "sin_factura"}
