"""Fotos del carrusel de la web (galleryHighlights de la página de inicio de cessa.com.bo, las que
se editan desde su panel) para el protector de pantalla y el fondo del kiosco.

La pantalla no tiene internet: el gateway lee la lista de la web, baja cada foto la primera vez que
se pide y la guarda en caché. Solo se bajan fotos del mismo sitio que KIOSCO_AVISOS_URL. Si la web
no responde siguen las últimas leídas."""
from __future__ import annotations

import logging
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings
from django.core.cache import cache

from .avisos import props_de_la_web

logger = logging.getLogger(__name__)

CLAVE_CACHE = "kiosco:galeria"
CLAVE_RESPALDO = "kiosco:galeria:ultima"
MAX_FOTOS = 8
MAX_BYTES = 4 * 1024 * 1024


def _url_permitida(url: str) -> bool:
    base = urlparse(settings.KIOSCO_AVISOS_URL)
    destino = urlparse(url)
    return destino.scheme in ("http", "https") and destino.netloc == base.netloc


def _leer() -> list[dict]:
    fotos = []
    for foto in props_de_la_web().get("galleryHighlights") or []:
        if not isinstance(foto, dict) or not isinstance(foto.get("id"), int) or not foto.get("url"):
            continue
        url = urljoin(settings.KIOSCO_AVISOS_URL, str(foto["url"]))
        if _url_permitida(url):
            fotos.append({"id": foto["id"], "url": url})
    return fotos[:MAX_FOTOS]


def fotos() -> list[dict]:
    """[{id, url}] -- nunca lanza: si no se pudo leer la web, las últimas leídas."""
    if not settings.KIOSCO_AVISOS_URL:
        return []
    en_cache = cache.get(CLAVE_CACHE)
    if en_cache is not None:
        return en_cache
    try:
        lista = _leer()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("kiosco.galeria_no_leida %s", exc)
        lista = cache.get(CLAVE_RESPALDO) or []
        cache.set(CLAVE_CACHE, lista, 300)  # se vuelve a probar en 5 minutos
        return lista
    cache.set(CLAVE_CACHE, lista, settings.KIOSCO_GALERIA_MINUTOS * 60)
    cache.set(CLAVE_RESPALDO, lista, None)
    return lista


def foto(id_foto: int) -> tuple[bytes, str] | None:
    """(contenido, tipo) de una foto de la lista, o None si no está o no se pudo bajar."""
    clave = f"kiosco:galeria:foto:{id_foto}"
    guardada = cache.get(clave)
    if guardada is not None:
        return guardada
    url = next((f["url"] for f in fotos() if f["id"] == id_foto), None)
    if url is None:
        return None
    try:
        respuesta = requests.get(url, timeout=15)
        respuesta.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("kiosco.foto_no_bajada id=%s error=%s", id_foto, exc)
        return None
    tipo = (respuesta.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not tipo.startswith("image/") or len(respuesta.content) > MAX_BYTES:
        logger.warning("kiosco.foto_rechazada id=%s tipo=%s bytes=%s", id_foto, tipo, len(respuesta.content))
        return None
    guardada = (respuesta.content, tipo)
    cache.set(clave, guardada, 24 * 3600)
    return guardada
