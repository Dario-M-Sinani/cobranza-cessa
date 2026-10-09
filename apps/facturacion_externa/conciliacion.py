"""Conciliación diaria: lo que api-cobranzas-bancos (la .102) tiene registrado con el usuario de
banco del gateway contra lo que el gateway cree que pasó. Solo lectura.

El usuario de banco lo usan dos cosas: los pagos QR de la web (SolicitudLiquidacion) y los
cobros del panel de cajeras (apps.cobranza.Factura); las dos guardan el uuid de su transacción.

Diferencias que importan (el dinero quedó registrado en el SIIC y acá no, o al revés):
- transacción PAGADA en la .102 sin liquidación ni factura conocida;
- PAGADA en la .102 pero acá no figura facturada, o con otro monto;
- facturada acá pero en la .102 no está PAGADA (o fue anulada);
- transacción EN_TRANSACCION (pago a medio procesar).
Las FALLIDA no se reportan: es lo normal cuando el SIIC rechaza (no queda nada pagado).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.cobranza.models import Factura
from services.cobranzas_banco_client import CobranzasBancoError, get_cobranzas_banco_client

from .models import SolicitudLiquidacion

CRITICA, AVISO = "CRÍTICA", "AVISO"
BANCOS = {"sip_bisa": "BISA", "bnb": "BNB"}


@dataclass
class Reporte:
    desde: date
    hasta: date
    diferencias: list[tuple[str, str]] = field(default_factory=list)
    totales: list[str] = field(default_factory=list)
    revisadas: int = 0

    def agregar(self, gravedad: str, texto: str) -> None:
        self.diferencias.append((gravedad, texto))

    @property
    def criticas(self) -> int:
        return sum(1 for gravedad, _ in self.diferencias if gravedad == CRITICA)

    def texto(self) -> str:
        periodo = f"{self.desde:%d/%m/%Y}" + (f" al {self.hasta:%d/%m/%Y}" if self.hasta != self.desde else "")
        lineas = [f"Conciliación {periodo}: {self.revisadas} transacciones en la .102 revisadas."]
        lineas += self.totales
        if self.diferencias:
            lineas.append(f"\n{len(self.diferencias)} diferencias ({self.criticas} críticas):")
            lineas += [f"• [{gravedad}] {texto}" for gravedad, texto in self.diferencias]
        else:
            lineas.append("Sin diferencias.")
        return "\n".join(lineas)


def _bs(valor) -> str:
    numero = f"{Decimal(str(valor or 0)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"Bs. {numero}"


def _rango(desde: date, hasta: date):
    inicio = timezone.make_aware(datetime.combine(desde, time.min))
    fin = timezone.make_aware(datetime.combine(hasta + timedelta(days=1), time.min))
    return inicio, fin


def _transacciones_remotas(cliente, desde: date, hasta: date) -> list[dict]:
    r = cliente._request_autenticado(
        "get", "/v1/transacciones", params={"fecha_inicio": f"{desde:%Y-%m-%d}", "fecha_fin": f"{hasta:%Y-%m-%d}"}
    )
    if not r.ok:
        raise CobranzasBancoError(f"GET /v1/transacciones: HTTP {r.status_code} {r.text[:200]}")
    datos = r.json()
    return datos.get("data", []) if isinstance(datos, dict) else datos


def _transaccion(cliente, uuid: str) -> dict | None:
    r = cliente._request_autenticado("get", f"/v1/transacciones/{uuid}")
    return r.json() if r.ok else None


def conciliar(desde: date, hasta: date | None = None) -> Reporte:
    hasta = hasta or desde
    reporte = Reporte(desde, hasta)
    inicio, fin = _rango(desde, hasta)

    liquidaciones = {
        s.cobranzas_uuid: s for s in SolicitudLiquidacion.objects.exclude(cobranzas_uuid="").defer("comprobante_pdf")
    }
    facturas_panel = {
        f.cobranzas_uuid: f
        for f in Factura.objects.exclude(cobranzas_uuid="").select_related("transaccion_qr", "cobro_efectivo").defer("comprobante_pdf")
    }

    cliente = get_cobranzas_banco_client()
    try:
        remotas = _transacciones_remotas(cliente, desde, hasta)
    except (CobranzasBancoError, ValueError) as exc:
        reporte.agregar(CRITICA, f"No se pudo consultar la .102: {exc}")
        return reporte
    reporte.revisadas = len(remotas)
    vistas = set()

    for t in remotas:
        uuid, estado = t.get("uuid", ""), t.get("estado", "")
        vistas.add(uuid)
        ref = f"transacción {t.get('id')} (lote {t.get('lote')}, caja {t.get('caja_codigo')}, {t.get('fecha_creacion')})"
        s, f = liquidaciones.get(uuid), facturas_panel.get(uuid)

        if estado == "EN_TRANSACCION":
            reporte.agregar(CRITICA, f"{ref} quedó EN_TRANSACCION (pago a medio procesar): revisar a mano en la .102.")
        elif estado == "PAGADA":
            pagado = Decimal(str(t.get("total_pagado") or 0))
            if s:
                if s.estado != SolicitudLiquidacion.Estado.FACTURADO:
                    reporte.agregar(CRITICA, f"{ref} PAGADA en la .102 pero el pago web {s.alias} (cliente {s.nro_cliente}) está '{s.estado}' en el gateway.")
                elif pagado != s.monto:
                    reporte.agregar(CRITICA, f"{ref}: la .102 registró {_bs(pagado)} y el pago web {s.alias} es de {_bs(s.monto)}.")
            elif f:
                monto = f.origen.monto_snapshot if f.origen else None
                if f.estado_envio != Factura.EstadoEnvio.ENVIADO:
                    reporte.agregar(CRITICA, f"{ref} PAGADA en la .102 pero la factura {f.pk} del panel está '{f.estado_envio}'.")
                elif monto is not None and pagado != monto:
                    reporte.agregar(CRITICA, f"{ref}: la .102 registró {_bs(pagado)} y el cobro del panel es de {_bs(monto)}.")
            else:
                reporte.agregar(CRITICA, f"{ref} PAGADA por {_bs(pagado)} en la .102 sin pago web ni cobro del panel que la registre.")
        elif estado == "ANULADA" and (
            (s and s.estado == SolicitudLiquidacion.Estado.FACTURADO) or (f and f.estado_envio == Factura.EstadoEnvio.ENVIADO)
        ):
            reporte.agregar(AVISO, f"{ref} fue ANULADA en la .102 ({t.get('fecha_anulacion')}) y acá figura facturada.")

    # Facturado acá en el período, con transacción creada antes (no vino en el listado).
    facturadas_web = SolicitudLiquidacion.objects.filter(
        estado=SolicitudLiquidacion.Estado.FACTURADO, procesado_en__gte=inicio, procesado_en__lt=fin
    ).defer("comprobante_pdf")
    enviadas_panel = Factura.objects.filter(
        estado_envio=Factura.EstadoEnvio.ENVIADO, emitida_en__gte=inicio, emitida_en__lt=fin
    ).select_related("transaccion_qr", "cobro_efectivo").defer("comprobante_pdf")
    for uuid, etiqueta in [(s.cobranzas_uuid, f"pago web {s.alias}") for s in facturadas_web] + [
        (f.cobranzas_uuid, f"factura {f.pk} del panel") for f in enviadas_panel
    ]:
        if not uuid or uuid in vistas:
            continue
        try:
            t = _transaccion(cliente, uuid)
        except (CobranzasBancoError, ValueError):
            t = None
        if not t or t.get("estado") != "PAGADA":
            estado = t.get("estado") if t else "no encontrada"
            reporte.agregar(CRITICA, f"El {etiqueta} figura facturado pero su transacción en la .102 está {estado}.")

    # Totales del período (para cruzar con los extractos de los bancos).
    web = list(facturadas_web)
    por_banco: dict[str, list] = {}
    for s in web:
        por_banco.setdefault(BANCOS.get(s.banco, s.banco or "sin banco"), []).append(s.monto)
    detalle = ", ".join(f"{banco} {len(montos)} = {_bs(sum(montos))}" for banco, montos in sorted(por_banco.items()))
    reporte.totales.append(f"Pagos web facturados: {len(web)} = {_bs(sum((s.monto for s in web), Decimal('0')))}" + (f" ({detalle})" if detalle else ""))
    panel = [f.origen.monto_snapshot for f in enviadas_panel if f.origen]
    reporte.totales.append(f"Cobros del panel facturados: {len(panel)} = {_bs(sum(panel, Decimal('0')))}")
    return reporte
