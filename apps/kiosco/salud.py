"""Estado de cada pantalla antes de cobrar: si algo de lo que hace falta para cobrar no funciona,
la pantalla no ofrece pagar; si solo falla algo secundario, cobra pero lo avisa antes del QR.

| Revisión  | Si falla                                                          |
|-----------|-------------------------------------------------------------------|
| base      | ROJO: no se puede registrar el pago                               |
| sip       | ROJO: no se puede generar el QR                                   |
| cobranzas | ROJO: sin login no se consulta la deuda (el kiosco la lee de ahí) |
| worker    | ROJO: nadie detecta el pago ni factura si la pantalla se cierra   |
| caja      | AMARILLO: caja cerrada o fuera de horario, la factura sale después |
| impresora | AMARILLO: sin ticket (solo con impresora de red, KIOSCO_IMPRESORAS) |

La pantalla pregunta cada minuto (GET /api/kiosco/estado/), y con eso el gateway sabe que está
prendida. `vigilar()` corre cada minuto en Celery y avisa por Telegram cuando cambian los
problemas de una pantalla (confirmado en dos pasadas seguidas, para no avisar por un corte de
segundos) o cuando una pantalla deja de preguntar en horario.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils import timezone

from services.alertas import notificar

from . import impresora, services
from .models import PantallaKiosco

logger = logging.getLogger(__name__)

VERDE, AMARILLO, ROJO = "verde", "amarillo", "rojo"
_GRAVEDAD = {VERDE: 0, AMARILLO: 1, ROJO: 2}

MENSAJE_FUERA_DE_SERVICIO = (
    "Por ahora este cajero no puede recibir pagos. Intenta de nuevo en unos minutos o acércate a ventanilla."
)
AVISO_SIN_TICKET = (
    "Hoy no podemos imprimir tu recibo. Tu factura se emite igual: descárgala en la app de CESSA o en "
    "www.cessa.com.bo."
)
AVISO_FACTURA_DESPUES = "Tu pago queda registrado al instante; la factura se emite cuando abra la caja."

# La tarea `vigilar_kioscos` corre cada minuto: más de esto sin pasar = el worker o el beat no corren.
MINUTOS_WORKER = 3
SEGUNDOS_CACHE = 30


TITULOS = {
    "base": "Base de datos del cajero",
    "cobranzas": "Sistema de cobranzas",
    "caja": "Caja del SIIC",
    "sip": "QR del banco (SIP)",
    "worker": "Revisión automática de pagos",
    "impresora": "Impresora",
}
_ORDEN = list(TITULOS)


@dataclass
class Revision:
    clave: str
    estado: str = VERDE
    # Resultado corto que se puede mostrar en la pantalla de arranque ("en línea", "sin papel").
    texto: str = "en línea"
    # Para Telegram, el log y la pantalla técnica con PIN (nunca a la vista del cliente).
    detalle: str = ""
    # Para el cliente, antes del QR (solo en AMARILLO).
    aviso: str = ""
    # Problema que se avisa aunque se pueda cobrar normal (queda poco papel).
    alerta: bool = False
    # Identifica el problema sin el texto variable del error: con esto se compara entre pasadas.
    codigo: str = ""
    # False para lo esperado (fuera del horario de caja): se muestra, pero no se avisa por Telegram.
    se_avisa: bool = True

    @property
    def es_problema(self) -> bool:
        return self.estado != VERDE or self.alerta


def _rango_horario() -> tuple[time, time] | None:
    try:
        desde, hasta = settings.KIOSCO_HORARIO_CAJA.split("-")
        return time.fromisoformat(desde.strip()), time.fromisoformat(hasta.strip())
    except (ValueError, AttributeError):
        return None


def en_horario(ahora: datetime | None = None) -> bool:
    rango = _rango_horario()
    if rango is None:
        return True
    hora = timezone.localtime(ahora or timezone.now()).time()
    return rango[0] <= hora <= rango[1]


def _base() -> Revision:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 -- cualquier falla de la base es "no se puede cobrar"
        return Revision("base", ROJO, "sin respuesta", f"Base de datos del gateway: {exc}", codigo="base")
    return Revision("base")


def _sip() -> Revision:
    cliente = services.get_mc4_client()
    verificar = getattr(cliente, "verificar_conexion", None)
    try:
        if verificar:
            verificar()
    except Exception as exc:  # noqa: BLE001
        return Revision("sip", ROJO, "sin respuesta", f"SIP (QR del banco) no responde o rechaza el login: {exc}",
                        codigo="sip")
    return Revision("sip")


def _cobranzas() -> list[Revision]:
    try:
        estado_caja = services.get_cobranzas_banco_client().estado_caja()
    except Exception as exc:  # noqa: BLE001
        return [Revision("cobranzas", ROJO, "sin respuesta",
                         f"Sistema de cobranzas (deuda y factura) sin respuesta o sin login: {exc}", codigo="cobranzas")]
    revisiones = [Revision("cobranzas")]
    if estado_caja not in ("ABIERTA", "SIN_ABRIR"):
        revisiones.append(Revision(
            "caja", AMARILLO, f"{estado_caja.lower()}: la factura sale después",
            f"La caja del SIIC que factura el kiosco está {estado_caja}: se cobra y se factura después.",
            aviso=AVISO_FACTURA_DESPUES, codigo=f"caja:{estado_caja}",
        ))
    elif not en_horario():
        revisiones.append(Revision(
            "caja", AMARILLO, "fuera de horario: la factura sale después",
            f"Fuera del horario de caja ({settings.KIOSCO_HORARIO_CAJA}): se cobra y se factura después.",
            aviso=AVISO_FACTURA_DESPUES, codigo="caja:fuera_de_horario", se_avisa=False,
        ))
    else:
        revisiones.append(Revision("caja", texto="abierta" if estado_caja == "ABIERTA" else "se abre sola con el primer pago"))
    return revisiones


def _impresora(nombre: str) -> Revision | None:
    direccion = settings.KIOSCO_IMPRESORAS.get(nombre)
    if not direccion:
        return None
    estado = impresora.estado(direccion)
    corto = impresora.DESCRIPCION.get(estado, estado)
    texto = f"Impresora ({direccion}): {corto}"
    if estado == impresora.OK:
        return Revision("impresora", texto=corto, detalle=texto)
    if estado == impresora.POCO_PAPEL:
        return Revision("impresora", VERDE, corto, texto, alerta=True, codigo="impresora:poco_papel")
    return Revision("impresora", AMARILLO, corto, texto, aviso=AVISO_SIN_TICKET, codigo=f"impresora:{estado}")


def _worker(pantalla: PantallaKiosco | None, ahora: datetime) -> Revision:
    if pantalla is None:
        return Revision("worker")
    referencia = pantalla.revisada_en or pantalla.creada_en
    if referencia and ahora - referencia > timedelta(minutes=MINUTOS_WORKER):
        minutos = int((ahora - referencia).total_seconds() // 60)
        return Revision("worker", ROJO, "detenida", f"El worker de Celery no corre (última revisión hace {minutos} min): "
                        "nadie detectaría el pago si la pantalla se cierra.", codigo="worker")
    return Revision("worker", texto="funcionando")


def revisar(nombre: str, pantalla: PantallaKiosco | None = None, usar_cache: bool = True) -> list[Revision]:
    """Todas las revisiones de la pantalla `nombre`. Lo que comparten todas las pantallas (base,
    SIP, cobranzas) y la impresora se guardan SEGUNDOS_CACHE para no consultar en cada pedido."""
    comunes = cache.get("kiosco:salud:comunes") if usar_cache else None
    if comunes is None:
        comunes = [_base(), _sip(), *_cobranzas()]
        cache.set("kiosco:salud:comunes", comunes, SEGUNDOS_CACHE)

    clave_impresora = f"kiosco:salud:impresora:{nombre}"
    de_impresora = cache.get(clave_impresora) if usar_cache else None
    if de_impresora is None:
        revision = _impresora(nombre)
        de_impresora = [revision] if revision else []
        cache.set(clave_impresora, de_impresora, SEGUNDOS_CACHE)

    revisiones = [*comunes, *de_impresora, _worker(pantalla, timezone.now())]
    for r in revisiones:
        if r.es_problema:
            logger.warning("kiosco.salud pantalla=%s %s=%s %s", nombre, r.clave, r.estado, r.detalle)
    return revisiones


def gravedad(revisiones: list[Revision]) -> str:
    return max((r.estado for r in revisiones), key=_GRAVEDAD.__getitem__, default=VERDE)


def _lista(revisiones: list[Revision], con_detalle: bool) -> list[dict]:
    """Las revisiones en el orden de TITULOS, para la pantalla de arranque (y con el detalle técnico
    para la pantalla con PIN). Sin impresora de red, figura como no revisable."""
    ordenadas = sorted(revisiones, key=lambda r: _ORDEN.index(r.clave) if r.clave in _ORDEN else len(_ORDEN))
    filas = [{"clave": r.clave, "titulo": TITULOS.get(r.clave, r.clave), "estado": r.estado, "texto": r.texto,
              **({"detalle": r.detalle} if con_detalle else {})} for r in ordenadas]
    if not any(r.clave == "impresora" for r in revisiones):
        filas.append({"clave": "impresora", "titulo": TITULOS["impresora"], "estado": "sin_revisar",
                      "texto": "imprime por el navegador: no se puede revisar el papel",
                      **({"detalle": "Sin KIOSCO_IMPRESORAS para esta pantalla."} if con_detalle else {})})
    return filas


def para_pantalla(nombre: str, pantalla: PantallaKiosco | None = None, usar_cache: bool = True,
                  con_detalle: bool = False) -> dict:
    """Lo que ve la pantalla: el color, el mensaje si no se puede cobrar, los avisos para el cliente
    y la lista de revisiones de la pantalla de arranque. El detalle técnico solo con PIN."""
    revisiones = revisar(nombre, pantalla, usar_cache=usar_cache)
    estado = gravedad(revisiones)
    avisos = list(dict.fromkeys(r.aviso for r in revisiones if r.estado == AMARILLO and r.aviso))
    revision_impresora = next((r for r in revisiones if r.clave == "impresora"), None)
    return {
        "estado": estado,
        "mensaje": MENSAJE_FUERA_DE_SERVICIO if estado == ROJO else "",
        "avisos": avisos,
        # La pantalla no ofrece imprimir si la impresora de red no está lista.
        "puede_imprimir": revision_impresora is None or revision_impresora.estado == VERDE,
        "revisiones": _lista(revisiones, con_detalle),
    }


def para_tecnico(nombre: str, pantalla: PantallaKiosco) -> dict:
    """Pantalla del personal (con PIN): todo revisado de nuevo, sin caché, con el detalle técnico."""
    datos = para_pantalla(nombre, pantalla, usar_cache=False, con_detalle=True)
    pantalla.refresh_from_db()
    return {
        **datos,
        "pantalla": nombre,
        "impresora": settings.KIOSCO_IMPRESORAS.get(nombre, ""),
        "horario_caja": settings.KIOSCO_HORARIO_CAJA,
        "ultima_revision_automatica": _hora(pantalla.revisada_en) if pantalla.revisada_en else "",
        "problemas_avisados": pantalla.problemas_avisados,
    }


def registrar_contacto(nombre: str) -> PantallaKiosco:
    pantalla, _ = PantallaKiosco.objects.get_or_create(nombre=nombre)
    PantallaKiosco.objects.filter(pk=pantalla.pk).update(ultimo_contacto=timezone.now())
    return pantalla


# ---------------------------------------------------------------- vigilancia (Celery, cada minuto)

def _hora(momento: datetime) -> str:
    return timezone.localtime(momento).strftime("%H:%M")


def _para_avisar(revisiones: list[Revision]) -> list[Revision]:
    return [r for r in revisiones if r.es_problema and r.se_avisa]


def _mensaje(nombre: str, revisiones: list[Revision]) -> tuple[str, str]:
    problemas = _para_avisar(revisiones)
    if not problemas:
        return f"🟢 Kiosco {nombre}: todo normal de nuevo", "Puede cobrar e imprimir con normalidad."
    estado = gravedad(problemas)
    if estado == ROJO:
        titulo = f"🔴 Kiosco {nombre}: fuera de servicio"
        pie = "La pantalla no ofrece pagar hasta que se resuelva."
    elif estado == AMARILLO:
        titulo = f"🟡 Kiosco {nombre}: cobra con avisos"
        pie = "La pantalla sigue cobrando y le avisa al cliente antes del QR."
    else:
        titulo = f"🟡 Kiosco {nombre}: revisar"
        pie = "Por ahora cobra con normalidad."
    return titulo, "\n".join(f"- {r.detalle}" for r in problemas) + f"\n\n{pie}"


def vigilar() -> int:
    """Revisa todas las pantallas de KIOSCO_TOKENS y avisa los cambios. Devuelve cuántos avisos salieron."""
    ahora = timezone.now()
    avisos = 0
    for nombre in settings.KIOSCO_TOKENS:
        pantalla, _ = PantallaKiosco.objects.get_or_create(nombre=nombre)
        pantalla.revisada_en = ahora
        # El worker es justamente quien corre esto: no se revisa a sí mismo.
        revisiones = [r for r in revisar(nombre, usar_cache=False) if r.clave != "worker"]
        if (
            pantalla.ultimo_contacto
            and en_horario(ahora)
            and ahora - pantalla.ultimo_contacto > timedelta(minutes=settings.KIOSCO_MINUTOS_SIN_CONTACTO)
        ):
            revisiones.append(Revision(
                "pantalla", ROJO, "sin contacto",
                f"La pantalla no da señales desde las {_hora(pantalla.ultimo_contacto)} "
                "(apagada, sin red o el navegador cerrado).", codigo="pantalla:sin_contacto",
            ))

        problemas = ",".join(sorted(r.codigo for r in _para_avisar(revisiones)))[:500]
        if problemas == pantalla.problemas_avisados:
            pantalla.problemas_nuevos, pantalla.veces_nuevos = "", 0
        elif problemas == pantalla.problemas_nuevos:
            pantalla.veces_nuevos += 1
        else:
            pantalla.problemas_nuevos, pantalla.veces_nuevos = problemas, 1

        if pantalla.problemas_nuevos == problemas and problemas != pantalla.problemas_avisados and pantalla.veces_nuevos >= 2:
            if notificar(*_mensaje(nombre, revisiones)):
                avisos += 1
            pantalla.problemas_avisados = problemas
            pantalla.problemas_nuevos, pantalla.veces_nuevos = "", 0

        pantalla.save(update_fields=["revisada_en", "problemas_avisados", "problemas_nuevos", "veces_nuevos"])
    return avisos
