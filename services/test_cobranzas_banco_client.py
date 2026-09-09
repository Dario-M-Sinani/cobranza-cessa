"""Tests del contrato real de LumenCobranzasBancoClient -- confirmado leyendo
CobranzasBancoService.php/FacturacionRecibo.php de cessa-laravel (ver
cobranzas_banco_client.py). No pega a la red: mockea requests."""
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from django.core.cache import cache

from services.cobranzas_banco_client import (
    CobranzasBancoAuthError,
    CobranzasBancoRequestError,
    LumenCobranzasBancoClient,
    construir_detalle,
    construir_documento,
)


def _response(json_body=None, status_code=200, ok=None, content=b""):
    mock = Mock()
    mock.status_code = status_code
    mock.ok = ok if ok is not None else status_code < 400
    mock.json.return_value = json_body or {}
    mock.text = str(json_body)
    mock.content = content
    return mock


@pytest.fixture(autouse=True)
def _limpiar_cache_token():
    cache.clear()
    yield
    cache.clear()


def _cliente():
    return LumenCobranzasBancoClient(
        base_url="https://cobranzas.example",
        client_id="cid",
        client_secret="secret",
        username="user",
        password="pass",
        agencia_sigla="SUC01",
    )


class TestAutenticacion:
    def test_obtiene_y_cachea_el_token(self):
        cliente = _cliente()
        with patch("services.cobranzas_banco_client.requests.post") as mock_post, \
                patch("services.cobranzas_banco_client.requests.request") as mock_request:
            mock_post.return_value = _response({"access_token": "tok-1"})
            mock_request.return_value = _response({"uuid": "abc"})

            cliente.crear_transaccion()
            cliente.crear_transaccion()

        # Un solo pedido de token pese a dos llamadas -- el segundo usa el cacheado.
        mock_post.assert_called_once_with(
            "https://cobranzas.example/oauth/token",
            json={
                "grant_type": "password",
                "client_id": "cid",
                "client_secret": "secret",
                "username": "user",
                "password": "pass",
                "scope": "",
            },
            timeout=30,
        )
        assert mock_request.call_count == 2

    def test_token_invalido_levanta_auth_error(self):
        cliente = _cliente()
        with patch("services.cobranzas_banco_client.requests.post") as mock_post:
            mock_post.return_value = _response({}, status_code=401, ok=False)
            with pytest.raises(CobranzasBancoAuthError):
                cliente.crear_transaccion()

    def test_401_en_request_reintenta_una_vez_con_token_nuevo(self):
        cliente = _cliente()
        with patch("services.cobranzas_banco_client.requests.post") as mock_post, \
                patch("services.cobranzas_banco_client.requests.request") as mock_request:
            mock_post.side_effect = [_response({"access_token": "tok-1"}), _response({"access_token": "tok-2"})]
            mock_request.side_effect = [_response({}, status_code=401, ok=False), _response({"uuid": "abc"})]

            uuid = cliente.crear_transaccion()

        assert uuid == "abc"
        assert mock_post.call_count == 2  # token viejo descartado, se pidió uno nuevo
        assert mock_request.call_count == 2


class TestFlujoDeCobro:
    def test_asegurar_caja_abierta_no_aperture_si_ya_existe(self):
        cliente = _cliente()
        with patch("services.cobranzas_banco_client.requests.post") as mock_post, \
                patch("services.cobranzas_banco_client.requests.request") as mock_request:
            mock_post.return_value = _response({"access_token": "tok"})
            mock_request.return_value = _response({}, status_code=200)

            cliente.asegurar_caja_abierta()

        mock_request.assert_called_once_with(
            "get", "https://cobranzas.example/v1/cajas/existe",
            headers={"Authorization": "Bearer tok"}, timeout=30,
        )

    def test_asegurar_caja_abierta_apertura_si_no_existe(self):
        cliente = _cliente()
        with patch("services.cobranzas_banco_client.requests.post") as mock_post, \
                patch("services.cobranzas_banco_client.requests.request") as mock_request:
            mock_post.return_value = _response({"access_token": "tok"})
            mock_request.side_effect = [
                _response({}, status_code=404, ok=False),
                _response({}, status_code=200),
            ]

            cliente.asegurar_caja_abierta()

        segunda_llamada = mock_request.call_args_list[1]
        assert segunda_llamada.args == ("post", "https://cobranzas.example/v1/cajas/aperturar")
        assert segunda_llamada.kwargs["json"]["agencia_sigla"] == "SUC01"

    def test_pagar_transaccion_usa_pagar_otro_documento(self):
        cliente = _cliente()
        with patch("services.cobranzas_banco_client.requests.post") as mock_post, \
                patch("services.cobranzas_banco_client.requests.request") as mock_request:
            mock_post.return_value = _response({"access_token": "tok"})
            mock_request.return_value = _response({}, status_code=200)

            cliente.pagar_transaccion("uuid-1", [{"a": 1}], {"b": 2})

        mock_request.assert_called_once_with(
            "put", "https://cobranzas.example/v1/transacciones/uuid-1/pagar-otro-documento",
            headers={"Authorization": "Bearer tok"}, timeout=30,
            json={"detalle": [{"a": 1}], "documento": {"b": 2}},
        )

    def test_pagar_transaccion_propia_usa_pagar_a_secas_sin_documento(self):
        cliente = _cliente()
        with patch("services.cobranzas_banco_client.requests.post") as mock_post, \
                patch("services.cobranzas_banco_client.requests.request") as mock_request:
            mock_post.return_value = _response({"access_token": "tok"})
            mock_request.return_value = _response({}, status_code=200)

            cliente.pagar_transaccion_propia("uuid-1", [{"a": 1}])

        mock_request.assert_called_once_with(
            "put", "https://cobranzas.example/v1/transacciones/uuid-1/pagar",
            headers={"Authorization": "Bearer tok"}, timeout=30,
            json={"detalle": [{"a": 1}]},
        )

    def test_pagar_transaccion_propia_fallida_levanta_request_error(self):
        cliente = _cliente()
        with patch("services.cobranzas_banco_client.requests.post") as mock_post, \
                patch("services.cobranzas_banco_client.requests.request") as mock_request:
            mock_post.return_value = _response({"access_token": "tok"})
            mock_request.return_value = _response({"error": "detalle inválido"}, status_code=422, ok=False)

            with pytest.raises(CobranzasBancoRequestError):
                cliente.pagar_transaccion_propia("uuid-1", [])

    def test_pagar_transaccion_fallida_levanta_request_error(self):
        cliente = _cliente()
        with patch("services.cobranzas_banco_client.requests.post") as mock_post, \
                patch("services.cobranzas_banco_client.requests.request") as mock_request:
            mock_post.return_value = _response({"access_token": "tok"})
            mock_request.return_value = _response({"error": "detalle inválido"}, status_code=422, ok=False)

            with pytest.raises(CobranzasBancoRequestError):
                cliente.pagar_transaccion("uuid-1", [], {})

    def test_obtener_comprobante_pdf_devuelve_bytes(self):
        cliente = _cliente()
        with patch("services.cobranzas_banco_client.requests.post") as mock_post, \
                patch("services.cobranzas_banco_client.requests.request") as mock_request:
            mock_post.return_value = _response({"access_token": "tok"})
            mock_request.return_value = _response(status_code=200, content=b"%PDF-1.4")

            pdf = cliente.obtener_comprobante_pdf("uuid-1")

        assert pdf == b"%PDF-1.4"

    def test_obtener_comprobante_json_devuelve_el_dict(self):
        cliente = _cliente()
        with patch("services.cobranzas_banco_client.requests.post") as mock_post, \
                patch("services.cobranzas_banco_client.requests.request") as mock_request:
            mock_post.return_value = _response({"access_token": "tok"})
            mock_request.return_value = _response({"nro_factura": "F-1"}, status_code=200)

            documento = cliente.obtener_comprobante_json("uuid-1")

        assert documento == {"nro_factura": "F-1"}
        mock_request.assert_called_once_with(
            "get", "https://cobranzas.example/v1/transacciones/uuid-1/documentos",
            headers={"Authorization": "Bearer tok"}, timeout=30,
            params={"formato": "json"},
        )

    def test_obtener_comprobante_json_toma_el_primer_elemento_si_es_lista(self):
        cliente = _cliente()
        with patch("services.cobranzas_banco_client.requests.post") as mock_post, \
                patch("services.cobranzas_banco_client.requests.request") as mock_request:
            mock_post.return_value = _response({"access_token": "tok"})
            mock_request.return_value = _response([{"nro_factura": "F-1"}], status_code=200)

            documento = cliente.obtener_comprobante_json("uuid-1")

        assert documento == {"nro_factura": "F-1"}

    def test_obtener_comprobante_json_fallido_levanta_request_error(self):
        cliente = _cliente()
        with patch("services.cobranzas_banco_client.requests.post") as mock_post, \
                patch("services.cobranzas_banco_client.requests.request") as mock_request:
            mock_post.return_value = _response({"access_token": "tok"})
            mock_request.return_value = _response({"error": "no encontrado"}, status_code=404, ok=False)

            with pytest.raises(CobranzasBancoRequestError):
                cliente.obtener_comprobante_json("uuid-1")


class TestConstruirDetalle:
    def test_formatea_fechas_y_magnitud_de_importe(self):
        detalle = construir_detalle(
            [
                {
                    "codigo_sucursal": "01", "nro_comprobante": "1", "nro_suministro": "123",
                    "fecha": "20260101", "tipo": "FC", "letra_comprobante": "A",
                    "nro_autorizacion": "0", "anio": 2026, "mes": 1,
                    "importe": -150.5, "debito_credito": "CREDITO", "detalle": "Conciliación",
                }
            ],
            nro_cliente_fallback="999",
        )

        assert detalle[0]["fecha"] == "20260101"
        assert detalle[0]["importe"] == "150.50"  # magnitud sin signo, nunca negativo
        assert detalle[0]["nro_cliente"] == "999"  # usa el fallback: el ítem no traía el suyo

    def test_fecha_ausente_cae_a_00000000(self):
        detalle = construir_detalle([{"importe": 10, "debito_credito": "DEBITO"}])
        assert detalle[0]["fecha"] == "00000000"
        assert detalle[0]["fecha_vencimiento"] == "00000000"


class TestConstruirDocumento:
    def test_arma_documento_con_moneda_bob(self, settings):
        settings.COBRANZAS_BANCO_DOCUMENTO_ENTE_ID = "5"
        settings.COBRANZAS_BANCO_DOCUMENTO_BANCO_ID = "7"

        documento = construir_documento(
            nro_cliente="123", monto=Decimal("150.50"), moneda="BOB",
            numero_documento="CESSA-WEB-1", fecha_pago="2026-09-08",
        )

        assert documento == {
            "ente_id": 5, "moneda": "B", "banco_id": 7, "numero": "CESSA-WEB-1",
            "importe": 150.5, "fecha": "20260908", "fecha_vencimiento": "20260908",
        }

    def test_moneda_usd_se_mapea_a_d(self, settings):
        settings.COBRANZAS_BANCO_DOCUMENTO_ENTE_ID = "5"
        settings.COBRANZAS_BANCO_DOCUMENTO_BANCO_ID = "7"

        documento = construir_documento(
            nro_cliente="123", monto=Decimal("10"), moneda="usd",
            numero_documento="X", fecha_pago="2026-09-08",
        )

        assert documento["moneda"] == "D"
