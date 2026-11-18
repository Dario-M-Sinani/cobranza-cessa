"""Preguntas frecuentes para el cuadro "preguntas" del kiosco.

Son las mismas del asistente de la web (cessa-website: Filament > FAQ, `GET /api/asistente/faqs`,
solo las publicadas y en su orden). Las lee el gateway (la pantalla no tiene internet) y les saca
el HTML: la pantalla solo muestra texto. Se guardan KIOSCO_PREGUNTAS_MINUTOS en caché; si la web
no responde siguen las últimas leídas."""
from __future__ import annotations

import html
import logging
import re

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

CLAVE_CACHE = "kiosco:preguntas"
CLAVE_RESPALDO = "kiosco:preguntas:ultimo"
_PARRAFOS = re.compile(r"<\s*/?\s*(p|ul|ol|div|h\d|li)\b[^>]*>", re.I)
_LINEA = re.compile(r"<\s*br\s*/?\s*>", re.I)
_VINETAS = re.compile(r"<\s*li\b[^>]*>", re.I)
_ETIQUETAS = re.compile(r"<[^>]+>")
_ESPACIOS = re.compile(r"[ \t ]+")


def texto_plano(contenido: str) -> str:
    """HTML del editor de la web -> texto: bloques (p, li, ul...) separados por línea en blanco,
    <br> como salto de línea, viñetas "• "."""
    contenido = str(contenido or "").replace("\r", "").replace("\n", " ")
    contenido = _VINETAS.sub("\n\n• ", contenido)
    contenido = _PARRAFOS.sub("\n\n", contenido)
    contenido = _LINEA.sub("\n", contenido)
    contenido = html.unescape(_ETIQUETAS.sub("", contenido))
    lineas = [_ESPACIOS.sub(" ", linea).strip() for linea in contenido.split("\n")]
    texto = "\n".join(lineas)
    return re.sub(r"\n{3,}", "\n\n", texto).strip()


_SIGLAS = ("CESSA", "AE", "SIAT", "QR", "NIT", "CI", "KW", "KWH")


def oracion(texto: str) -> str:
    """"¿CÓMO SE MIDE EL CONSUMO?" -> "¿Cómo se mide el consumo?" (las preguntas de la web están
    todas en mayúsculas). Respeta las siglas conocidas; si no está todo en mayúsculas, no la toca."""
    letras = [c for c in texto if c.isalpha()]
    if not letras or sum(c.isupper() for c in letras) < len(letras) * 0.8:
        return texto
    bajo = texto.lower()
    for i, c in enumerate(bajo):
        if c.isalpha():
            bajo = bajo[:i] + c.upper() + bajo[i + 1:]
            break
    for sigla in _SIGLAS:
        bajo = re.sub(rf"\b{sigla.lower()}\b", sigla if sigla not in ("KW", "KWH") else sigla[:1].lower() + sigla[1:].replace("WH", "Wh"), bajo)
    return bajo


def _leer() -> list[dict]:
    respuesta = requests.get(settings.KIOSCO_PREGUNTAS_URL, timeout=10, headers={"Accept": "application/json"})
    respuesta.raise_for_status()
    datos = respuesta.json()
    lista = datos.get("faqs") if isinstance(datos, dict) else None
    if not isinstance(lista, list):
        raise ValueError(f"respuesta inesperada: {str(datos)[:200]}")
    preguntas = []
    for f in lista:
        if not isinstance(f, dict):
            continue
        pregunta = oracion(" ".join(html.unescape(str(f.get("question") or "")).split()))
        respuesta_texto = texto_plano(f.get("answer"))
        if pregunta and respuesta_texto:
            preguntas.append({"pregunta": pregunta, "respuesta": respuesta_texto})
    return preguntas


def preguntas() -> list[dict]:
    """Nunca lanza: si no se pudo leer, las últimas leídas (o ninguna)."""
    if not settings.KIOSCO_PREGUNTAS_URL:
        return []
    en_cache = cache.get(CLAVE_CACHE)
    if en_cache is not None:
        return en_cache
    try:
        lista = _leer()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("kiosco.preguntas_no_leidas %s", exc)
        lista = cache.get(CLAVE_RESPALDO) or []
        cache.set(CLAVE_CACHE, lista, 300)  # se vuelve a probar en 5 minutos
        return lista
    cache.set(CLAVE_CACHE, lista, settings.KIOSCO_PREGUNTAS_MINUTOS * 60)
    cache.set(CLAVE_RESPALDO, lista, None)
    return lista
