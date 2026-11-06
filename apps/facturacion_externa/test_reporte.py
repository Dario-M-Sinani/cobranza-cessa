"""Reporte de cobros (reporte.py), avisos de SIP guardados y panel solo consulta."""
import base64
from datetime import datetime, timezone as tz
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import load_workbook
from rest_framework.test import APIClient

from apps.usuarios.models import Usuario

from . import reporte
from .models import AvisoSip, SolicitudLiquidacion


def _item(mes, anio="2026", importe="10.00"):
    return {"mes": str(mes), "anio": anio, "importe": importe, "debito_credito": "DEBITO", "detalle": f"Fact {mes}"}


def _solicitud(alias="CESSA-WEB-20261106132141-TST1", detalle=None, monto="183.10", **extra):
    return SolicitudLiquidacion.objects.create(
        alias=alias, origen=SolicitudLiquidacion.origen_de_alias(alias), nro_cliente="100100",
        monto=Decimal(monto), detalle=detalle or [_item(10), _item(11)],
        fecha_pago=datetime(2026, 11, 6, 13, 25, tzinfo=tz.utc), banco="sip_bisa", **extra,
    )


def test_glosa_igual_que_la_web():
    assert reporte.glosa(2, "100100", Decimal("183.1")) == "CESSA 2 comp 100100 B183.10"
    assert reporte.glosa(24, "1234567890", Decimal("50000")) == "24 comp 1234567890 B50000.00"


@pytest.mark.django_db
def test_descripcion_igual_que_la_web_y_por_canal():
    web = _solicitud()
    assert reporte.descripcion(web) == (
        "Pago de 2 meses (2 comprobantes) por 183.10 bolivianos — Cliente 100100 — "
        "Períodos: Octubre 2026 y Noviembre 2026"
    )
    panel = _solicitud(alias="CESSA-ADMIN-20261106135847-TST2", detalle=[_item(10)], monto="432")
    assert reporte.descripcion(panel).startswith("Pago de 1 mes (1 comprobante) por 432 bolivianos")
    assert reporte.descripcion(panel).endswith("— Generado desde el panel")
    bot = _solicitud(alias="CESSA-BOT-20261106135911-TST3", detalle=[_item(10)])
    assert bot.origen == SolicitudLiquidacion.Origen.BOT_TELEGRAM
    assert reporte.descripcion(bot).endswith("— Generado desde el bot de Telegram")


def test_fecha_de_creacion_sale_del_alias():
    # web: hora UTC en el alias (13:21 UTC = 09:21 Bolivia); bot: ya viene en hora de Bolivia
    assert reporte.creado_en("CESSA-WEB-20261106132141-TST1").strftime("%H:%M") == "09:21"
    assert reporte.creado_en("CESSA-BOT-20261106135911-TST3").strftime("%H:%M") == "13:59"
    assert reporte.creado_en("txqr-5") is None


@pytest.mark.django_db
def test_exportar_excel_con_pagador_del_aviso_de_sip(settings):
    _solicitud(estado=SolicitudLiquidacion.Estado.FACTURADO, numero_orden_originante="")
    AvisoSip.objects.create(alias="CESSA-WEB-20261106132141-TST1", datos={
        "alias": "CESSA-WEB-20261106132141-TST1", "nombreCliente": "CLIENTE DE PRUEBA",
        "documentoCliente": "1234567", "numeroOrdenOriginante": "6666000000001",
    })
    supervisor = Usuario.objects.create_user(username="sup", password="x", rol=Usuario.Rol.SUPERVISOR, activo=True)
    cliente = APIClient()
    cliente.force_authenticate(supervisor)

    respuesta = cliente.get("/api/liquidaciones/exportar/")

    assert respuesta.status_code == 200
    assert respuesta["Content-Disposition"].startswith('attachment; filename="cobros-qr-')
    ws = load_workbook(BytesIO(respuesta.content)).active
    assert ws["B1"].value == "CESSA — Compañía Eléctrica Sucre S.A."
    assert ws["B2"].value == "Reporte de Cobros QR"
    encabezado = [c.value for c in ws[5]]
    assert encabezado == [n for n, _ in reporte.COLUMNAS]  # mismas columnas que el reporte de la web
    fila = dict(zip(encabezado, [c.value for c in ws[6]]))
    assert fila["Generado Por"] == "Web (cliente)"
    resumen = {ws.cell(row=r, column=2).value: ws.cell(row=r, column=3).value for r in range(7, ws.max_row + 1)}
    assert resumen["TOTAL COBRADO"] == 183.1
    assert fila["Periodo / Glosa"] == "CESSA 2 comp 100100 B183.10"
    assert fila["Pagador"] == "CLIENTE DE PRUEBA"
    assert fila["Documento Pagador"] == "1234567"
    assert fila["N° de Orden"] == "6666000000001"
    assert fila["Estado"] == "Facturado"


@pytest.mark.django_db
def test_exportar_solo_supervisor_o_admin():
    cajera = Usuario.objects.create_user(username="caj", password="x", rol=Usuario.Rol.CAJERA, activo=True)
    cliente = APIClient()
    cliente.force_authenticate(cajera)
    assert cliente.get("/api/liquidaciones/exportar/").status_code == 403


@pytest.mark.django_db
def test_el_callback_guarda_el_aviso_de_sip(settings, monkeypatch):
    settings.SIP_CALLBACK_USERNAME, settings.SIP_CALLBACK_PASSWORD = "u", "p"
    settings.SIP_CALLBACK_SIN_REENVIO = ["CESSA-BOT-"]
    auth = "Basic " + base64.b64encode(b"u:p").decode()
    cuerpo = {"alias": "CESSA-BOT-20261106-AB12", "nombreCliente": "PEREZ", "documentoCliente": "123"}

    APIClient().post("/api/externo/sip/confirmar-pago", cuerpo, format="json", HTTP_AUTHORIZATION=auth)
    APIClient().post("/api/externo/sip/confirmar-pago", cuerpo, format="json")  # sin auth: no se guarda

    assert AvisoSip.objects.count() == 1
    assert AvisoSip.objects.get().datos["nombreCliente"] == "PEREZ"


@pytest.mark.django_db
def test_panel_solo_consulta_bloquea_cobro_y_caja(settings):
    settings.PANEL_SOLO_CONSULTA = True
    supervisor = Usuario.objects.create_user(username="sup", password="x", rol=Usuario.Rol.SUPERVISOR, activo=True)
    cliente = APIClient()
    cliente.force_authenticate(supervisor)

    assert cliente.post("/api/cobros-efectivo/", {}, format="json").status_code == 403
    assert cliente.get("/api/cajas/").status_code == 403
    assert cliente.get("/api/liquidaciones/").status_code == 200
    token = APIClient().post("/api/auth/token/", {"username": "sup", "password": "x"}, format="json").data["access"]
    carga = token.split(".")[1]
    assert '"solo_consulta":true' in base64.urlsafe_b64decode(carga + "=" * (-len(carga) % 4)).decode().replace(" ", "")


@pytest.mark.django_db
def test_detalle_del_panel_trae_glosa_pagador_y_generado_por():
    s = _solicitud(alias="CESSA-BOT-20261106135911-TST3", detalle=[_item(10)], monto="23.10")
    AvisoSip.objects.create(alias=s.alias, datos={"nombreCliente": "CLIENTE DE PRUEBA", "documentoCliente": "1234567"})
    supervisor = Usuario.objects.create_user(username="sup", password="x", rol=Usuario.Rol.SUPERVISOR, activo=True)
    cliente = APIClient()
    cliente.force_authenticate(supervisor)

    datos = cliente.get(f"/api/liquidaciones/{s.id}/").json()

    assert datos["generado_por"] == "bot-telegram"
    assert datos["glosa"] == "CESSA 1 comp 100100 B23.10"
    assert datos["pagador"] == "CLIENTE DE PRUEBA"
    assert datos["documento_pagador"] == "1234567"
    assert datos["creado_en"].startswith("2026-11-06T13:59:11")
