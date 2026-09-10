"""Cliente para consultar la deuda de un cliente.

Diseñado para que la fuente pueda cambiar sin tocar el resto del sistema: el
resto de la app solo conoce `DeudaClientInterface` y llama a `get_deuda_client()`.

`SiicDeudaClient` pega directo al mismo endpoint SIIC que ya usa
`cessa-laravel` en producción (`CessaApiService::consultaDeuda()` ->
`GET /v1/consulta/cliente`), reutilizando las mismas credenciales
(`CESSA_API_URL`/`CESSA_API_TOKEN` allá, `SIIC_DEUDA_BASE_URL`/
`SIIC_DEUDA_TOKEN` acá) -- confirmado 2026-09-07 leyendo
`CessaApiService.php`/`ConsultaDeudaController.php`/`PagoQrController.php`
del repo hermano, no adivinado. Detalles del contrato real:
- `GET {base_url}/v1/consulta/cliente?nro_cliente=...&ver_deuda=si`
- Header `Authorization: <token>` -- el token va tal cual, SIN prefijo
  "Bearer" (a diferencia de lo que asumía el stub anterior de este archivo).
- Respuesta: `{"error"?, "nro_cliente", "nombre", "deuda": [...], ...}` --
  `error` presente o `nro_cliente` vacío significa "no se encontró abonado".
- Cada ítem de `deuda` trae el shape ya confirmado en
  `NECESIDADES_SIIC_FACTURACION.md`: `codigo_sucursal`, `nro_comprobante`,
  `nro_suministro`, `fecha`, `tipo`, `letra_comprobante`, `nro_autorizacion`,
  `nro_cliente`, `anio`, `mes`, `importe`, `detalle`, más `debito_credito`
  (no confiar en el signo de `importe` tal cual -- cessa-laravel deriva el
  signo real de `debito_credito`, "CREDITO" resta, cualquier otro valor
  suma; se replica ese mismo criterio acá).

Nota de alcance: a diferencia de `ConsultaDeudaController`/`PagoQrController`
de cessa-laravel (público, sin login), este endpoint es interno y requiere
que el usuario ya esté autenticado como cajero -- por eso no se replica acá
el segundo factor obligatorio (N° de Cuenta / zona-manzano-correlativo) que
esos controllers exigen para el público general. Si esto deja de ser
suficiente (ej. auditoría pide el mismo doble factor puertas adentro),
avisar para agregarlo.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from decimal import Decimal

import requests
from django.conf import settings


@dataclass(frozen=True)
class ItemDeuda:
    codigo_sucursal: str
    nro_comprobante: str
    nro_suministro: str
    fecha: str
    tipo: str
    letra_comprobante: str
    nro_autorizacion: str
    nro_cliente: str
    anio: int
    mes: int
    # Magnitud con signo ya aplicado (negativo = a favor del cliente, ej. una
    # conciliación NC) -- ver `_importe_firmado()` más abajo, mismo criterio
    # que cessa-laravel: el signo nunca se toma de `importe` tal cual llega
    # de SIIC, se deriva siempre de `debito_credito`.
    importe: Decimal
    detalle: str
    debito_credito: str


@dataclass(frozen=True)
class ResultadoConsultaDeuda:
    codigo_externo_cliente: str
    nombre_cliente: str
    items: list[ItemDeuda]

    @property
    def monto_total(self) -> Decimal:
        return sum((item.importe for item in self.items), Decimal("0"))


class DeudaClientError(Exception):
    """Error genérico al consultar la deuda de un cliente (falla real de
    conexión/formato -- la fuente no pudo responder en absoluto)."""


class ClienteNoEncontradoError(DeudaClientError):
    """SIIC respondió normalmente pero no existe un abonado con ese número
    -- resultado esperado, no una falla de la integración. Separado de
    DeudaClientError para que la vista pueda distinguir "no existe" (404)
    de "la fuente no responde" (502)."""


class DeudaClientInterface(abc.ABC):
    @abc.abstractmethod
    def consultar_deuda(self, codigo_externo: str) -> ResultadoConsultaDeuda: ...


def _reparar_codificacion(valor):
    """La base de datos del SIIC guarda nombres/direcciones como UTF-8
    doble-codificado para un subconjunto de registros (ej. "SIÑANI" vuelve
    como "SIÃ\\x91ANI") -- problema de origen del sistema comercial, no algo
    que se pueda arreglar en la fuente. Mismo fix que ya usa
    `CessaApiService::fixEncoding()` del lado Laravel (confirmado
    2026-09-07 con un caso real: "SIÃANI DURAN JHAMINA NADIR"), portado acá:
    reinterpretar la cadena como ISO-8859-1 y decodificarla como UTF-8
    recupera los bytes originales. Texto ya correcto no es válido como ese
    round-trip (o da la misma cadena), así que nunca se toca por error."""
    if not isinstance(valor, str) or valor == "":
        return valor
    try:
        candidato = valor.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return valor
    return candidato if candidato != valor else valor


def _reparar_codificacion_recursivo(datos):
    if isinstance(datos, dict):
        return {clave: _reparar_codificacion_recursivo(valor) for clave, valor in datos.items()}
    if isinstance(datos, list):
        return [_reparar_codificacion_recursivo(valor) for valor in datos]
    return _reparar_codificacion(datos)


def _importe_firmado(item: dict) -> Decimal:
    """Mismo criterio que `CessaApiService`/`ConsultaDeudaController` del
    lado Laravel: nunca se confía en el signo de `importe` tal cual viene de
    SIIC, se deriva siempre de `debito_credito` ("CREDITO" resta, cualquier
    otro valor -- típicamente "DEBITO" -- suma)."""
    magnitud = abs(Decimal(str(item.get("importe", 0))))
    if str(item.get("debito_credito", "DEBITO")).upper() == "CREDITO":
        return -magnitud
    return magnitud


class SiicDeudaClient(DeudaClientInterface):
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: int = 20):
        self.base_url = base_url or settings.SIIC_DEUDA_BASE_URL
        self.token = token or settings.SIIC_DEUDA_TOKEN
        self.timeout = timeout

    def consultar_deuda(self, codigo_externo: str) -> ResultadoConsultaDeuda:
        try:
            response = requests.get(
                f"{self.base_url}/v1/consulta/cliente",
                params={"nro_cliente": codigo_externo, "ver_deuda": "si"},
                # SIN "Bearer": el token va tal cual en el header, igual que
                # CessaApiService del lado Laravel.
                headers={"Authorization": self.token},
                timeout=self.timeout,
            )
            # A propósito NO se llama a response.raise_for_status(): SIIC
            # devuelve "cliente no encontrado" como 4xx con un body JSON
            # parseable (`{"error": "..."}"`), no como una falla de
            # conexión -- mismo motivo por el que CessaApiService del lado
            # Laravel usa `retry(..., throw: false)`. Solo una excepción de
            # requests (timeout, DNS, conexión rechazada) es un error real
            # de cliente acá.
            body = response.json()
        except requests.RequestException as exc:
            raise DeudaClientError(f"consulta-deuda {codigo_externo}: {exc}") from exc
        except ValueError as exc:
            raise DeudaClientError(f"consulta-deuda {codigo_externo}: respuesta no es JSON válido") from exc

        body = _reparar_codificacion_recursivo(body)

        # 401/403 es SIIC rechazando el token configurado, no "no existe el
        # abonado" -- antes caía en el `body.get("error")` de abajo y se
        # reportaba como ClienteNoEncontradoError (404), escondiendo un
        # problema real de credenciales detrás de un resultado esperado.
        if response.status_code in (401, 403):
            raise DeudaClientError(
                f"consulta-deuda {codigo_externo}: SIIC rechazó las credenciales configuradas "
                f"(HTTP {response.status_code}: {body})"
            )

        if body.get("error") or not body.get("nro_cliente"):
            raise ClienteNoEncontradoError(f"No se encontró ningún abonado con el número {codigo_externo}.")

        items = [
            ItemDeuda(
                codigo_sucursal=str(item.get("codigo_sucursal", "")),
                nro_comprobante=str(item.get("nro_comprobante", "")),
                nro_suministro=str(item.get("nro_suministro", "")),
                fecha=str(item.get("fecha", "")),
                tipo=str(item.get("tipo", "")),
                letra_comprobante=str(item.get("letra_comprobante", "")),
                nro_autorizacion=str(item.get("nro_autorizacion", "")),
                nro_cliente=str(item.get("nro_cliente", "")),
                anio=int(item["anio"]),
                mes=int(item["mes"]),
                importe=_importe_firmado(item),
                detalle=item.get("detalle", ""),
                debito_credito=str(item.get("debito_credito", "")),
            )
            for item in body.get("deuda", [])
        ]

        return ResultadoConsultaDeuda(
            codigo_externo_cliente=codigo_externo,
            nombre_cliente=body.get("nombre", ""),
            items=items,
        )


def get_deuda_client() -> DeudaClientInterface:
    """Punto único de resolución del cliente configurado (real, mock o sandbox)."""
    from django.utils.module_loading import import_string

    client_class = import_string(settings.DEUDA_CLIENT_CLASS)
    return client_class()
