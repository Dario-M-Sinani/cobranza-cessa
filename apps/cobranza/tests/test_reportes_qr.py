"""Lista y reportes de QR (apps/cobranza/reportes_qr.py), como listaQr.xhtml de la app Java."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.cobranza.models import TransaccionQR
from apps.usuarios.models import Usuario

from .test_cobro_agrupado import _api, cajera, deudas  # noqa: F401 -- fixtures


@pytest.fixture
def supervisor(db):
    return Usuario.objects.create_user(username="sup-qr", password="x", rol=Usuario.Rol.SUPERVISOR)


@pytest.fixture
def otra(db):
    return Usuario.objects.create_user(username="caj-qr2", password="x", rol=Usuario.Rol.CAJERA, activo=True)


def _qr(usuario, deuda, monto, estado=TransaccionQR.Estado.PENDIENTE_CONFIRMACION, dias_atras=0):
    t = TransaccionQR.objects.create(deuda=deuda, usuario=usuario, monto_snapshot=Decimal(monto), estado=TransaccionQR.Estado.GENERADO)
    if estado != TransaccionQR.Estado.GENERADO:
        if estado == TransaccionQR.Estado.PAGADO:
            t.transicionar_estado(TransaccionQR.Estado.PENDIENTE_CONFIRMACION)
        t.transicionar_estado(estado)
    if dias_atras:
        TransaccionQR.objects.filter(pk=t.pk).update(creado_en=timezone.now() - timedelta(days=dias_atras))
    return t


@pytest.fixture
def qrs(cajera, otra, deudas):  # noqa: F811
    a, b = deudas
    return {
        "pagado": _qr(cajera, a, "22.30", TransaccionQR.Estado.PAGADO),
        "pendiente": _qr(cajera, b, "10.00"),
        "anulado": _qr(otra, a, "5.00", TransaccionQR.Estado.CANCELADO),
        "viejo": _qr(cajera, b, "99.00", TransaccionQR.Estado.PAGADO, dias_atras=3),
    }


@pytest.mark.django_db
class TestBuscar:
    URL = "/api/transacciones-qr/buscar/"

    def test_hoy_por_defecto_con_resumen(self, supervisor, qrs):
        r = _api(supervisor).get(self.URL)
        assert r.status_code == 200, r.data
        assert {f["id"] for f in r.data["resultados"]} == {qrs["pagado"].pk, qrs["pendiente"].pk, qrs["anulado"].pk}
        assert (r.data["total"], r.data["pagados"], r.data["monto_pagado"]) == (3, 1, "22.30")

    def test_filtros(self, supervisor, qrs, deudas):  # noqa: F811
        hoy = timezone.localdate()
        desde = (hoy - timedelta(days=5)).isoformat()
        r = _api(supervisor).get(self.URL, {"desde": desde, "hasta": hoy.isoformat(), "estado": "pagado"})
        assert {f["id"] for f in r.data["resultados"]} == {qrs["pagado"].pk, qrs["viejo"].pk}
        r = _api(supervisor).get(self.URL, {"cajero": "CAJ-QR2"})
        assert [f["id"] for f in r.data["resultados"]] == [qrs["anulado"].pk]
        codigo = deudas[1].cliente.codigo_externo
        r = _api(supervisor).get(self.URL, {"clientes": f" {codigo} , 1"})
        assert [f["id"] for f in r.data["resultados"]] == [qrs["pendiente"].pk]
        r = _api(supervisor).get(self.URL, {"qr": qrs["pagado"].pk})
        assert r.data["total"] == 1

    def test_la_cajera_ve_solo_los_suyos(self, cajera, qrs):  # noqa: F811
        r = _api(cajera).get(self.URL)
        assert {f["id"] for f in r.data["resultados"]} == {qrs["pagado"].pk, qrs["pendiente"].pk}

    @pytest.mark.parametrize("params, mensaje", [
        ({"desde": "ayer"}, "Fechas"),
        ({"desde": "2026-11-10", "hasta": "2026-11-01"}, "anterior"),
        ({"estado": "raro"}, "Estado"),
        ({"clientes": "12a"}, "coma"),
        ({"qr": "x"}, "número"),
    ])
    def test_filtros_invalidos(self, supervisor, params, mensaje):
        r = _api(supervisor).get(self.URL, params)
        assert r.status_code == 400 and mensaje in r.data["detail"]

    def test_paginado(self, supervisor, cajera, deudas):  # noqa: F811
        for _ in range(30):
            _qr(cajera, deudas[0], "1.00", TransaccionQR.Estado.GENERADO)
        r1 = _api(supervisor).get(self.URL)
        r2 = _api(supervisor).get(self.URL, {"pagina": 2})
        assert (r1.data["paginas"], len(r1.data["resultados"]), len(r2.data["resultados"])) == (2, 25, 5)


@pytest.mark.django_db
class TestReportes:
    def test_totales_por_dia(self, supervisor, qrs):
        hoy = timezone.localdate()
        r = _api(supervisor).get("/api/transacciones-qr/totales-por-dia/", {"desde": (hoy - timedelta(days=5)).isoformat()
                                 , "hasta": hoy.isoformat()})
        assert r.status_code == 200, r.data
        assert [d["dia"] for d in r.data["dias"]] == [(hoy - timedelta(days=3)).isoformat(), hoy.isoformat()]
        dia_hoy = r.data["dias"][1]
        assert (dia_hoy["generados"], dia_hoy["pagados"], dia_hoy["monto_pagado"], dia_hoy["anulados"], dia_hoy["pendientes"]) == (
            3, 1, "22.30", 1, 1)
        assert r.data["totales"]["monto_pagado"] == "121.30" and r.data["totales"]["generados"] == 4

    def test_detalle_sin_paginar(self, supervisor, qrs):
        r = _api(supervisor).get("/api/transacciones-qr/detalle/", {"estado": "pagado"})
        assert r.status_code == 200
        assert [f["id"] for f in r.data["resultados"]] == [qrs["pagado"].pk]
        assert r.data["resultados"][0]["pagado_en"] is not None

    def test_txt(self, cajera, qrs):  # noqa: F811
        r = _api(cajera).get("/api/transacciones-qr/exportar-txt/")
        assert r.status_code == 200 and r["Content-Type"].startswith("text/plain")
        hoy = timezone.localdate()
        assert f'filename="qr_{hoy:%Y%m%d}_{hoy:%Y%m%d}.txt"' in r["Content-Disposition"]
        lineas = r.content.decode().split("\r\n")
        assert lineas[0].startswith("ID|FECHA|HORA|PAGADO_EN|COD_CLIENTE")
        datos = [linea.split("|") for linea in lineas[1:] if linea]
        assert {int(d[0]) for d in datos} == {qrs["pagado"].pk, qrs["pendiente"].pk}  # solo los de la cajera
        pagado = next(d for d in datos if int(d[0]) == qrs["pagado"].pk)
        assert pagado[6] == "22.30" and pagado[7] == "PAGADO" and pagado[3]
