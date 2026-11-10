"""Callback único de SIP (Banco BISA) para todos los sistemas de CESSA que generan QR SIP.

SIP tiene un solo Basic Auth por cuenta, así que en vez de que cada sistema exponga el suyo,
todos mandan en `generaQr` la URL de este endpoint (`settings.SIP_CALLBACK_URL`) y acá se
decide por el prefijo del alias:

- `txqr-` (QR del panel de cobranza): se confirma contra SIP con `verificar_pago()` -- igual
  que la tarea periódica, así un aviso falso no puede marcar nada.
- `CESSA-KIO-` (pantallas de autoservicio, apps.kiosco): mismo criterio, se confirma contra SIP
  y se factura acá mismo. Las pantallas están en la red interna, sin internet.
- Prefijos de `settings.SIP_CALLBACK_SIN_REENVIO` (hoy `CESSA-BOT-`, el bot de Telegram): se
  responde `0000` sin reenviar. Ese sistema no tiene URL pública y detecta el pago consultando
  el estado del QR a SIP por su cuenta; el aviso solo se registra en el log.
- Prefijos de `settings.SIP_CALLBACK_REENVIOS` (hoy `CESSA-` → cessa-laravel): se reenvía el
  mismo body con `X-Api-Key`, y se le devuelve a SIP lo que responda el destino.

SIP espera siempre JSON con `codigo`: `0000` = éxito; cualquier otro le manda un correo a los
administradores de la cuenta SIP (y SIP reintenta). Nunca se devuelve 502: Cloudflare lo
reemplaza por su propia página (hallazgo 2026-09-23).
"""
import base64
import binascii
import hmac
import logging

import requests
from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.cobranza import services as cobranza_services
from apps.cobranza.models import TransaccionQR

from .models import AvisoSip, SolicitudLiquidacion

logger = logging.getLogger(__name__)

PREFIJO_PANEL = "txqr-"
TIMEOUT_REENVIO = 20


def _basic_auth_valido(request) -> bool:
    usuario = settings.SIP_CALLBACK_USERNAME
    clave = settings.SIP_CALLBACK_PASSWORD
    if not usuario or not clave:
        return False

    encabezado = request.headers.get("Authorization", "")
    if not encabezado.startswith("Basic "):
        return False
    try:
        recibido_usuario, _, recibida_clave = base64.b64decode(encabezado[6:]).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False

    usuario_ok = hmac.compare_digest(usuario.encode(), recibido_usuario.encode())
    clave_ok = hmac.compare_digest(clave.encode(), recibida_clave.encode())
    return usuario_ok and clave_ok


def _guardar_aviso(alias: str, datos) -> None:
    """Para el reporte de cobros (pagador, documento, N° de orden). Nunca frena el aviso."""
    try:
        AvisoSip.objects.create(alias=alias, datos=dict(datos))
    except Exception:  # noqa: BLE001 -- el registro es accesorio; el pago no puede esperar por esto
        logger.exception("sip_callback.aviso_no_guardado alias=%s", alias)


def _respuesta(codigo: str, mensaje: str, http=status.HTTP_200_OK) -> Response:
    return Response({"codigo": codigo, "mensaje": mensaje}, status=http)


class SipCallbackView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        if not _basic_auth_valido(request):
            return _respuesta("9999", "No autorizado", status.HTTP_401_UNAUTHORIZED)

        alias = request.data.get("alias") if isinstance(request.data, dict) else None
        if not isinstance(alias, str) or not alias.strip() or len(alias) > 50:
            return _respuesta("9998", "Datos inválidos: alias es obligatorio", status.HTTP_400_BAD_REQUEST)
        alias = alias.strip()
        _guardar_aviso(alias, request.data)

        if alias.startswith(PREFIJO_PANEL):
            return self._confirmar_en_panel(alias)

        # Antes que los reenvíos: "CESSA-KIO-" y "CESSA-BOT-" también empiezan con "CESSA-".
        if alias.startswith(SolicitudLiquidacion.PREFIJO_KIOSCO):
            return self._confirmar_kiosco(alias)

        if any(alias.startswith(prefijo) for prefijo in settings.SIP_CALLBACK_SIN_REENVIO):
            logger.info("sip_callback.sin_reenvio alias=%s", alias)
            return _respuesta("0000", "Registro Exitoso")

        for prefijo, url in settings.SIP_CALLBACK_REENVIOS.items():
            if alias.startswith(prefijo):
                return self._reenviar(alias, url, request.data)

        logger.warning("sip_callback.alias_desconocido alias=%s", alias)
        return _respuesta("9999", "Alias no reconocido")

    def _confirmar_en_panel(self, alias: str) -> Response:
        try:
            transaccion = TransaccionQR.objects.get(pk=int(alias[len(PREFIJO_PANEL):]))
        except (ValueError, TransaccionQR.DoesNotExist):
            logger.warning("sip_callback.alias_desconocido alias=%s", alias)
            return _respuesta("9999", "Alias no reconocido")

        # Si sigue pendiente se confirma contra SIP; si ya se registró (reintento de SIP o lo
        # tomó antes la tarea periódica) no se toca nada. En ambos casos el aviso se recibió.
        cobranza_services.verificar_pago(transaccion)
        logger.info("sip_callback.panel alias=%s estado=%s", alias, transaccion.estado)
        return _respuesta("0000", "Registro Exitoso")

    def _confirmar_kiosco(self, alias: str) -> Response:
        # Import tardío: apps.kiosco depende de esta app, no al revés.
        from apps.kiosco import services as kiosco_services
        from apps.kiosco.models import PagoKiosco

        pago = PagoKiosco.objects.filter(alias=alias).first()
        if pago is None:
            logger.warning("sip_callback.alias_desconocido alias=%s", alias)
            return _respuesta("9999", "Alias no reconocido")

        # Igual que el panel: el aviso no se cree, se confirma contra SIP. Si ya se registró
        # (reintento de SIP, o lo vio antes la pantalla) no se toca nada.
        if pago.estado != PagoKiosco.Estado.PAGADO:
            pago = kiosco_services.consultar_sip(pago)
        logger.info("sip_callback.kiosco alias=%s estado=%s", alias, pago.estado)
        return _respuesta("0000", "Registro Exitoso")

    def _reenviar(self, alias: str, url: str, datos) -> Response:
        try:
            respuesta = requests.post(
                url,
                json=datos,
                headers={"X-Api-Key": settings.API_KEY_CESSA_LARAVEL, "Accept": "application/json"},
                timeout=TIMEOUT_REENVIO,
            )
            cuerpo = respuesta.json()
        except (requests.RequestException, ValueError) as exc:
            logger.error("sip_callback.reenvio_fallido alias=%s url=%s error=%s", alias, url, exc)
            return _respuesta("9999", "Destino no disponible, reintentar")

        if not isinstance(cuerpo, dict) or "codigo" not in cuerpo:
            logger.error("sip_callback.reenvio_sin_codigo alias=%s http=%s", alias, respuesta.status_code)
            return _respuesta("9999", "Respuesta inválida del destino, reintentar")

        logger.info("sip_callback.reenviado alias=%s http=%s codigo=%s", alias, respuesta.status_code, cuerpo["codigo"])
        http = respuesta.status_code if respuesta.status_code in (200, 400) else status.HTTP_200_OK
        return Response({"codigo": cuerpo["codigo"], "mensaje": cuerpo.get("mensaje", "")}, status=http)
