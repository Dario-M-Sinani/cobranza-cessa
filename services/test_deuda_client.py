"""Tests del contrato real de SiicDeudaClient contra `/v1/consulta/cliente`
-- confirmado leyendo CessaApiService.php/ConsultaDeudaController.php de
cessa-laravel (ver deuda_client.py). No pega a la red: mockea requests.get."""
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from services.deuda_client import DeudaClientError, SiicDeudaClient, _reparar_codificacion


def _response(json_body, status_code=200):
    mock = Mock()
    mock.status_code = status_code
    mock.json.return_value = json_body
    mock.raise_for_status = Mock()
    return mock


class TestSiicDeudaClient:
    def test_arma_la_request_con_el_contrato_real(self):
        cliente = SiicDeudaClient(base_url="https://siic.example", token="el-token")
        with patch("services.deuda_client.requests.get") as mock_get:
            mock_get.return_value = _response({"nro_cliente": "123", "nombre": "Cliente Test", "deuda": []})
            cliente.consultar_deuda("123")

        mock_get.assert_called_once_with(
            "https://siic.example/v1/consulta/cliente",
            params={"nro_cliente": "123", "ver_deuda": "si"},
            headers={"Authorization": "el-token"},  # sin "Bearer "
            timeout=20,
        )

    def test_parsea_nombre_y_deuda(self):
        cliente = SiicDeudaClient(base_url="https://siic.example", token="x")
        body = {
            "nro_cliente": "123",
            "nombre": "Cliente Test",
            "deuda": [
                {
                    "codigo_sucursal": "01", "nro_comprobante": "1", "nro_suministro": "123",
                    "fecha": "20260101", "tipo": "FC", "letra_comprobante": "A",
                    "nro_autorizacion": "0", "nro_cliente": "123", "anio": 2026, "mes": 1,
                    "importe": 150.5, "detalle": "Consumo", "debito_credito": "DEBITO",
                },
                {
                    "codigo_sucursal": "01", "nro_comprobante": "2", "nro_suministro": "123",
                    "fecha": "20260102", "tipo": "NC", "letra_comprobante": "A",
                    "nro_autorizacion": "0", "nro_cliente": "123", "anio": 2026, "mes": 1,
                    "importe": 20.0, "detalle": "Conciliacion", "debito_credito": "CREDITO",
                },
            ],
        }
        with patch("services.deuda_client.requests.get") as mock_get:
            mock_get.return_value = _response(body)
            resultado = cliente.consultar_deuda("123")

        assert resultado.nombre_cliente == "Cliente Test"
        assert len(resultado.items) == 2
        assert resultado.items[0].importe == Decimal("150.5")
        assert resultado.items[1].importe == Decimal("-20.0")  # CREDITO resta
        assert resultado.monto_total == Decimal("130.5")

    def test_no_confia_en_el_signo_crudo_de_importe(self):
        """SIIC podría mandar `importe` ya negativo para una CREDITO -- el
        cliente igual debe tratarlo como magnitud y aplicar el signo por
        `debito_credito`, nunca duplicar el signo."""
        cliente = SiicDeudaClient(base_url="https://siic.example", token="x")
        body = {
            "nro_cliente": "123", "nombre": "Cliente Test",
            "deuda": [{
                "codigo_sucursal": "01", "nro_comprobante": "2", "nro_suministro": "123",
                "fecha": "20260102", "tipo": "NC", "letra_comprobante": "A",
                "nro_autorizacion": "0", "nro_cliente": "123", "anio": 2026, "mes": 1,
                "importe": -20.0, "detalle": "Conciliacion", "debito_credito": "CREDITO",
            }],
        }
        with patch("services.deuda_client.requests.get") as mock_get:
            mock_get.return_value = _response(body)
            resultado = cliente.consultar_deuda("123")

        assert resultado.items[0].importe == Decimal("-20.0")

    def test_error_en_la_respuesta_levanta_deuda_client_error(self):
        cliente = SiicDeudaClient(base_url="https://siic.example", token="x")
        with patch("services.deuda_client.requests.get") as mock_get:
            mock_get.return_value = _response({"error": "No encontrado"})
            with pytest.raises(DeudaClientError):
                cliente.consultar_deuda("999")

    def test_nro_cliente_vacio_levanta_deuda_client_error(self):
        cliente = SiicDeudaClient(base_url="https://siic.example", token="x")
        with patch("services.deuda_client.requests.get") as mock_get:
            mock_get.return_value = _response({"nro_cliente": ""})
            with pytest.raises(DeudaClientError):
                cliente.consultar_deuda("999")

    def test_repara_nombre_utf8_doble_codificado(self):
        """Caso real reportado 2026-09-07: "SIÑANI DURAN JHAMINA NADIR"
        vuelve de SIIC como "SIÃANI..." (el byte de la Ñ mal decodificado
        como ISO-8859-1). Mismo bug ya documentado y arreglado en
        CessaApiService::fixEncoding() del lado Laravel."""
        nombre_correcto = "SIÑANI DURAN JHAMINA NADIR"
        nombre_doble_codificado = nombre_correcto.encode("utf-8").decode("latin-1")

        cliente = SiicDeudaClient(base_url="https://siic.example", token="x")
        with patch("services.deuda_client.requests.get") as mock_get:
            mock_get.return_value = _response(
                {"nro_cliente": "123", "nombre": nombre_doble_codificado, "deuda": []}
            )
            resultado = cliente.consultar_deuda("123")

        assert resultado.nombre_cliente == nombre_correcto

    def test_no_toca_texto_ya_correcto(self):
        assert _reparar_codificacion("Juan Pérez Mamani") == "Juan Pérez Mamani"
        assert _reparar_codificacion("") == ""
        assert _reparar_codificacion(None) is None
