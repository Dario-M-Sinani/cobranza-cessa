"""Lista y reportes de los QR del panel, como listaQr.xhtml de la app Java de cobranzas: filtros por
fechas, estado, cajero, clientes y N° de QR; totales por día; detalle; exportación a TXT.

Los PDF los arma el panel como páginas imprimibles (el navegador los guarda en PDF), con los datos
de acá. La cajera ve solo sus QR (el queryset ya llega filtrado).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import TransaccionQR

POR_PAGINA = 25
MAX_DIAS = 366
_CLIENTES = re.compile(r"^\s*\d+\s*(,\s*\d+\s*)*$")  # "101194, 101591"


class FiltroInvalido(ValueError):
    pass


@dataclass
class Filtros:
    desde: date
    hasta: date
    estado: str = ""
    cajero: str = ""
    clientes: tuple[str, ...] = ()
    qr: int | None = None


def leer_filtros(params) -> Filtros:
    hoy = timezone.localdate()
    try:
        desde = date.fromisoformat(params.get("desde") or hoy.isoformat())
        hasta = date.fromisoformat(params.get("hasta") or desde.isoformat())
    except ValueError:
        raise FiltroInvalido("Fechas inválidas (AAAA-MM-DD).")
    if hasta < desde:
        raise FiltroInvalido("La fecha final es anterior a la inicial.")
    if (hasta - desde).days > MAX_DIAS:
        raise FiltroInvalido("El rango no puede pasar de un año.")

    estado = (params.get("estado") or "").strip()
    if estado and estado not in TransaccionQR.Estado.values:
        raise FiltroInvalido("Estado inválido.")

    clientes = (params.get("clientes") or "").strip()
    if clientes and not _CLIENTES.match(clientes):
        raise FiltroInvalido("Los N° de cliente van separados por coma, solo números.")

    qr = (params.get("qr") or "").strip()
    if qr and not qr.isdigit():
        raise FiltroInvalido("El N° de QR es un número.")

    return Filtros(
        desde=desde,
        hasta=hasta,
        estado=estado,
        cajero=(params.get("cajero") or "").strip(),
        clientes=tuple(c.strip() for c in clientes.split(",")) if clientes else (),
        qr=int(qr) if qr else None,
    )


def filtrar(qs, f: Filtros):
    """Por la fecha en que se generó el QR (hora de Bolivia), como la lista de la app Java."""
    qs = qs.filter(creado_en__date__gte=f.desde, creado_en__date__lte=f.hasta)
    if f.estado:
        qs = qs.filter(estado=f.estado)
    if f.cajero:
        qs = qs.filter(usuario__username__iexact=f.cajero)
    if f.clientes:
        qs = qs.filter(deuda__cliente__codigo_externo__in=f.clientes)
    if f.qr is not None:
        qs = qs.filter(pk=f.qr)
    return qs


def fila(t: TransaccionQR) -> dict:
    factura = getattr(t, "factura", None)
    return {
        "id": t.pk,
        "creado_en": t.creado_en,
        "pagado_en": t.actualizado_en if t.estado == TransaccionQR.Estado.PAGADO else None,
        "cliente": {"codigo": t.deuda.cliente.codigo_externo, "nombre": t.deuda.cliente.nombre},
        "monto": f"{t.monto_snapshot:.2f}",
        "estado": t.estado,
        # En el panel "cancelado" se muestra como "Anulado" (se inhabilitó en el banco).
        "estado_descripcion": "Anulado" if t.estado == TransaccionQR.Estado.CANCELADO else t.get_estado_display(),
        "cajero": t.usuario.username,
        "factura": (factura.numero_factura or factura.get_estado_envio_display()) if factura else "",
        # Pagado y la factura no llegó al SIIC (o todavía no se armó): se puede reintentar.
        "factura_pendiente": t.estado == TransaccionQR.Estado.PAGADO and (
            factura is None or factura.estado_envio in (factura.EstadoEnvio.PENDIENTE, factura.EstadoEnvio.ERROR)
        ),
        "id_operacion": t.id_operacion_mc4,
    }


def resumen(qs) -> dict:
    pagados = qs.filter(estado=TransaccionQR.Estado.PAGADO).aggregate(n=Count("id"), monto=Sum("monto_snapshot"))
    return {
        "total": qs.count(),
        "pagados": pagados["n"],
        "monto_pagado": f"{pagados['monto'] or Decimal('0'):.2f}",
    }


def pagina(qs, numero: int) -> dict:
    total = qs.count()
    paginas = max(1, -(-total // POR_PAGINA))
    numero = min(max(1, numero), paginas)
    inicio = (numero - 1) * POR_PAGINA
    filas = qs.select_related("deuda__cliente", "usuario", "factura")[inicio:inicio + POR_PAGINA]
    return {"pagina": numero, "paginas": paginas, "por_pagina": POR_PAGINA, "resultados": [fila(t) for t in filas]}


def totales_por_dia(qs) -> dict:
    """Una fila por día con cuántos QR se generaron, cuántos se pagaron y cuánto entró."""
    E = TransaccionQR.Estado
    filas = (
        qs.annotate(dia=TruncDate("creado_en"))
        .values("dia")
        .annotate(
            generados=Count("id"),
            pagados=Count("id", filter=Q(estado=E.PAGADO)),
            monto_pagado=Sum("monto_snapshot", filter=Q(estado=E.PAGADO)),
            anulados=Count("id", filter=Q(estado=E.CANCELADO)),
            vencidos=Count("id", filter=Q(estado=E.VENCIDO)),
            con_error=Count("id", filter=Q(estado=E.ERROR)),
            pendientes=Count("id", filter=Q(estado__in=[E.GENERADO, E.PENDIENTE_CONFIRMACION])),
        )
        .order_by("dia")
    )
    dias = []
    for r in filas:
        dias.append({**r, "dia": r["dia"].isoformat(), "monto_pagado": f"{r['monto_pagado'] or Decimal('0'):.2f}"})
    campos = ("generados", "pagados", "anulados", "vencidos", "con_error", "pendientes")
    totales = {c: sum(d[c] for d in dias) for c in campos}
    totales["monto_pagado"] = f"{sum(Decimal(d['monto_pagado']) for d in dias):.2f}"
    return {"dias": dias, "totales": totales}


def _texto(valor) -> str:
    return str(valor or "").replace("|", "/").replace("\r", " ").replace("\n", " ").strip()


def exportar_txt(qs) -> str:
    """Una línea por QR, separada por "|", con encabezado. Fechas y horas de Bolivia."""
    columnas = ["ID", "FECHA", "HORA", "PAGADO_EN", "COD_CLIENTE", "CLIENTE", "MONTO", "ESTADO", "CAJERO", "FACTURA", "ID_OPERACION"]
    lineas = ["|".join(columnas)]
    for t in qs.select_related("deuda__cliente", "usuario", "factura").order_by("creado_en"):
        f = fila(t)
        creado = timezone.localtime(t.creado_en)
        pagado = timezone.localtime(f["pagado_en"]).strftime("%d/%m/%Y %H:%M:%S") if f["pagado_en"] else ""
        lineas.append("|".join(_texto(v) for v in [
            f["id"], creado.strftime("%d/%m/%Y"), creado.strftime("%H:%M:%S"), pagado,
            f["cliente"]["codigo"], f["cliente"]["nombre"], f["monto"], f["estado_descripcion"].upper(),
            f["cajero"], f["factura"], f["id_operacion"],
        ]))
    return "\r\n".join(lineas) + "\r\n"


def nombre_archivo(f: Filtros, extension: str) -> str:
    return f"qr_{f.desde:%Y%m%d}_{f.hasta:%Y%m%d}.{extension}"

