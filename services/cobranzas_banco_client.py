"""Cliente de api-cobranzas-bancos -- la API Lumen/Passport que expone el
sistema comercial de CESSA (SIIC) para registrar cobros como facturas reales.

Puerto directo de `CobranzasBancoService.php`/`FacturacionRecibo.php` de
cessa-laravel (repo hermano): mismo flujo (autenticar con OAuth2 password
grant, token cacheado -> asegurar Caja abierta -> crear Transacción -> pagar
con `/pagar-otro-documento` -> descargar comprobante) y mismo armado de
`detalle`/`documento`, confirmado ahí decompilando (javap) el .class real de
la app "Cobranza" (Spring Boot/JSF) que ya usa esta API en producción -- no
adivinado acá tampoco.

A diferencia de MC4/SIIC, cessa-laravel nunca pudo probar esto en vivo (sin
red hacia la red interna de CESSA desde Hostinger, ver comentario en
CobranzasBancoService.php) -- por eso esta integración vive acá: el VPS de
cobranza_cessa sí está en la red de facturación y puede llegar a esta API.
"""
from __future__ import annotations

import abc
import json
from datetime import date, datetime
from decimal import Decimal

import requests
from django.conf import settings
from django.core.cache import cache


class CobranzasBancoError(Exception):
    """Error genérico de la integración con api-cobranzas-bancos."""


class CobranzasBancoAuthError(CobranzasBancoError):
    pass


class CobranzasBancoRequestError(CobranzasBancoError):
    pass


class CobranzasBancoClientInterface(abc.ABC):
    """Contrato que espera el resto del sistema. Cualquier implementación
    (real, fake) debe cumplirlo para poder inyectarse vía
    `COBRANZAS_BANCO_CLIENT_CLASS`."""

    @abc.abstractmethod
    def asegurar_caja_abierta(self) -> None: ...

    @abc.abstractmethod
    def crear_transaccion(self) -> str: ...

    @abc.abstractmethod
    def pagar_transaccion(self, uuid: str, detalle: list[dict], documento: dict) -> None: ...

    @abc.abstractmethod
    def pagar_transaccion_propia(self, uuid: str, detalle: list[dict]) -> None: ...

    @abc.abstractmethod
    def obtener_comprobante_pdf(self, uuid: str) -> bytes: ...

    @abc.abstractmethod
    def obtener_comprobante_json(self, uuid: str) -> dict: ...


def _formatear_fecha(valor) -> str:
    """Mismo fallback que FacturacionRecibo::formatearFecha() del lado
    Laravel: "00000000" si no hay fecha o no se puede parsear."""
    if not valor:
        return "00000000"
    if isinstance(valor, (date, datetime)):
        return valor.strftime("%Y%m%d")
    try:
        return datetime.fromisoformat(str(valor)).strftime("%Y%m%d")
    except ValueError:
        # SIIC ya manda fechas como "YYYYMMDD" en el snapshot crudo de deuda
        # (ver ItemDeuda.fecha) -- si ya viene en ese formato, se usa tal cual.
        crudo = str(valor)
        return crudo if len(crudo) == 8 and crudo.isdigit() else "00000000"


def construir_detalle(items_deuda: list[dict], nro_cliente_fallback: str = "") -> list[dict]:
    """Arma el `detalle` (lista de "Deuda") exactamente como lo hace
    `FacturacionRecibo::construirDetalle()`: fechas a "yyyyMMdd", `importe` a
    magnitud sin signo con 2 decimales fijos (el signo lo lleva
    `debito_credito`, nunca se manda un importe negativo). `items_deuda` es
    el snapshot crudo (misma forma que `Recibo::debt_items` de cessa-laravel
    / `ItemDeuda` de `services/deuda_client.py`); `nro_cliente_fallback` se
    usa solo si un ítem no trae `nro_cliente` propio."""
    return [
        {
            "codigo_sucursal": item.get("codigo_sucursal"),
            "nro_comprobante": item.get("nro_comprobante"),
            "nro_suministro": item.get("nro_suministro"),
            "fecha": _formatear_fecha(item.get("fecha")),
            "tipo": item.get("tipo"),
            "letra_comprobante": item.get("letra_comprobante"),
            "nro_autorizacion": item.get("nro_autorizacion"),
            "anio": item.get("anio"),
            "mes": item.get("mes"),
            "importe": f"{abs(Decimal(str(item.get('importe', 0)))):.2f}",
            "debito_credito": item.get("debito_credito"),
            "fecha_vencimiento": _formatear_fecha(item.get("fecha_vencimiento")),
            "fecha_autorizacion": _formatear_fecha(item.get("fecha_autorizacion")),
            "detalle": item.get("detalle"),
            "otras_ventas_codigo": item.get("otras_ventas_codigo"),
            "imprimir_recibo": item.get("imprimir_recibo"),
            "nro_cliente": item.get("nro_cliente") or nro_cliente_fallback,
        }
        for item in items_deuda
    ]


def construir_documento(
    *, nro_cliente: str, monto: Decimal, moneda: str, numero_documento: str, fecha_pago
) -> dict:
    """Arma el `documento` que exige `/pagar-otro-documento`, igual que
    `FacturacionRecibo::construirDocumento()`. `ente_id`/`banco_id` salen de
    catálogos reales de api-cobranzas-bancos (`GET /v1/entes`, `GET
    /v1/bancos`), configurados en settings -- no inventados."""
    fecha = _formatear_fecha(fecha_pago)
    moneda_codigo = "D" if moneda.upper() == "USD" else "B"

    return {
        "ente_id": int(settings.COBRANZAS_BANCO_DOCUMENTO_ENTE_ID),
        "moneda": moneda_codigo,
        "banco_id": int(settings.COBRANZAS_BANCO_DOCUMENTO_BANCO_ID),
        "numero": numero_documento,
        "importe": float(monto),
        "fecha": fecha,
        "fecha_vencimiento": fecha,
    }


class LumenCobranzasBancoClient(CobranzasBancoClientInterface):
    TOKEN_CACHE_KEY = "cobranzas_banco:auth_token"
    TOKEN_TTL_SECONDS = 55 * 60  # el token Passport real dura ~1h; se cachea un poco menos por margen.

    def __init__(
        self,
        base_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        username: str | None = None,
        password: str | None = None,
        agencia_sigla: str | None = None,
        timeout: int = 30,
    ):
        self.base_url = base_url or settings.COBRANZAS_BANCO_BASE_URL
        self.client_id = client_id or settings.COBRANZAS_BANCO_CLIENT_ID
        self.client_secret = client_secret or settings.COBRANZAS_BANCO_CLIENT_SECRET
        self.username = username or settings.COBRANZAS_BANCO_USERNAME
        self.password = password or settings.COBRANZAS_BANCO_PASSWORD
        self.agencia_sigla = agencia_sigla or settings.COBRANZAS_BANCO_AGENCIA_SIGLA
        self.timeout = timeout

    def asegurar_caja_abierta(self) -> None:
        response = self._request_autenticado("get", "/v1/cajas/existe")
        if response.ok:
            return
        if response.status_code != 404:
            raise CobranzasBancoRequestError(
                f"verificar caja: HTTP {response.status_code}: {response.text}"
            )

        response = self._request_autenticado(
            "post",
            "/v1/cajas/aperturar",
            json={
                "agencia_sigla": self.agencia_sigla,
                "monto_inicial": 0,
                "descripcion_apertura": "Apertura automática -- panel de cobranza CESSA (liquidación de cobros)",
            },
        )
        if not response.ok:
            raise CobranzasBancoRequestError(f"aperturar caja: {self._extraer_error(response)}")

    def crear_transaccion(self) -> str:
        response = self._request_autenticado("post", "/v1/transacciones")
        if not response.ok:
            raise CobranzasBancoRequestError(f"crear transacción: {self._extraer_error(response)}")

        uuid = (response.json() or {}).get("uuid")
        if not uuid:
            raise CobranzasBancoRequestError(f"crear transacción: la respuesta no trajo uuid: {response.text}")
        return uuid

    def pagar_transaccion(self, uuid: str, detalle: list[dict], documento: dict) -> None:
        response = self._request_autenticado(
            "put",
            f"/v1/transacciones/{uuid}/pagar-otro-documento",
            json={"detalle": detalle, "documento": documento},
        )
        if not response.ok:
            raise CobranzasBancoRequestError(f"pagar transacción: {self._extraer_error(response)}")

    def pagar_transaccion_propia(self, uuid: str, detalle: list[dict]) -> None:
        """`/pagar` a secas (no `/pagar-otro-documento`) -- para dinero que
        entró por la Caja propia de este mismo backend (cobro en efectivo o
        QR generado por un cajero del panel), a diferencia de
        `pagar_transaccion()` que es para dinero que entró por un canal
        externo (banco/QR web de cessa-laravel). Confirmado en el código
        real de api-cobranzas-bancos (`TransaccionController::pagar()`,
        `routes/web.php`): no exige "documento", solo "detalle" -- el origen
        del dinero lo infiere del Cajero/Caja autenticado."""
        response = self._request_autenticado(
            "put", f"/v1/transacciones/{uuid}/pagar", json={"detalle": detalle}
        )
        if not response.ok:
            raise CobranzasBancoRequestError(f"pagar transacción (propia): {self._extraer_error(response)}")

    def obtener_comprobante_pdf(self, uuid: str) -> bytes:
        response = self._request_autenticado(
            "get", f"/v1/transacciones/{uuid}/documentos", params={"formato": "pdf"}
        )
        if not response.ok:
            raise CobranzasBancoRequestError(f"obtener comprobante: {self._extraer_error(response)}")
        return response.content

    def obtener_comprobante_json(self, uuid: str) -> dict:
        """Datos estructurados del comprobante (nro_factura, cliente, etc.) --
        usado por cessa-laravel (ComprobanteTicketController) para armar el
        ticket imprimible; a diferencia del PDF, este nunca se persiste acá,
        se reenvía en vivo cada vez que se pide."""
        response = self._request_autenticado(
            "get", f"/v1/transacciones/{uuid}/documentos", params={"formato": "json"}
        )
        if not response.ok:
            raise CobranzasBancoRequestError(f"obtener comprobante json: {self._extraer_error(response)}")

        data = response.json() or {}
        if isinstance(data, list):
            return data[0] if data else {}
        return data

    def _obtener_token(self, forzar_refresco: bool = False) -> str:
        if forzar_refresco:
            cache.delete(self.TOKEN_CACHE_KEY)

        token = cache.get(self.TOKEN_CACHE_KEY)
        if token:
            return token

        response = requests.post(
            f"{self.base_url}/oauth/token",
            json={
                "grant_type": "password",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "username": self.username,
                "password": self.password,
                "scope": "",
            },
            timeout=self.timeout,
        )
        if not response.ok:
            raise CobranzasBancoAuthError(f"HTTP {response.status_code}: {response.text}")

        token = (response.json() or {}).get("access_token")
        if not token:
            raise CobranzasBancoAuthError(f"la respuesta no trajo access_token: {response.text}")

        cache.set(self.TOKEN_CACHE_KEY, token, self.TOKEN_TTL_SECONDS)
        return token

    def _request_autenticado(self, metodo: str, path: str, _reintentado: bool = False, **kwargs) -> requests.Response:
        token = self._obtener_token()
        response = requests.request(
            metodo,
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=self.timeout,
            **kwargs,
        )

        if response.status_code == 401 and not _reintentado:
            self._obtener_token(forzar_refresco=True)
            return self._request_autenticado(metodo, path, _reintentado=True, **kwargs)

        return response

    def _extraer_error(self, response: requests.Response) -> str:
        try:
            error = (response.json() or {}).get("error")
        except ValueError:
            return response.text
        if isinstance(error, (dict, list)):
            return json.dumps(error, ensure_ascii=False)
        return str(error) if error else response.text


def get_cobranzas_banco_client() -> CobranzasBancoClientInterface:
    """Punto único de resolución del cliente configurado (real o fake)."""
    from django.utils.module_loading import import_string

    client_class = import_string(settings.COBRANZAS_BANCO_CLIENT_CLASS)
    return client_class()
