"""Tipo de cambio oficial del dólar publicado por el Banco Central de Bolivia, como referencia para
comparar con la cotización cargada en el SIIC (la que manda: con esa se cierran las cajas).

El BCB no tiene una API documentada: se lee su página de "última cotización". Si falla (caída,
cambio de formato), se usa factura.bo, que la copia del BCB en JSON. Solo se piden datos públicos:
no se manda nada del panel ni de los usuarios.

Para cargar en el SIIC no alcanza con una fuente: `leer_contraste` lee una segunda (apibcb.cucu.bo, que
también copia al BCB) y la carga solo se hace si las dos dan el mismo valor (apps/cobranza/cotizacion.py).
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlparse
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings

TIMEOUT = 20
_AGENTE = "Mozilla/5.0 (compatible; cotizacion-dolar)"


class TipoCambioError(Exception):
    pass


@dataclass
class TipoCambioBCB:
    valor: Decimal  # Bs por $us
    vigencia: str  # "del sábado 14 de noviembre de 2026 al lunes 16 de noviembre de 2026"
    fuente: str  # "bcb.gob.bo" o "factura.bo"


def _decimal(texto) -> Decimal:
    try:
        valor = Decimal(str(texto).strip().replace(".", "").replace(",", ".") if isinstance(texto, str) else str(texto))
    except InvalidOperation:
        raise TipoCambioError(f"Valor de cotización ilegible: {texto!r}")
    if not Decimal("1") <= valor <= Decimal("100"):
        raise TipoCambioError(f"Cotización fuera de rango: {valor}")
    return valor


def _texto_plano(pagina: str) -> str:
    sin_estilos = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", pagina, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", sin_estilos))).strip()


def leer_de_bcb() -> TipoCambioBCB:
    try:
        r = requests.get(settings.BCB_COTIZACION_URL, timeout=TIMEOUT, headers={"User-Agent": _AGENTE})
        r.raise_for_status()
    except requests.RequestException as exc:
        raise TipoCambioError(f"BCB no responde: {exc}") from exc
    texto = _texto_plano(r.text)
    m = re.search(r"[ÚU]ltima cotizaci[óo]n\s*Bs\s*([\d.,]+)\s*/\s*\$us", texto, re.I)
    if not m:
        raise TipoCambioError("No se encontró la cotización en la página del BCB (¿cambió el formato?).")
    vigencia = re.search(r"VIGENCIA:\s*(.+?)(?:\s+M[áa]s informaci[óo]n|\s+Tipos de cambio|$)", texto, re.I)
    return TipoCambioBCB(_decimal(m.group(1)), (vigencia.group(1).strip().lower() if vigencia else "")[:120], "bcb.gob.bo")


def leer_de_factura_bo() -> TipoCambioBCB:
    try:
        r = requests.get(settings.FACTURA_BO_COTIZACION_URL, timeout=TIMEOUT, headers={"User-Agent": _AGENTE})
        r.raise_for_status()
        datos = (r.json() or {}).get("datos") or {}
    except (requests.RequestException, ValueError) as exc:
        raise TipoCambioError(f"factura.bo no responde: {exc}") from exc
    if datos.get("usd_bob") is None:
        raise TipoCambioError("factura.bo no trae la cotización.")
    vigencia = str(datos.get("vigencia_usd") or "").removeprefix("VIGENCIA:").strip().lower()
    return TipoCambioBCB(_decimal(datos["usd_bob"]), vigencia[:120], "factura.bo")


def tipo_cambio_oficial() -> TipoCambioBCB:
    """Del BCB; si no se puede, de factura.bo. Si fallan los dos, TipoCambioError con ambos motivos."""
    try:
        return leer_de_bcb()
    except TipoCambioError as primero:
        try:
            return leer_de_factura_bo()
        except TipoCambioError as segundo:
            raise TipoCambioError(f"{primero} / {segundo}") from segundo


@dataclass
class Contraste:
    valor: Decimal
    desde: date
    hasta: date
    fuente: str  # el host, p. ej. "apibcb.cucu.bo"


def leer_contraste() -> Contraste:
    """La segunda opinión (COTIZACION_CONTRASTE_URL): {"tc_oficial": {"valor", "vigencia_desde", "vigencia_hasta",
    "desactualizado"}}. Cualquier cosa rara → TipoCambioError (y entonces no se carga nada)."""
    url = settings.COTIZACION_CONTRASTE_URL
    fuente = urlparse(url).hostname or url
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": _AGENTE})
        r.raise_for_status()
        tc = (r.json() or {}).get("tc_oficial") or {}
    except (requests.RequestException, ValueError, AttributeError) as exc:
        raise TipoCambioError(f"{fuente} no responde: {exc}") from exc
    if tc.get("desactualizado") is not False:
        raise TipoCambioError(f"{fuente} dice que su cotización está desactualizada (o no lo dice).")
    try:
        valor = _decimal(Decimal(str(tc.get("valor")).strip()))
        desde, hasta = date.fromisoformat(str(tc.get("vigencia_desde"))), date.fromisoformat(str(tc.get("vigencia_hasta")))
    except (InvalidOperation, ValueError) as exc:
        raise TipoCambioError(f"{fuente} trae datos ilegibles: {exc}") from exc
    return Contraste(valor, desde, hasta, fuente)
