"""QR generados sin pagar (QrGenerado): aviso de la web/bot, lista de "Pagos web" y reporte."""
from datetime import datetime, timedelta, timezone as tz
from decimal import Decimal
from io import BytesIO

import pytest
from django.utils import timezone
from openpyxl import load_workbook
from rest_framework.test import APIClient

from apps.usuarios.models import Usuario

from . import reporte
from .models import QrGenerado, SolicitudLiquidacion

URL = "/api/externo/qr-generados/"


@pytest.fixture
def externo(settings):
    settings.API_KEY_CESSA_LARAVEL = "clave-de-prueba"
    cliente = APIClient()
    cliente.credentials(HTTP_X_API_KEY="clave-de-prueba")
    return cliente


@pytest.fixture
def supervisor():
    usuario = Usuario.objects.create_user(username="sup-qr", password="x", rol=Usuario.Rol.SUPERVISOR, activo=True)
    cliente = APIClient()
    cliente.force_authenticate(usuario)
    return cliente


def _item(mes):
    return {"mes": str(mes), "anio": "2026", "importe": "30.00", "debito_credito": "DEBITO", "detalle": f"Fact {mes}"}


def _qr(alias, horas=-0.02, vence_en_horas=None, **extra):
    creado = timezone.now() + timedelta(hours=horas)
    datos = {
        "alias": alias, "origen": SolicitudLiquidacion.origen_de_alias(alias), "nro_cliente": "100100",
        "monto": Decimal("60.00"), "banco": "sip_bisa", "detalle": [_item(9), _item(10)], "creado_en": creado,
        "vence_en": None if vence_en_horas is None else timezone.now() + timedelta(hours=vence_en_horas),
    }
    datos.update(extra)
    return QrGenerado.objects.create(**datos)


@pytest.mark.django_db
class TestAviso:
    def test_registra_y_es_idempotente(self, externo):
        cuerpo = {"alias": "CESSA-WEB-20261109150000-AAAA", "nro_cliente": "100100", "monto": "60.00",
                  "banco": "sip_bisa", "detalle": [_item(10)], "creado_en": "2026-11-09T11:00:00-04:00"}
        assert externo.post(URL, cuerpo, format="json").status_code == 200
        assert externo.post(URL, cuerpo, format="json").status_code == 200
        qr = QrGenerado.objects.get()
        assert qr.origen == SolicitudLiquidacion.Origen.CESSA_LARAVEL_WEB
        # Sin vencimiento: vale hasta el fin del día (Bolivia) en que se generó.
        assert timezone.localtime(qr.vence_en).strftime("%Y-%m-%d %H:%M") == "2026-11-10 00:00"

    def test_aviso_posterior_solo_con_estado(self, externo):
        _qr("CESSA-BOT-20261109110000-BBBB")
        r = externo.post(URL, {"alias": "CESSA-BOT-20261109110000-BBBB", "estado": "anulado",
                               "observacion": "Anulado: se generó otro QR para la cuenta"}, format="json")
        assert r.status_code == 200 and r.json()["estado"] == "anulado"
        qr = QrGenerado.objects.get()
        assert qr.monto == Decimal("60.00") and qr.origen == SolicitudLiquidacion.Origen.BOT_TELEGRAM

    def test_primer_aviso_sin_monto_es_400(self, externo):
        r = externo.post(URL, {"alias": "CESSA-WEB-1", "nro_cliente": "1"}, format="json")
        assert r.status_code == 400 and "monto" in r.json()

    def test_sin_api_key_no(self):
        assert APIClient().post(URL, {"alias": "x"}, format="json").status_code in (401, 403)


@pytest.mark.django_db
class TestPanel:
    def test_lista_mezcla_pagos_y_qr_sin_pagar(self, supervisor):
        _qr("CESSA-WEB-1-EXP", vence_en_horas=-1)
        _qr("CESSA-WEB-2-VIG", vence_en_horas=5)
        _qr("CESSA-WEB-3-ANU", estado=QrGenerado.Estado.ANULADO, vence_en_horas=5)
        _qr("CESSA-WEB-4-PAG", vence_en_horas=-1)  # se pagó: se ve por su liquidación, no como QR
        SolicitudLiquidacion.objects.create(
            alias="CESSA-WEB-4-PAG", nro_cliente="100100", monto=Decimal("60.00"), detalle=[_item(10)],
            fecha_pago=timezone.now(), estado=SolicitudLiquidacion.Estado.FACTURADO,
        )

        filas = supervisor.get("/api/liquidaciones/").json()
        por_alias = {f["alias"]: (f["tipo"], f["estado"]) for f in filas}
        assert por_alias == {
            "CESSA-WEB-1-EXP": ("qr", "expirado"), "CESSA-WEB-2-VIG": ("qr", "vigente"),
            "CESSA-WEB-3-ANU": ("qr", "anulado"), "CESSA-WEB-4-PAG": ("pago", "facturado"),
        }
        expirados = supervisor.get("/api/liquidaciones/?estado=expirado").json()
        assert [f["alias"] for f in expirados] == ["CESSA-WEB-1-EXP"]
        assert expirados[0]["glosa"] == "CESSA 2 comp 100100 B60.00"
        assert [f["alias"] for f in supervisor.get("/api/liquidaciones/?estado=facturado").json()] == ["CESSA-WEB-4-PAG"]

        resumen = supervisor.get("/api/liquidaciones/resumen/").json()
        assert resumen["qr_hoy"]["expirado"] == {"cantidad": 1, "monto": "60.00"}
        assert resumen["qr_hoy"]["vigente"]["cantidad"] == 1 and resumen["qr_hoy"]["anulado"]["cantidad"] == 1

    def test_excel_incluye_expirados(self, supervisor):
        _qr("CESSA-WEB-20261109140000-EXP1", vence_en_horas=-1,
            creado_en=datetime(2026, 11, 9, 14, 0, tzinfo=tz.utc))
        contenido = reporte.excel([], generado_por="sup", qrs=QrGenerado.objects.all())
        ws = load_workbook(BytesIO(contenido)).active
        assert ws["B6"].value == "CESSA-WEB-20261109140000-EXP1"
        assert ws["D6"].value == "Expirado"
        assert ws["I6"].value is None  # sin fecha de pago
        textos = [c.value for c in ws["B"] if c.value]
        assert "No cobrado (pendiente, expirado, anulado)" in textos
