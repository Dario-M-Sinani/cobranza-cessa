"""Reporte de cobros QR (web, panel de la web y bot-telegram) para exportar a Excel desde
"Pagos web", con el mismo formato que el reporte de cobros del panel de la web (logo, títulos,
encabezado azul, Cliente/Alias resaltados y el resumen al final). Todo sale de lo que el gateway
ya tiene, sin pedirle nada a cessa-laravel ni al bot:

- glosa, descripción y período: del `detalle` (comprobantes) que llega en cada liquidar, con el
  mismo formato que arma cessa-laravel (PagoQrController / ListRecibos del panel de la web);
- "Generado Por" y fecha de creación del QR: del alias (CESSA-WEB-/CESSA-ADMIN- con la hora en
  UTC, CESSA-BOT- con la hora de Bolivia);
- pagador, documento y N° de orden: del último aviso de SIP (`AvisoSip`) de ese alias.

A diferencia del reporte de la web, acá solo están los QR pagados (los que se piden liquidar):
los vencidos o cancelados sin pagar no llegan al gateway.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone as tz
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from django.utils import timezone

from .models import AvisoSip, SolicitudLiquidacion

BOLIVIA = ZoneInfo("America/La_Paz")
LOGO = Path(__file__).parent / "recursos" / "logo-cessa.png"
MESES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre",
         "Octubre", "Noviembre", "Diciembre"]
GENERADO_POR = {
    SolicitudLiquidacion.Origen.CESSA_LARAVEL_WEB: "Web (cliente)",
    SolicitudLiquidacion.Origen.CESSA_LARAVEL_PANEL: "Panel de la web",
    SolicitudLiquidacion.Origen.BOT_TELEGRAM: "bot-telegram",
}
BANCOS = {"sip_bisa": "BISA", "bnb": "BNB"}
_ALIAS_FECHA = re.compile(r"^CESSA-[A-Z]+-(\d{14})-")

# Mismas columnas, en el mismo orden y ancho, que el reporte de cobros de la web.
COLUMNAS = [
    ("Número de Cliente", 17), ("Alias", 34), ("Monto (Bs)", 13), ("Estado", 18), ("Periodo / Glosa", 30),
    ("Descripción de Pago", 40), ("Banco", 12), ("Fecha de Creación", 17), ("Fecha de Pago", 17),
    ("N° de Orden", 16), ("Pagador", 28), ("Documento Pagador", 17), ("Observación", 40), ("Generado Por", 18),
]


def glosa(cantidad: int, nro_cliente: str, monto: Decimal) -> str:
    con_prefijo = f"CESSA {cantidad} comp {nro_cliente} B{monto:.2f}"
    return con_prefijo if len(con_prefijo) <= 30 else con_prefijo[len("CESSA "):]


def descripcion(solicitud: SolicitudLiquidacion) -> str:
    items = solicitud.detalle or []
    n = len(items)
    meses = "1 mes" if n == 1 else f"{n} meses"
    comprobantes = "1 comprobante" if n == 1 else f"{n} comprobantes"
    periodos = []
    for item in items:
        try:
            periodos.append(f"{MESES[int(item.get('mes'))]} {item.get('anio')}")
        except (TypeError, ValueError, IndexError):
            periodos.append(str(item.get("detalle") or "").strip())
    fechas = (", ".join(periodos[:-1]) + " y " + periodos[-1]) if len(periodos) > 1 else "".join(periodos)
    monto = solicitud.monto
    legible = f"{monto:.0f}" if monto == monto.to_integral_value() else f"{monto:.2f}"
    texto = f"Pago de {meses} ({comprobantes}) por {legible} bolivianos — Cliente {solicitud.nro_cliente} — Períodos: {fechas}"
    if solicitud.origen == SolicitudLiquidacion.Origen.CESSA_LARAVEL_PANEL:
        texto += " — Generado desde el panel"
    elif solicitud.origen == SolicitudLiquidacion.Origen.BOT_TELEGRAM:
        texto += " — Generado desde el bot de Telegram"
    return texto


def creado_en(alias: str) -> datetime | None:
    """La hora del alias: UTC en la web (CESSA-WEB-/CESSA-ADMIN-), Bolivia en el bot."""
    m = _ALIAS_FECHA.match(alias)
    if not m:
        return None
    try:
        crudo = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    zona = BOLIVIA if alias.startswith(SolicitudLiquidacion.PREFIJO_BOT_TELEGRAM) else tz.utc
    return crudo.replace(tzinfo=zona).astimezone(BOLIVIA)


def observacion(s: SolicitudLiquidacion) -> str:
    if s.estado == s.Estado.DESCARTADO:
        return f"Descartado: {s.nota_descarte}"
    if s.estado == s.Estado.FACTURADO:
        return f"Facturado en el intento {s.intentos + 1}" if s.intentos else ""
    return s.error or ""


@dataclass
class Fila:
    solicitud: SolicitudLiquidacion
    aviso: dict

    @property
    def pagador(self) -> str:
        return str(self.aviso.get("nombreCliente") or "").strip()

    @property
    def documento_pagador(self) -> str:
        return str(self.aviso.get("documentoCliente") or "").strip()

    @property
    def nro_orden(self) -> str:
        return self.solicitud.numero_orden_originante or str(self.aviso.get("numeroOrdenOriginante") or "")

    def valores(self) -> list:
        s = self.solicitud
        hora = lambda d: d.astimezone(BOLIVIA).replace(tzinfo=None) if d else None  # noqa: E731
        return [
            s.nro_cliente,
            s.alias,
            float(s.monto),
            s.get_estado_display(),
            glosa(len(s.detalle or []), s.nro_cliente, s.monto),
            descripcion(s),
            BANCOS.get(s.banco, s.banco or ""),
            hora(creado_en(s.alias)),
            hora(s.fecha_pago),
            self.nro_orden,
            self.pagador,
            self.documento_pagador,
            observacion(s),
            GENERADO_POR.get(s.origen, s.origen),
        ]


def avisos_por_alias(aliases) -> dict[str, dict]:
    avisos: dict[str, dict] = {}
    for aviso in AvisoSip.objects.filter(alias__in=list(aliases)).order_by("recibido_en"):
        avisos[aviso.alias] = aviso.datos  # queda el último
    return avisos


def filas(solicitudes) -> list[Fila]:
    solicitudes = list(solicitudes)
    avisos = avisos_por_alias(s.alias for s in solicitudes)
    return [Fila(s, avisos.get(s.alias, {})) for s in solicitudes]


def _bs(valor: Decimal) -> str:
    return f"Bs {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def excel(solicitudes, generado_por: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    lista = filas(solicitudes)
    E = SolicitudLiquidacion.Estado
    grupos = {
        "facturado": [f for f in lista if f.solicitud.estado == E.FACTURADO],
        "error": [f for f in lista if f.solicitud.estado == E.ERROR],
        "otros": [f for f in lista if f.solicitud.estado not in (E.FACTURADO, E.ERROR)],
    }
    suma = lambda fs: sum((f.solicitud.monto for f in fs), Decimal(0))  # noqa: E731

    azul, celeste, gris = "1F3A68", "DCE6F1", "BFBFBF"
    fino = Side(style="thin", color=gris)
    grueso = Side(style="medium", color=azul)
    borde = Border(left=fino, right=fino, top=fino, bottom=fino)
    borde_total = Border(top=grueso, bottom=grueso)
    base = "Calibri"
    f_titulo = Font(name=base, bold=True, size=16, color=azul)
    f_subtitulo = Font(name=base, bold=True, size=12, color=azul)
    f_nota = Font(name=base, italic=True, size=9, color="595959")
    f_encabezado = Font(name=base, bold=True, size=10, color="FFFFFF")
    f_dato = Font(name=base, size=10)
    f_clave = Font(name=base, bold=True, size=10, color=azul)
    relleno_encabezado = PatternFill("solid", fgColor=azul)
    relleno_clave = PatternFill("solid", fgColor=celeste)
    arriba = Alignment(vertical="top", wrap_text=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "Cobros QR"
    ws.sheet_view.showGridLines = False
    ultima = get_column_letter(len(COLUMNAS))

    ahora = timezone.now().astimezone(BOLIVIA)
    cobrado = grupos["facturado"] + grupos["error"]
    titulos = [
        ("CESSA — Compañía Eléctrica Sucre S.A.", f_titulo, 26),
        ("Reporte de Cobros QR", f_subtitulo, 20),
        (f"Generado el {ahora:%d/%m/%Y %H:%M} por {generado_por} · {len(lista)} registro(s) · "
         f"Facturado: {_bs(suma(grupos['facturado']))} · Con error al facturar: {_bs(suma(grupos['error']))} · "
         f"Total cobrado: {_bs(suma(cobrado))}", f_nota, 18),
    ]
    for fila, (texto, fuente, alto) in enumerate(titulos, start=1):
        celda = ws.cell(row=fila, column=2, value=texto)
        celda.font, celda.alignment = fuente, Alignment(vertical="center")
        ws.merge_cells(f"B{fila}:{ultima}{fila}")
        ws.row_dimensions[fila].height = alto
    if LOGO.exists():
        try:
            from openpyxl.drawing.image import Image

            logo = Image(str(LOGO))
            logo.width = logo.height = 60
            ws.add_image(logo, "A1")
        except ImportError:  # sin Pillow: el reporte sale igual, sin logo
            pass

    for col, (nombre, ancho) in enumerate(COLUMNAS, start=1):
        celda = ws.cell(row=5, column=col, value=nombre)
        celda.font, celda.fill, celda.border = f_encabezado, relleno_encabezado, borde
        celda.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(col)].width = ancho
    ws.row_dimensions[5].height = 30

    for i, f in enumerate(lista, start=6):
        for col, valor in enumerate(f.valores(), start=1):
            celda = ws.cell(row=i, column=col, value=valor)
            celda.border, celda.font, celda.alignment = borde, f_dato, arriba
            if col in (1, 2):
                celda.font, celda.fill, celda.number_format = f_clave, relleno_clave, "@"
                celda.alignment = Alignment(vertical="top")
            elif col == 3:
                celda.number_format, celda.alignment = "#,##0.00", Alignment(vertical="top")
            elif col in (8, 9):
                celda.number_format = "dd/mm/yyyy hh:mm"
                celda.alignment = Alignment(horizontal="center", vertical="top")
    fin = 5 + len(lista)
    ws.freeze_panes = "A6"
    ws.auto_filter.ref = f"A5:{ultima}{max(fin, 5)}"

    # Resumen (como el de la web): monto y cantidad por situación.
    r = fin + 2
    for col, texto in enumerate(("RESUMEN", "Monto (Bs)", "Cantidad"), start=2):
        celda = ws.cell(row=r, column=col, value=texto)
        celda.font, celda.fill, celda.border = f_encabezado, relleno_encabezado, borde
        celda.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    def renglon(fila, texto, fs, total=False):
        for col, valor in ((2, texto), (3, float(suma(fs))), (4, f"{len(fs)} registro(s)")):
            celda = ws.cell(row=fila, column=col, value=valor)
            if total:
                celda.font, celda.border = Font(name=base, bold=True, size=10, color=azul), borde_total
            else:
                celda.font, celda.border, celda.alignment = f_dato, borde, arriba
            if col == 3:
                celda.number_format = "#,##0.00"

    renglon(r + 1, "Pagado y facturado", grupos["facturado"], total=True)
    renglon(r + 2, "Pagado, con error al facturar (revisar)", grupos["error"], total=True)
    renglon(r + 3, "TOTAL COBRADO", cobrado, total=True)
    renglon(r + 5, "Pendiente de facturar o descartado", grupos["otros"])
    renglon(r + 6, "Total general (todo lo listado)", lista)

    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = ws.page_margins.right = 0.4

    salida = BytesIO()
    wb.save(salida)
    return salida.getvalue()
