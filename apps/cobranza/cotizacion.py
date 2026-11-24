"""Cotización del dólar: la del SIIC (COTIZA, con la que el SIIC cierra las cajas: es la que manda) junto a
la oficial del BCB (referencia). Queda una fila por día en CotizacionDolar como constancia de qué valía
cada una y cuándo se leyó.

- La del SIIC se lee de la API SIIC (SIIC_DEUDA_BASE_URL, con su token: la misma tabla COTIZA con la
  que esa API cierra las cajas); sin ella configurada, con el token de la Nest de quien mira. Se vuelve
  a leer cada pocos minutos: si a la mañana todavía no estaba cargada, aparece en cuanto la carguen.
- La del BCB la lee una tarea cada hora (apps/cobranza/tasks.py) y, si falta, la primera consulta.
- Carga automática (COTIZACION_CARGA_AUTOMATICA): la misma tarea carga en el SIIC, con el valor del BCB,
  cada día de su vigencia (de hoy en adelante) que todavía no tenga cotización. Sin nadie que confirme, pero
  con dos fuentes: el valor tiene que venir de bcb.gob.bo (no del respaldo factura.bo) y coincidir con el de
  COTIZACION_CONTRASTE_URL vigente hoy. Si no, no carga nada y queda la alerta.
  La API SIIC solo inserta si el día no tiene, y solo donde está habilitada (hoy, la copia de test
  RESTAURA; ADMINDTA, la de prod, la sigue cargando DTIC hasta que acuerden).
- Corrección: supervisor/admin cambia el valor de un día con un motivo; la API SIIC solo lo cambia si
  todavía tiene el valor que vio quien corrige.
- Cada carga y corrección queda en CotizacionCambio (y en la auditoría): día, valor anterior y nuevo,
  de dónde salió, quién (nadie = la tarea) y por qué.

Alertas para la supervisora: sin cotización en el SIIC hoy (las cajas no van a poder cerrar), carga
automática fallida y cotización del SIIC distinta de la oficial.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.auditoria.models import LogAuditoria
from apps.usuarios.models import Usuario
from services.cobranzas_banco_client import CajeraCobranzasClient, CobranzasBancoError
from services.deuda_client import (
    CotizacionNoCargada,
    DeudaClientError,
    cargar_cotizacion_siic,
    corregir_cotizacion_siic,
    cotizacion_siic,
)
from services.tipo_cambio import TipoCambioError, leer_contraste, tipo_cambio_oficial

from .models import CotizacionCambio, CotizacionDolar

RELEER_SIIC_CADA = timedelta(minutes=10)
RELEER_BCB_CADA = timedelta(hours=1)
TOLERANCIA = Decimal("0.005")
# Lo mismo que acepta la API SIIC: días a cargar o corregir alrededor de hoy.
VENTANA_DIAS = 7

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}


def _del_dia(fecha=None) -> CotizacionDolar:
    fila, _ = CotizacionDolar.objects.get_or_create(fecha=fecha or timezone.localdate())
    return fila


def actualizar_bcb(fila: CotizacionDolar | None = None, forzar: bool = False) -> CotizacionDolar:
    fila = fila or _del_dia()
    ahora = timezone.now()
    if not forzar and fila.bcb is not None and fila.bcb_leida_en and ahora - fila.bcb_leida_en < RELEER_BCB_CADA:
        return fila
    try:
        oficial = tipo_cambio_oficial()
    except TipoCambioError as exc:
        fila.bcb_error = str(exc)[:500]
        fila.save(update_fields=["bcb_error", "actualizada_en"])
        return fila
    fila.bcb, fila.bcb_vigencia, fila.bcb_fuente = oficial.valor, oficial.vigencia, oficial.fuente
    fila.bcb_leida_en, fila.bcb_error = ahora, ""
    fila.save()
    return fila


def _por_api_siic() -> bool:
    return bool(settings.SIIC_DEUDA_BASE_URL)


def actualizar_siic(fila: CotizacionDolar, usuario=None, forzar: bool = False) -> CotizacionDolar:
    if not _por_api_siic() and getattr(usuario, "origen", "") != Usuario.Origen.NEST:
        return fila  # ni API SIIC ni token de la Nest: no hay cómo preguntarle al SIIC
    ahora = timezone.now()
    if not forzar and fila.siic_leida_en and ahora - fila.siic_leida_en < RELEER_SIIC_CADA and not fila.siic_error:
        return fila
    if not forzar and not cache.add(f"cotizacion-siic-{fila.fecha}", 1, timeout=30):  # varias pantallas: una consulta
        return fila
    fecha = fila.fecha.strftime("%Y%m%d")
    try:
        datos = cotizacion_siic(fecha) if _por_api_siic() else CajeraCobranzasClient(usuario).cotizacion_dolar(fecha)
    except (CobranzasBancoError, DeudaClientError) as exc:
        fila.siic_error = str(exc)[:500]
        fila.save(update_fields=["siic_error", "actualizada_en"])
        return fila
    valor = datos.get("cotizacion")
    fila.siic = Decimal(str(valor)) if valor not in (None, "") else None
    fila.siic_leida_en, fila.siic_error = ahora, ""
    fila.save()
    return fila


# ---------------------------------------------------------------- vigencia del BCB

def _cercano(dia: int, hoy: date) -> date | None:
    """La fecha con ese número de día más cercana a hoy (mes anterior, este o el siguiente)."""
    candidatos = []
    for delta in (-1, 0, 1):
        total = hoy.year * 12 + hoy.month - 1 + delta
        try:
            candidatos.append(date(total // 12, total % 12 + 1, dia))
        except ValueError:
            pass
    return min(candidatos, key=lambda d: abs((d - hoy).days), default=None)


def dias_de_vigencia(vigencia: str, hoy: date | None = None) -> list[date]:
    """Los días que cubre la vigencia del BCB. Entiende "del sábado 14 de noviembre de 2026 al lunes 16 de
    noviembre de 2026" (bcb.gob.bo), "del lunes 16 al martes 17" (factura.bo, sin mes: los más cercanos a
    hoy) y un solo día. Lo que no entiende, o un rango raro (al revés o de más de una semana) → []."""
    hoy = hoy or timezone.localdate()
    texto = (vigencia or "").lower()
    completas = []
    for d, mes, anio in re.findall(r"(\d{1,2}) de ([a-záéíóú]+) de (\d{4})", texto):
        if mes not in MESES:
            return []
        try:
            completas.append(date(int(anio), MESES[mes], int(d)))
        except ValueError:
            return []
    if completas:
        desde, hasta = completas[0], completas[-1]
    else:
        numeros = [int(n) for n in re.findall(r"\b(\d{1,2})\b", texto)]
        if not numeros or len(numeros) > 2:
            return []
        desde = _cercano(numeros[0], hoy)
        if desde is None:
            return []
        hasta = desde
        if len(numeros) == 2:
            hasta = next((desde + timedelta(days=i) for i in range(8) if (desde + timedelta(days=i)).day == numeros[1]), None)
            if hasta is None:
                return []
    if hasta < desde or (hasta - desde).days > 7:
        return []
    return [desde + timedelta(days=i) for i in range((hasta - desde).days + 1)]


# ---------------------------------------------------------------- escrituras en el SIIC (con registro)

def _registrar(fecha: date, accion: str, anterior, nuevo: Decimal, origen: str, tabla: str, usuario=None, motivo: str = ""):
    cambio = CotizacionCambio.objects.create(
        fecha=fecha, accion=accion, valor_anterior=anterior, valor_nuevo=nuevo, origen=origen[:255],
        motivo=motivo, usuario=usuario, tabla=tabla[:40],
    )
    detalle = f"Bs {anterior:.4f} → {nuevo:.4f}" if anterior is not None else f"Bs {nuevo:.4f}"
    LogAuditoria.objects.create(
        usuario=usuario,
        accion=f"Cotización del dólar {fecha:%d/%m/%Y} en el SIIC ({cambio.get_accion_display()}): {detalle} · {origen}"
        + (f" · motivo: {motivo}" if motivo else ""),
        entidad_afectada=f"CotizacionDolar:{fecha.isoformat()}",
    )
    return cambio


def carga_automatica() -> list[dict]:
    """La tarea de cada hora, después de leer el BCB: carga en el SIIC, con el valor del BCB, cada día de la
    vigencia (de hoy hasta VENTANA_DIAS adelante) que todavía no tenga. Devuelve lo que hizo (para el log)."""
    if not (settings.COTIZACION_CARGA_AUTOMATICA and _por_api_siic()):
        return []
    hoy = timezone.localdate()
    bcb = actualizar_bcb(_del_dia(hoy))
    if bcb.bcb is None or bcb.bcb_error:
        return []
    dias = [d for d in dias_de_vigencia(bcb.bcb_vigencia, hoy) if hoy <= d <= hoy + timedelta(days=VENTANA_DIAS)]
    if not dias:
        if not bcb.carga_error.startswith("Vigencia"):
            bcb.carga_error = f"Vigencia del BCB que no se entiende o que no incluye hoy: {bcb.bcb_vigencia!r}. No se cargó nada."
            bcb.save(update_fields=["carga_error", "actualizada_en"])
        return []
    faltan = []
    for dia in dias:
        fila = actualizar_siic(_del_dia(dia), forzar=True)
        if fila.siic_error or fila.siic is not None:
            continue  # no se pudo leer (se reintenta en una hora) o ya tiene
        faltan.append((dia, fila))
    if not faltan:
        _limpiar_aviso(bcb)
        return []
    confirmada, problema = _verificar(bcb, hoy)
    if problema:
        mensaje = f"Verificación: {problema} No se cargó nada; se vuelve a intentar en una hora."[:500]
        if bcb.carga_error != mensaje:
            bcb.carga_error = mensaje
            bcb.save(update_fields=["carga_error", "actualizada_en"])
        return []
    origen = f"{bcb.bcb_fuente or 'BCB'}, vigencia {bcb.bcb_vigencia}, {confirmada}"
    hecho = []
    for dia, fila in faltan:
        try:
            r = cargar_cotizacion_siic(dia.strftime("%Y%m%d"), f"{bcb.bcb:.4f}")
        except DeudaClientError as exc:  # incluye CotizacionNoCargada (no habilitada, ya había otra...)
            mensaje = f"No se pudo cargar en el SIIC: {exc}"[:500]
            if fila.carga_error != mensaje:
                fila.carga_error = mensaje
                fila.save(update_fields=["carga_error", "actualizada_en"])
            continue
        fila.siic = Decimal(str(r.get("cotizacion") or bcb.bcb))
        fila.siic_leida_en, fila.siic_error, fila.carga_error = timezone.now(), "", ""
        if fila.bcb is None:  # el día futuro también queda con su valor oficial de referencia
            fila.bcb, fila.bcb_vigencia, fila.bcb_fuente, fila.bcb_leida_en = bcb.bcb, bcb.bcb_vigencia, bcb.bcb_fuente, bcb.bcb_leida_en
        fila.save()
        if r.get("insertada"):
            _registrar(dia, CotizacionCambio.Accion.CARGA_AUTOMATICA, None, fila.siic, origen, str(r.get("tabla") or ""))
            hecho.append({"fecha": dia.isoformat(), "cotizacion": f"{fila.siic:.4f}"})
    _limpiar_aviso(bcb)
    return hecho


def _limpiar_aviso(bcb: CotizacionDolar):
    """Saca el aviso de vigencia o verificación de una vuelta anterior (los rechazos del SIIC quedan)."""
    bcb.refresh_from_db()
    if bcb.carga_error.startswith(("Vigencia", "Verificación")):
        bcb.carga_error = ""
        bcb.save(update_fields=["carga_error", "actualizada_en"])


def _verificar(bcb: CotizacionDolar, hoy: date) -> tuple[str, str]:
    """Antes de escribir en el SIIC: (constancia para el registro, "") si se puede cargar, o ("", motivo)."""
    if bcb.bcb_fuente != "bcb.gob.bo":
        return "", (f"la oficial de hoy se leyó de {bcb.bcb_fuente or 'otra fuente'} porque bcb.gob.bo no respondió; "
                    "al SIIC solo se carga la leída del BCB.")
    if not settings.COTIZACION_CONTRASTE_URL:
        return "sin segunda fuente (COTIZACION_CONTRASTE_URL vacío)", ""
    try:
        otra = leer_contraste()
    except TipoCambioError as exc:
        return "", f"no se pudo confirmar el valor con la segunda fuente ({exc})."
    if not otra.desde <= hoy <= otra.hasta:
        return "", (f"{otra.fuente} no tiene la cotización vigente hoy (la suya va del {otra.desde:%d/%m/%Y} "
                    f"al {otra.hasta:%d/%m/%Y}).")
    if abs(otra.valor - bcb.bcb) >= Decimal("0.00005"):
        return "", f"bcb.gob.bo da Bs {bcb.bcb:.4f} y {otra.fuente} Bs {otra.valor:.4f}: no coinciden."
    return f"confirmada con {otra.fuente} (Bs {otra.valor:.4f})", ""


class CambioRechazado(Exception):
    def __init__(self, mensaje: str, status: int = 409):
        super().__init__(mensaje)
        self.status = status


def corregir(usuario, fecha: date, valor, motivo: str) -> dict:
    """Supervisor/admin: deja `valor` como cotización de `fecha` en el SIIC. Si ese día no tiene, la carga; si
    tiene otra, la corrige (solo si todavía vale la que se acaba de leer). Motivo obligatorio."""
    if not _por_api_siic():
        raise CambioRechazado("Cambiar la cotización necesita la API SIIC (SIIC_DEUDA_BASE_URL) configurada.", 503)
    motivo = (motivo or "").strip()
    if len(motivo) < 5:
        raise CambioRechazado("Indique el motivo del cambio (al menos 5 caracteres).", 400)
    try:
        nuevo = Decimal(str(valor).strip().replace(",", ".")).quantize(Decimal("0.0001"))
    except (InvalidOperation, ValueError):
        raise CambioRechazado("Valor inválido.", 400) from None
    if not Decimal("1") <= nuevo <= Decimal("100"):
        raise CambioRechazado("La cotización tiene que estar entre 1 y 100.", 400)
    hoy = timezone.localdate()
    if abs((fecha - hoy).days) > VENTANA_DIAS:
        raise CambioRechazado(f"Solo se puede cambiar la cotización de hasta {VENTANA_DIAS} días antes o después de hoy.", 400)
    if not cache.add(f"cotizacion-cambio-{fecha}", 1, timeout=60):
        raise CambioRechazado("Hay otro cambio de esta cotización en curso: espere un momento.")
    try:
        fila = actualizar_siic(_del_dia(fecha), forzar=True)
        if fila.siic_error:
            raise CambioRechazado(f"No se pudo leer la cotización actual del SIIC: {fila.siic_error}", 502)
        anterior = fila.siic
        if anterior is not None and abs(anterior - nuevo) < Decimal("0.00005"):
            raise CambioRechazado(f"La cotización del {fecha:%d/%m/%Y} ya es Bs {nuevo:.4f}.", 400)
        ymd = fecha.strftime("%Y%m%d")
        try:
            if anterior is None:
                r = cargar_cotizacion_siic(ymd, f"{nuevo:.4f}")
                accion = CotizacionCambio.Accion.CARGA_MANUAL
            else:
                r = corregir_cotizacion_siic(ymd, f"{anterior:.4f}", f"{nuevo:.4f}")
                accion = CotizacionCambio.Accion.CORRECCION
        except CotizacionNoCargada as exc:
            raise CambioRechazado(str(exc), exc.status) from exc
        except DeudaClientError as exc:
            raise CambioRechazado(f"El SIIC no respondió: {exc}", 502) from exc
        fila.siic = Decimal(str(r.get("cotizacion") or nuevo))
        fila.siic_leida_en, fila.siic_error, fila.carga_error = timezone.now(), "", ""
        fila.save()
        if accion == CotizacionCambio.Accion.CORRECCION or r.get("insertada"):
            _registrar(fecha, accion, anterior, fila.siic, "manual", str(r.get("tabla") or ""), usuario=usuario, motivo=motivo)
        return estado(fila)
    finally:
        cache.delete(f"cotizacion-cambio-{fecha}")


# ---------------------------------------------------------------- lo que ve el panel

def _cambio(c: CotizacionCambio) -> dict:
    return {
        "fecha": c.fecha.isoformat(),
        "accion": c.accion,
        "accion_texto": c.get_accion_display(),
        "valor_anterior": f"{c.valor_anterior:.4f}" if c.valor_anterior is not None else None,
        "valor_nuevo": f"{c.valor_nuevo:.4f}",
        "origen": c.origen,
        "motivo": c.motivo,
        "usuario": c.usuario.username if c.usuario else None,
        "tabla": c.tabla,
        "creado_en": c.creado_en,
    }


def estado(fila: CotizacionDolar, cambios: list[CotizacionCambio] | None = None) -> dict:
    alertas = []
    if fila.siic_leida_en and fila.siic is None:
        alertas.append({
            "tipo": "sin_siic",
            "mensaje": "Hoy no hay cotización del dólar cargada en el SIIC: las cajas no van a poder cerrar hasta que se cargue.",
        })
    if fila.carga_error and fila.siic is None:
        alertas.append({"tipo": "carga_fallida", "mensaje": f"La carga automática no pudo: {fila.carga_error}"})
    if fila.siic is not None and fila.bcb is not None and abs(fila.siic - fila.bcb) >= TOLERANCIA:
        alertas.append({
            "tipo": "diferencia",
            "mensaje": f"La cotización del SIIC (Bs {fila.siic:.2f}) no coincide con la oficial del BCB (Bs {fila.bcb:.2f}).",
        })
    if cambios is None:
        cambios = list(CotizacionCambio.objects.select_related("usuario").filter(fecha=fila.fecha))
    return {
        "fecha": fila.fecha.isoformat(),
        "siic": f"{fila.siic:.4f}" if fila.siic is not None else None,
        "siic_leida_en": fila.siic_leida_en,
        "siic_error": fila.siic_error,
        "bcb": f"{fila.bcb:.4f}" if fila.bcb is not None else None,
        "bcb_vigencia": fila.bcb_vigencia,
        "bcb_fuente": fila.bcb_fuente,
        "bcb_leida_en": fila.bcb_leida_en,
        "bcb_error": fila.bcb_error,
        "carga_error": fila.carga_error,
        "carga_automatica": bool(settings.COTIZACION_CARGA_AUTOMATICA and _por_api_siic()),
        "editable": bool(settings.COTIZACION_EDITABLE),
        "cambios": [_cambio(c) for c in cambios],  # del más nuevo al más viejo
        "alertas": alertas,
    }


def de_hoy(usuario) -> dict:
    fila = _del_dia()
    actualizar_siic(fila, usuario)
    if fila.bcb is None and not fila.bcb_error:
        actualizar_bcb(fila)
    return estado(fila)


def historial(desde, hasta) -> list[dict]:
    cambios: dict[date, list[CotizacionCambio]] = {}
    for c in CotizacionCambio.objects.select_related("usuario").filter(fecha__gte=desde, fecha__lte=hasta):
        cambios.setdefault(c.fecha, []).append(c)
    filas = CotizacionDolar.objects.filter(fecha__gte=desde, fecha__lte=hasta).order_by("-fecha")
    return [estado(f, cambios.get(f.fecha, [])) for f in filas]
