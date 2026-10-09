"""Conciliación .102 vs gateway (conciliacion.py). Sin red: cliente de api-cobranzas falso."""
from datetime import date
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from apps.clientes.models import Cliente
from apps.cobranza.models import CobroEfectivo, Deuda, Factura
from apps.usuarios.models import Usuario

from .conciliacion import conciliar
from .models import SolicitudLiquidacion

HOY = date(2026, 10, 7)


def _resp(datos, ok=True):
    r = Mock(ok=ok, status_code=200 if ok else 404, text="")
    r.json.return_value = datos
    return r


class _Banco:
    def __init__(self, listado, individuales=None):
        self.listado, self.individuales = listado, individuales or {}

    def _request_autenticado(self, metodo, path, **kw):
        if path == "/v1/transacciones":
            return _resp(self.listado)
        uuid = path.rsplit("/", 1)[-1]
        return _resp(self.individuales.get(uuid), ok=uuid in self.individuales)


def _t(uuid, estado="PAGADA", total="86.10", **kw):
    return {"id": 1, "uuid": uuid, "estado": estado, "total_pagado": total, "lote": 6, "caja_codigo": 8197,
            "fecha_creacion": "2026-10-07 11:06:45", **kw}


def _web(uuid, estado="facturado", monto="86.10", **kw):
    return SolicitudLiquidacion.objects.create(
        alias=f"R-{uuid}", nro_cliente="179185", monto=Decimal(monto), fecha_pago=timezone.now(), detalle=[],
        estado=estado, cobranzas_uuid=uuid, banco="bnb", procesado_en=timezone.make_aware(timezone.datetime(2026, 10, 7, 11)),
        **kw,
    )


def _conciliar(banco):
    with patch("apps.facturacion_externa.conciliacion.get_cobranzas_banco_client", return_value=banco):
        return conciliar(HOY)


@pytest.mark.django_db
class TestConciliacion:
    def test_todo_cuadra(self):
        _web("u1")
        r = _conciliar(_Banco([_t("u1"), _t("u-fallida", estado="FALLIDA", total="0.00")]))
        assert r.diferencias == []
        assert r.revisadas == 2
        assert "Pagos web facturados: 1 = Bs. 86,10 (BNB 1 = Bs. 86,10)" in r.texto()

    def test_pagada_en_la_102_sin_registro(self):
        r = _conciliar(_Banco([_t("desconocida", total="50.00")]))
        assert r.criticas == 1 and "sin pago web ni cobro del panel" in r.diferencias[0][1]

    def test_pagada_en_la_102_pero_no_facturada_aca(self):
        _web("u1", estado="descartado")
        r = _conciliar(_Banco([_t("u1")]))
        assert "está 'descartado'" in r.diferencias[0][1]

    def test_monto_distinto(self):
        _web("u1", monto="90.00")
        r = _conciliar(_Banco([_t("u1", total="86.10")]))
        assert "Bs. 86,10" in r.diferencias[0][1] and "Bs. 90,00" in r.diferencias[0][1]

    def test_en_transaccion(self):
        r = _conciliar(_Banco([_t("u1", estado="EN_TRANSACCION")]))
        assert "EN_TRANSACCION" in r.diferencias[0][1]

    def test_facturado_aca_con_transaccion_de_otro_dia_que_no_esta_pagada(self):
        _web("vieja")
        r = _conciliar(_Banco([], individuales={"vieja": _t("vieja", estado="FALLIDA")}))
        assert "está FALLIDA" in r.diferencias[0][1]

    def test_anulada_en_la_102(self):
        _web("u1")
        r = _conciliar(_Banco([_t("u1", estado="ANULADA", fecha_anulacion="2026-10-07 12:00")]))
        assert r.diferencias[0][0] == "AVISO"

    def test_cobro_del_panel(self):
        cajera = Usuario.objects.create_user(username="c", password="x", rol=Usuario.Rol.CAJERA, activo=True)
        deuda = Deuda.objects.create(cliente=Cliente.objects.create(codigo_externo="1", nombre="X"), monto=Decimal("40"))
        cobro = CobroEfectivo.objects.create(deuda=deuda, usuario=cajera, monto_snapshot=Decimal("40"), monto_recibido=Decimal("40"), vuelto=0)
        Factura.objects.create(
            cobro_efectivo=cobro, cobranzas_uuid="p1", estado_envio=Factura.EstadoEnvio.ENVIADO,
            emitida_en=timezone.make_aware(timezone.datetime(2026, 10, 7, 9)),
        )
        r = _conciliar(_Banco([_t("p1", total="40.00")]))
        assert r.diferencias == []
        assert "Cobros del panel facturados: 1 = Bs. 40,00" in r.texto()

    def test_la_102_caida(self):
        banco = Mock()
        banco._request_autenticado.return_value = _resp({}, ok=False)
        r = _conciliar(banco)
        assert r.criticas == 1 and "No se pudo consultar" in r.diferencias[0][1]
