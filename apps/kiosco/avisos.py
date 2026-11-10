"""Avisos de cortes para la barra que corre arriba en la pantalla del kiosco.

Dos fuentes, las dos las lee el gateway (la pantalla no tiene internet):
- Cortes programados: la API interna de cortes (KIOSCO_CORTES_API_URL, ej.
  https://10.1.1.106:8080/v1/scheduled-outages, header `Authorization: <token>` sin "Bearer").
  Trae todos los próximos; la página de la web muestra solo 3.
- Cortes de emergencia: la página de inicio de cessa.com.bo (KIOSCO_AVISOS_URL, CessaLanding,
  HomeController::index), que los trae como JSON en `<script data-page="app">` (Inertia). La API no
  los tiene. Si la API no responde, de la web salen también los programados.

Se guardan KIOSCO_AVISOS_MINUTOS en caché; si ninguna fuente responde siguen los últimos leídos."""
from __future__ import annotations

import json
import logging
import re

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from services.cobranzas_banco_client import verificacion_tls

logger = logging.getLogger(__name__)

CLAVE_CACHE = "kiosco:avisos"
CLAVE_RESPALDO = "kiosco:avisos:ultimo"
_DATOS_PAGINA = re.compile(r'<script[^>]*data-page="app"[^>]*>(.*?)</script>', re.S)


def _hora(valor) -> str:
    return str(valor or "")[:5]


def _texto(valor) -> str:
    return " ".join(str(valor or "").split())


def _aviso(tipo, fecha, desde, hasta, zonas, motivo, restablecido=False) -> dict:
    return {
        "tipo": tipo,
        "restablecido": bool(restablecido),
        "fecha": str(fecha or "")[:10],
        "desde": _hora(desde),
        "hasta": _hora(hasta),
        "zonas": _texto(zonas),
        "motivo": _texto(motivo),
    }


def _de_la_web(corte: dict) -> dict:
    return _aviso(
        "emergencia" if corte.get("type") == "emergencia" else "programado",
        corte.get("execution_date"), corte.get("start_time"), corte.get("finish_time"),
        corte.get("location"), corte.get("reason"), corte.get("restored_at"),
    )


def props_de_la_web() -> dict:
    """Los datos (props de Inertia) de la página de inicio de la web; también los usa la galería."""
    respuesta = requests.get(settings.KIOSCO_AVISOS_URL, timeout=10, headers={"Accept": "text/html"})
    respuesta.raise_for_status()
    coincidencia = _DATOS_PAGINA.search(respuesta.text)
    if not coincidencia:
        raise ValueError("la página no trae data-page")
    return json.loads(coincidencia.group(1)).get("props") or {}


def leer_de_la_web() -> tuple[list[dict], list[dict]]:
    """(emergencias, programados) de la página de inicio de la web."""
    props = props_de_la_web()
    emergencias = [_de_la_web(c) for c in (props.get("emergencies") or []) if isinstance(c, dict)]
    programados = [_de_la_web(c) for c in (props.get("outages") or []) if isinstance(c, dict)]
    return emergencias, programados


def leer_de_la_api() -> list[dict]:
    """Cortes programados de hoy en adelante, ordenados por fecha y hora."""
    respuesta = requests.get(
        settings.KIOSCO_CORTES_API_URL, timeout=10,
        headers={"Authorization": settings.KIOSCO_CORTES_API_TOKEN, "Accept": "application/json"},
        verify=verificacion_tls(settings.KIOSCO_CORTES_API_VERIFY),
    )
    respuesta.raise_for_status()
    cortes = respuesta.json()
    if not isinstance(cortes, list):
        raise ValueError(f"respuesta inesperada: {str(cortes)[:200]}")
    hoy = timezone.localdate().isoformat()
    avisos = [
        _aviso("programado", c.get("fecha_ejecucion"), c.get("hora_inicio"), c.get("hora_fin"), c.get("ubicacion"), c.get("razon"))
        for c in cortes if isinstance(c, dict)
    ]
    return sorted((a for a in avisos if a["fecha"] >= hoy), key=lambda a: (a["fecha"], a["desde"]))


def _leer() -> list[dict]:
    """Emergencias primero, después los programados. Lanza si no se pudo leer nada."""
    emergencias, programados, errores = [], None, []
    if settings.KIOSCO_CORTES_API_URL:
        try:
            programados = leer_de_la_api()
        except (requests.RequestException, ValueError) as exc:
            errores.append(f"API de cortes: {exc}")
    if settings.KIOSCO_AVISOS_URL:
        try:
            emergencias, de_la_web = leer_de_la_web()
            if programados is None:
                programados = de_la_web
        except (requests.RequestException, ValueError) as exc:
            errores.append(f"web: {exc}")
    for error in errores:
        logger.warning("kiosco.avisos_no_leidos %s", error)
    if programados is None and errores:
        raise ValueError("; ".join(errores))
    return emergencias + (programados or [])


def avisos() -> list[dict]:
    """Nunca lanza: si no se pudo leer nada, los últimos leídos."""
    if not settings.KIOSCO_CORTES_API_URL and not settings.KIOSCO_AVISOS_URL:
        return []
    en_cache = cache.get(CLAVE_CACHE)
    if en_cache is not None:
        return en_cache
    try:
        lista = _leer()
    except ValueError:
        lista = cache.get(CLAVE_RESPALDO) or []
        cache.set(CLAVE_CACHE, lista, 60)  # se vuelve a probar en un minuto
        return lista
    cache.set(CLAVE_CACHE, lista, settings.KIOSCO_AVISOS_MINUTOS * 60)
    cache.set(CLAVE_RESPALDO, lista, None)
    return lista
