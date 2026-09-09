"""Cliente para la pasarela SIP de MC4 (https://mc4.com.bo).

Es la misma pasarela que ya usa cessa-laravel en producción (ver
`SipQrProvider.php` en ese repo, cuenta destino configurada hoy es Banco
BISA). El flujo replicado acá es el mismo: autenticar (token de 1h, se
cachea) -> generar/inhabilitar/consultar QR con `apikeyServicio`.

`SipMC4Client` es un stub documentado: sigue el contrato real endpoint por
endpoint, pero no se probó todavía contra el ambiente de MC4 desde este
proyecto (faltan credenciales, ver .env.example). Para desarrollar sin red,
usar una implementación de prueba que satisfaga `MC4ClientInterface` y
apuntar `MC4_CLIENT_CLASS` a ella.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import requests
from django.conf import settings
from django.core.cache import cache


class EstadoPagoMC4:
    PAGADO = "PAGADO"
    INHABILITADO = "INHABILITADO"
    EXPIRADO = "EXPIRADO"
    ERROR = "ERROR"
    PENDIENTE = "PENDIENTE"


@dataclass(frozen=True)
class SolicitudQR:
    alias: str
    monto: Decimal
    moneda: str
    descripcion: str
    fecha_vencimiento: date
    callback_url: str = ""
    unico_uso: bool = True


@dataclass(frozen=True)
class ResultadoQR:
    imagen_qr_base64: str
    id_qr: str
    id_transaccion: str
    fecha_vencimiento: datetime
    banco_destino: str
    cuenta_destino: str


@dataclass(frozen=True)
class EstadoTransaccionQR:
    alias: str
    estado: str
    fecha_procesamiento: datetime | None
    monto: Decimal | None
    numero_orden_originante: str | None
    id_qr: str | None


class MC4ClientError(Exception):
    """Error genérico de la integración con MC4/SIP."""


class MC4AuthenticationError(MC4ClientError):
    pass


class MC4RequestError(MC4ClientError):
    pass


class MC4ClientInterface(abc.ABC):
    """Contrato que espera el resto del sistema. Cualquier implementación
    (real, mock, sandbox) debe cumplirlo para poder inyectarse vía
    `MC4_CLIENT_CLASS`."""

    @abc.abstractmethod
    def generar_qr(self, solicitud: SolicitudQR) -> ResultadoQR: ...

    @abc.abstractmethod
    def inhabilitar_qr(self, alias: str) -> None: ...

    @abc.abstractmethod
    def consultar_estado(self, alias: str) -> EstadoTransaccionQR: ...


class SipMC4Client(MC4ClientInterface):
    TOKEN_CACHE_KEY = "mc4:auth_token"
    TOKEN_TTL_SECONDS = 55 * 60  # el token real dura 1h; se cachea un poco menos por margen.

    def __init__(
        self,
        base_url: str | None = None,
        apikey: str | None = None,
        username: str | None = None,
        password: str | None = None,
        apikey_servicio: str | None = None,
        timeout: int = 20,
    ):
        self.base_url = base_url or settings.MC4_BASE_URL
        self.apikey = apikey or settings.MC4_APIKEY
        self.username = username or settings.MC4_USERNAME
        self.password = password or settings.MC4_PASSWORD
        self.apikey_servicio = apikey_servicio or settings.MC4_APIKEY_SERVICIO
        self.timeout = timeout

    def generar_qr(self, solicitud: SolicitudQR) -> ResultadoQR:
        payload = {
            "alias": solicitud.alias,
            "callback": solicitud.callback_url,
            # SIP limita la glosa a 30 caracteres.
            "detalleGlosa": solicitud.descripcion[:30],
            "monto": round(float(solicitud.monto), 2),
            "moneda": solicitud.moneda,
            "fechaVencimiento": solicitud.fecha_vencimiento.strftime("%d/%m/%Y"),
            "tipoSolicitud": "API",
            "unicoUso": "true" if solicitud.unico_uso else "false",
        }
        body = self._post_autenticado("/api/v1/generaQr", payload, operacion="generar QR")
        objeto = body["objeto"]

        return ResultadoQR(
            imagen_qr_base64=objeto["imagenQr"],
            id_qr=str(objeto["idQr"]),
            id_transaccion=str(objeto["idTransaccion"]),
            fecha_vencimiento=datetime.strptime(objeto["fechaVencimiento"], "%d/%m/%Y"),
            banco_destino=objeto["bancoDestino"],
            cuenta_destino=objeto["cuentaDestino"],
        )

    def inhabilitar_qr(self, alias: str) -> None:
        self._post_autenticado("/api/v1/inhabilitarPago", {"alias": alias}, operacion="inhabilitar QR")

    def consultar_estado(self, alias: str) -> EstadoTransaccionQR:
        body = self._post_autenticado("/api/v1/estadoTransaccion", {"alias": alias}, operacion="consultar estado")
        objeto = body["objeto"]

        return EstadoTransaccionQR(
            alias=objeto["alias"],
            estado=objeto["estadoActual"],
            fecha_procesamiento=(
                datetime.fromisoformat(objeto["fechaProcesamiento"])
                if objeto.get("fechaProcesamiento")
                else None
            ),
            monto=Decimal(str(objeto["monto"])) if "monto" in objeto else None,
            numero_orden_originante=objeto.get("numeroOrdenOriginante"),
            id_qr=objeto.get("idQr"),
        )

    def _obtener_token(self, forzar_refresco: bool = False) -> str:
        if forzar_refresco:
            cache.delete(self.TOKEN_CACHE_KEY)

        token = cache.get(self.TOKEN_CACHE_KEY)
        if token:
            return token

        response = requests.post(
            f"{self.base_url}/autenticacion/v1/generarToken",
            headers={"apikey": self.apikey},
            json={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        if not response.ok:
            raise MC4AuthenticationError(f"HTTP {response.status_code}: {response.text}")

        body = response.json()
        if body.get("codigo") != "OK":
            raise MC4AuthenticationError(body.get("mensaje", "respuesta sin código OK"))

        token = body["objeto"]["token"]
        cache.set(self.TOKEN_CACHE_KEY, token, self.TOKEN_TTL_SECONDS)
        return token

    def _post_autenticado(self, path: str, payload: dict, operacion: str, _reintentado: bool = False) -> dict:
        token = self._obtener_token()
        response = requests.post(
            f"{self.base_url}{path}",
            headers={"apikeyServicio": self.apikey_servicio, "Authorization": f"Bearer {token}"},
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code == 401 and not _reintentado:
            self._obtener_token(forzar_refresco=True)
            return self._post_autenticado(path, payload, operacion, _reintentado=True)

        return self._decodificar(response, operacion, codigo_exito="0000")

    def _decodificar(self, response: requests.Response, operacion: str, codigo_exito: str) -> dict:
        if not response.ok and response.status_code != 400:
            raise MC4RequestError(f"{operacion}: HTTP {response.status_code}: {response.text}")

        body = response.json() if response.content else None
        if not isinstance(body, dict) or body.get("codigo") != codigo_exito:
            mensaje = body.get("mensaje", "respuesta inesperada") if isinstance(body, dict) else response.text
            raise MC4RequestError(f"{operacion}: {mensaje}")

        return body


def get_mc4_client() -> MC4ClientInterface:
    """Punto único de resolución del cliente configurado (real, mock o sandbox)."""
    from django.utils.module_loading import import_string

    client_class = import_string(settings.MC4_CLIENT_CLASS)
    return client_class()
