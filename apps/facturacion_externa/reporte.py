"""Reporte de cobros QR (web, panel de la web y bot-telegram) para exportar a Excel desde
"Pagos web". Todo sale de lo que el gateway ya tiene, sin pedirle nada a cessa-laravel ni al bot:

- glosa, descripción y período: del `detalle` (comprobantes) que llega en cada liquidar, con el
  mismo formato que arma cessa-laravel (PagoQrController / ListRecibos del panel de la web);
- canal y fecha de creación del QR: del alias (CESSA-WEB-/CESSA-ADMIN- con la hora en UTC,
  CESSA-BOT- con la hora de Bolivia);
- pagador, documento y N° de orden: del último aviso de SIP (`AvisoSip`) de ese alias.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone as tz
from decimal import Decimal
from io import BytesIO
from zoneinfo import ZoneInfo

from django.utils import timezone

from .models import AvisoSip, SolicitudLiquidacion

BOLIVIA = ZoneInfo("America/La_Paz")
MESES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre",
         "Octubre", "Noviembre", "Diciembre"]
CANALES = {
    SolicitudLiquidacion.Origen.CESSA_LARAVEL_WEB: "Web (cliente)",
    SolicitudLiquidacion.Origen.CESSA_LARAVEL_PANEL: "Panel de la web",
    SolicitudLiquidacion.Origen.BOT_TELEGRAM: "bot-telegram",
}
BANCOS = {"sip_bisa": "BISA", "bnb": "BNB"}
_ALIAS_FECHA = re.compile(r"^CESSA-[A-Z]+-(\d{14})-")

COLUMNAS = [
    "Número de Cliente", "Alias", "Canal", "Monto (Bs)", "Estado", "Periodo / Glosa", "Descripción de Pago",
    "Banco", "Fecha de Creación", "Fecha de Pago", "Fecha de Factura", "N° de Orden", "Pagador",
    "Documento Pagador", "Intentos de Facturación", "Observación",
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


@dataclass
class Fila:
    solicitud: SolicitudLiquidacion
    aviso: dict

    def valores(self) -> list:
        s, a = self.solicitud, self.aviso
        hora = lambda d: d.astimezone(BOLIVIA).replace(tzinfo=None) if d else None  # noqa: E731
        observacion = (f"Descartado: {s.nota_descarte}" if s.estado == s.Estado.DESCARTADO else s.error) or ""
        return [
            s.nro_cliente,
            s.alias,
            CANALES.get(s.origen, s.origen),
            float(s.monto),
            s.get_estado_display(),
            glosa(len(s.detalle or []), s.nro_cliente, s.monto),
            descripcion(s),
            BANCOS.get(s.banco, s.banco or ""),
            hora(creado_en(s.alias)),
            hora(s.fecha_pago),
            hora(s.procesado_en) if s.estado == s.Estado.FACTURADO else None,
            s.numero_orden_originante or str(a.get("numeroOrdenOriginante") or ""),
            str(a.get("nombreCliente") or ""),
            str(a.get("documentoCliente") or ""),
            s.intentos,
            observacion,
        ]


def filas(solicitudes) -> list[Fila]:
    solicitudes = list(solicitudes)
    avisos: dict[str, dict] = {}
    for aviso in AvisoSip.objects.filter(alias__in=[s.alias for s in solicitudes]).order_by("recibido_en"):
        avisos[aviso.alias] = aviso.datos  # queda el último
    return [Fila(s, avisos.get(s.alias, {})) for s in solicitudes]


def excel(solicitudes, generado_por: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    lista = filas(solicitudes)
    facturado = sum((f.solicitud.monto for f in lista if f.solicitud.estado == SolicitudLiquidacion.Estado.FACTURADO), Decimal(0))
    con_error = sum((f.solicitud.monto for f in lista if f.solicitud.estado == SolicitudLiquidacion.Estado.ERROR), Decimal(0))
    bs = lambda v: f"Bs {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")  # noqa: E731

    wb = Workbook()
    ws = wb.active
    ws.title = "Cobros QR"
    azul, amarillo = "004C98", "FFE71E"
    ws.append(["CESSA — Compañía Eléctrica Sucre S.A."])
    ws.append(["Reporte de Cobros QR (web y bot-telegram)"])
    ws.append([
        f"Generado el {timezone.now().astimezone(BOLIVIA):%d/%m/%Y %H:%M} por {generado_por} · {len(lista)} registro(s) · "
        f"Facturado: {bs(facturado)} · Con error al facturar: {bs(con_error)} · Total cobrado: {bs(facturado + con_error)}"
    ])
    ws.append(COLUMNAS)
    ws["A1"].font = Font(bold=True, size=14, color=azul)
    ws["A2"].font = Font(bold=True, size=12)
    ws["A3"].font = Font(italic=True, size=9, color="555555")
    for celda in ws[4]:
        celda.font = Font(bold=True, color="FFFFFF")
        celda.fill = PatternFill("solid", fgColor=azul)
        celda.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[4].height = 30
    for fila in lista:
        ws.append(fila.valores())

    fechas = {COLUMNAS.index(c) + 1 for c in ("Fecha de Creación", "Fecha de Pago", "Fecha de Factura")}
    monto = COLUMNAS.index("Monto (Bs)") + 1
    anchos = {"Alias": 34, "Periodo / Glosa": 30, "Descripción de Pago": 70, "Pagador": 32, "Observación": 40}
    for i, nombre in enumerate(COLUMNAS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = anchos.get(nombre, 18)
        for (celda,) in ws.iter_rows(min_row=5, min_col=i, max_col=i):
            if i in fechas:
                celda.number_format = "dd/mm/yyyy hh:mm"
            elif i == monto:
                celda.number_format = "#,##0.00"
    ws.freeze_panes = "A5"
    ws.auto_filter.ref = f"A4:{get_column_letter(len(COLUMNAS))}{max(4, ws.max_row)}"
    ws.sheet_properties.tabColor = amarillo

    salida = BytesIO()
    wb.save(salida)
    return salida.getvalue()
