"""Alertas de pagos web sin factura (alertas.py, services/alertas.py). No pega a la red ni a Redis:
se reemplaza la tarea Celery y requests."""
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from services import alertas as servicio

from . import alertas
from .models import SolicitudLiquidacion


@pytest.fixture
def con_telegram(settings):
    settings.ALERTAS_TELEGRAM_BOT_TOKEN = "123:abc"
    settings.ALERTAS_TELEGRAM_CHAT_IDS = ["111", "222"]
    settings.ALERTAS_EMAIL_DESTINOS = []
    settings.ALERTAS_INTENTOS_MINIMOS = 3
    settings.PANEL_URL = "https://10.1.1.88"


@pytest.fixture
def encolar():
    with patch("apps.facturacion_externa.tasks.enviar_alerta.delay") as delay:
        yield delay


def _solicitud(**kw):
    datos = {
        "alias": "CESSA-WEB-1", "nro_cliente": "115997", "monto": Decimal("3068.60"), "banco": "sip_bisa",
        "fecha_pago": timezone.now(), "detalle": [], "estado": SolicitudLiquidacion.Estado.ERROR, "intentos": 1,
    }
    datos.update(kw)
    return SolicitudLiquidacion.objects.create(**datos)


@pytest.mark.django_db
class TestCuandoAvisar:
    def test_rechazo_definitivo_avisa_enseguida_y_una_sola_vez(self, con_telegram, encolar):
        s = _solicitud(error="La deuda cambió desde que se generó el QR: ya no están pendientes N° 20253")
        alertas.evaluar(s)
        s.refresh_from_db()
        alertas.evaluar(s)

        assert encolar.call_count == 1
        asunto, texto = encolar.call_args.args
        assert "SIN FACTURA" in asunto
        assert "115997" in texto and "Bs. 3.068,60" in texto and "BISA" in texto
        assert "N° 20253" in texto and "https://10.1.1.88/pagos-web" in texto
        assert s.alertado_en is not None

    def test_error_comun_espera_al_tercer_intento(self, con_telegram, encolar):
        s = _solicitud(error="Error inesperado: timeout", intentos=2)
        alertas.evaluar(s)
        assert encolar.call_count == 0
        s.intentos = 3
        s.save()
        alertas.evaluar(s)
        assert encolar.call_count == 1

    def test_caja_fuera_de_horario_no_avisa(self, con_telegram, encolar):
        alertas.evaluar(_solicitud(error="aperturar caja: El operador no puede aperturar caja fuera de horario", intentos=9))
        assert encolar.call_count == 0

    def test_si_se_factura_despues_del_aviso_avisa_resuelto(self, con_telegram, encolar):
        s = _solicitud(estado=SolicitudLiquidacion.Estado.FACTURADO, alertado_en=timezone.now())
        alertas.evaluar(s)
        assert "resuelto" in encolar.call_args.args[0]
        s.refresh_from_db()
        assert s.alertado_en is None

    def test_facturado_sin_aviso_previo_no_manda_nada(self, con_telegram, encolar):
        alertas.evaluar(_solicitud(estado=SolicitudLiquidacion.Estado.FACTURADO))
        assert encolar.call_count == 0

    def test_sin_canal_configurado_no_encola_ni_marca(self, settings, encolar):
        settings.ALERTAS_TELEGRAM_BOT_TOKEN = ""
        settings.ALERTAS_EMAIL_DESTINOS = []
        s = _solicitud(error="La deuda no existe")
        alertas.evaluar(s)
        s.refresh_from_db()
        assert encolar.call_count == 0 and s.alertado_en is None

    def test_liquidar_solicitud_evalua_la_alerta(self, con_telegram, encolar, monkeypatch):
        from . import services

        s = _solicitud(estado=SolicitudLiquidacion.Estado.PENDIENTE)

        def falla(solicitud):
            solicitud.estado = SolicitudLiquidacion.Estado.ERROR
            solicitud.error = "La deuda no existe con los datos proporcionados"
            solicitud.save()
            return solicitud

        monkeypatch.setattr(services, "_liquidar", falla)
        services.liquidar_solicitud(s)
        assert encolar.call_count == 1

    def test_si_la_alerta_falla_el_pago_sigue(self, con_telegram, monkeypatch):
        from . import services

        monkeypatch.setattr(services, "_liquidar", lambda s: s)
        with patch("apps.facturacion_externa.alertas.evaluar", side_effect=RuntimeError("boom")):
            s = services.liquidar_solicitud(_solicitud())
        assert s.pk


@pytest.mark.django_db
class TestResumenYEnvio:
    def test_resumen_de_errores(self, con_telegram):
        assert alertas.resumen_de_errores() is None
        _solicitud(alias="A", error="La deuda no existe")
        _solicitud(alias="B", estado=SolicitudLiquidacion.Estado.FACTURADO)
        asunto, texto = alertas.resumen_de_errores()
        assert asunto.startswith("1 pagos web") and "La deuda no existe" in texto

    def test_telegram_manda_a_cada_chat(self, con_telegram):
        with patch("services.alertas.requests.post", return_value=Mock(ok=True)) as post:
            assert servicio.notificar("Asunto", "Texto") == 2
        assert post.call_args.kwargs["json"]["chat_id"] == "222"
        assert "bot123:abc/sendMessage" in post.call_args.args[0]

    def test_telegram_caido_no_lanza(self, con_telegram):
        import requests

        with patch("services.alertas.requests.post", side_effect=requests.ConnectionError("x")):
            assert servicio.notificar("A", "B") == 0

    def test_email(self, settings, mailoutbox):
        settings.ALERTAS_TELEGRAM_BOT_TOKEN = ""
        settings.ALERTAS_EMAIL_DESTINOS = ["cobranzas@cessa.com.bo"]
        assert servicio.notificar("Pago web sin factura", "detalle") == 1
        assert mailoutbox[0].subject == "[Cobranza CESSA] Pago web sin factura"
