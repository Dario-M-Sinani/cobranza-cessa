"""SIIC simulado para desarrollo local: mismos endpoints y mismo formato que la API Nest de
test (:6012), con datos inventados. Solo librería estándar.

    GET  /v1/consulta/cliente?nro_cliente=N&ver_deuda=si   deuda (como SiicDeudaClient)
    GET  /v1/clientes/N/pagos?limit=L                      facturas pagadas (siic_historial)
    GET  /v1/clientes/N/facturas?estado=pendientes|pagadas consumo y lecturas
    POST /v1/comprobantes                                  PDF de un comprobante
    POST /_simulador/pagar      {"items": [detalle]}       marca comprobantes como pagados
    POST /_simulador/reiniciar                             vuelve todo a la deuda inicial
    GET  /_simulador/clientes                              clientes de ejemplo para probar

Cada número de cliente da siempre el mismo nombre y la misma deuda. Casos fijos:
    999999 no existe · terminados en 0 sin deuda · 777777 mucha deuda (20 meses)
Lo pagado se guarda en local/.siic_simulado.json (sobrevive a reinicios).
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ESTADO_ARCHIVO = os.environ.get("SIIC_SIMULADO_ESTADO", os.path.join(os.path.dirname(__file__), ".siic_simulado.json"))
PUERTO = int(os.environ.get("SIIC_SIMULADO_PUERTO", "6012"))

MESES = ["ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO", "JULIO", "AGOSTO",
         "SEPTIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE"]
NOMBRES = ["MARIA", "JUAN CARLOS", "ROSA", "LUIS FERNANDO", "CARMEN", "JORGE", "ANA LUCIA", "PEDRO",
           "SILVIA", "MARCO ANTONIO", "PATRICIA", "RENE", "GLADYS", "OSCAR", "LIZETH", "FREDDY"]
APELLIDOS = ["MAMANI", "QUISPE", "FLORES", "GUTIERREZ", "VARGAS", "ROJAS", "CHOQUE", "PAREDES",
             "ARCE", "SALAZAR", "MENDOZA", "CAMACHO", "TORREZ", "ZURITA", "CORTEZ", "AGUILAR"]
CALLES = ["CALVO", "BOLIVAR", "JUNIN", "ESPAÑA", "RAVELO", "SAN ALBERTO", "AV. DEL MAESTRO", "GRAU"]

_candado = threading.Lock()


def _cargar_estado() -> dict:
    try:
        with open(ESTADO_ARCHIVO, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"pagados": {}}


def _guardar_estado(estado: dict) -> None:
    with open(ESTADO_ARCHIVO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)


ESTADO = _cargar_estado()


def _meses_atras(hoy: date, n: int) -> tuple[int, int]:
    total = hoy.year * 12 + (hoy.month - 1) - n
    return total // 12, total % 12 + 1


def _fecha(d: date) -> str:
    return d.strftime("%Y%m%d")


def _cliente(nro: str) -> dict | None:
    """Todo lo que el SIIC sabe de un cliente, generado a partir del número (siempre igual)."""
    if not nro.isdigit() or nro == "999999" or len(nro) > 9:
        return None
    rnd = random.Random(int(nro) * 7919)
    hoy = date.today()
    nombre = f"{rnd.choice(APELLIDOS)} {rnd.choice(APELLIDOS)} {rnd.choice(NOMBRES)}"
    if nro == "777777":
        meses_deuda = 20
    elif nro.endswith("0"):
        meses_deuda = 0
    else:
        meses_deuda = rnd.choice([1, 1, 2, 2, 3, 4, 6])
    meses_pagados = rnd.randint(4, 14)

    lectura = Decimal(rnd.randint(2000, 9000))
    comprobante = 2_000_000 + int(nro) % 900_000 * 3
    facturas = []  # de la más vieja a la más nueva
    for atras in range(meses_deuda + meses_pagados, 0, -1):
        anio, mes = _meses_atras(hoy, atras)
        consumo = Decimal(rnd.randint(60, 620))
        importe = (consumo * Decimal("1.05") + Decimal(rnd.randint(500, 1500)) / 100).quantize(Decimal("0.1"))
        emision = date(anio, mes, 1) + timedelta(days=rnd.randint(8, 12))
        comprobante += rnd.randint(150_000, 170_000)
        facturas.append({
            "anio": anio, "mes": mes, "emision": emision, "vence": emision + timedelta(days=30),
            "consumo": consumo, "lectura_anterior": lectura, "lectura_actual": lectura + consumo,
            "importe": importe, "nro_comprobante": str(comprobante),
            "pendiente": atras <= meses_deuda,
        })
        lectura += consumo

    items_deuda, pagadas = [], []
    for i, f in enumerate(facturas):
        item = {
            "codigo_sucursal": "1", "nro_comprobante": f["nro_comprobante"], "nro_suministro": "1",
            "fecha": _fecha(f["emision"]), "tipo": "3", "letra_comprobante": " " * 12,
            "nro_autorizacion": "3", "nro_cliente": nro, "anio": str(f["anio"]), "mes": str(f["mes"]),
            "importe": f"{f['importe']:.2f}", "debito_credito": "DEBITO",
            "fecha_vencimiento": _fecha(f["vence"]), "fecha_autorizacion": _fecha(f["emision"] - timedelta(days=2)),
            "otras_ventas_codigo": "0", "imprimir_recibo": "0",
            "detalle": f"Fact Energia {MESES[f['mes'] - 1]}/{f['anio']}",
        }
        if f["pendiente"]:
            items_deuda.append(item)
            # Algunas facturas impagas tienen una conciliación (nota de débito o de crédito).
            if rnd.random() < 0.3:
                es_credito = rnd.random() < 0.5
                items_deuda.append({
                    **item, "nro_comprobante": str(10_000 + int(nro) % 9000 + i), "tipo": "30",
                    "nro_autorizacion": "0", "fecha": _fecha(f["emision"] + timedelta(days=rnd.randint(20, 60))),
                    "fecha_autorizacion": "00000000", "otras_ventas_codigo": "22",
                    "importe": f"{Decimal(rnd.randint(150, 6000)) / 10:.2f}",
                    "debito_credito": "CREDITO" if es_credito else "DEBITO",
                    "detalle": f"{'NC' if es_credito else 'ND'}. CONCILIACIÓN {MESES[f['mes'] - 1]}/{f['anio']}",
                })
        else:
            pago = f["vence"] - timedelta(days=rnd.randint(0, 20))
            pagadas.append({**item, "pago_fecha": _fecha(min(pago, hoy)),
                            "pago_hora": f"{rnd.randint(8, 18):02d}:{rnd.randint(0, 59):02d}:{rnd.randint(0, 59):02d}"})

    # Lo que se pagó desde el panel (o la web/kiosco locales) deja de ser deuda.
    pagados_aca = ESTADO["pagados"].get(nro, {})
    for item in list(items_deuda):
        pago = pagados_aca.get(item["nro_comprobante"])
        if pago:
            items_deuda.remove(item)
            if item["tipo"] == "3":
                pagadas.append({k: v for k, v in item.items() if k not in ("anio", "mes", "fecha_vencimiento",
                                "fecha_autorizacion", "otras_ventas_codigo", "imprimir_recibo", "debito_credito")}
                               | {"pago_fecha": pago["fecha"], "pago_hora": pago["hora"]})
    pagadas.sort(key=lambda p: (p["pago_fecha"], p["pago_hora"], p["fecha"]), reverse=True)

    return {
        "nro": nro, "nombre": nombre, "direccion": f"{rnd.choice(CALLES)} {rnd.randint(10, 990)}",
        "nit": str(rnd.randint(1_000_000, 9_999_999)), "medidor": str(rnd.randint(100_000_000, 299_999_999)),
        "deuda": items_deuda, "pagadas": pagadas, "facturas": facturas,
    }


def _cuenta(nro: str) -> tuple[str, str, str]:
    """Cuenta inventada pero fija para cada cliente: 100021 -> 01-000-00021 (y al revés)."""
    n = int(nro)
    return f"{n // 100000:02d}", f"{(n // 1000) % 100:03d}", f"{n % 1000:05d}"


def _nro(q: dict) -> str:
    """`nro_cliente`, o el cliente de la cuenta `zona`/`manzano`/`correlativo`."""
    if q.get("nro_cliente"):
        return q["nro_cliente"]
    try:
        z, m, c = int(q.get("zona", "")), int(q.get("manzano", "")), int(q.get("correlativo", ""))
    except ValueError:
        return ""
    return str(z * 100000 + m * 1000 + c) if m < 100 and c < 1000 else ""


def _consulta_cliente(nro: str) -> tuple[int, dict]:
    c = _cliente(nro)
    if c is None:
        return 404, {"error": "No existe el cliente"}
    total = sum((Decimal(i["importe"]) * (-1 if i["debito_credito"] == "CREDITO" else 1) for i in c["deuda"]),
                Decimal("0"))
    return 200, {
        "nro_cliente": nro, "zona": _cuenta(nro)[0], "manzano": _cuenta(nro)[1], "correlativo": _cuenta(nro)[2],
        "nro_cuenta": "".join(_cuenta(nro)),
        "nit_document": c["nit"], "nombre": c["nombre"], "direccion": c["direccion"], "nro_medidor": c["medidor"],
        "estado_codigo": "1", "estado_descripcion": "CORTADO" if nro.endswith("7") else "CONECTADO",
        "categoria_codigo": "2       ", "categoria_descripcion": "GENERAL" if nro.endswith("5") else "DOMICILIARIA", "deuda": c["deuda"], "deuda_total": f"{total:.2f}",
        "deuda_cantidad": str(len(c["deuda"])),
    }


def _facturas(nro: str, estado: str) -> list[dict]:
    c = _cliente(nro)
    if c is None:
        return []
    pagados_aca = ESTADO["pagados"].get(nro, {})
    filas = []
    for f in c["facturas"]:
        pendiente = f["pendiente"] and f["nro_comprobante"] not in pagados_aca
        if (estado == "pendientes") == pendiente:
            filas.append({
                "anio": str(f["anio"]), "mes": str(f["mes"]), "importe": f"{f['importe']:.2f}",
                "lectura_anterior": f"{f['lectura_anterior']:.2f}", "lectura_actual": f"{f['lectura_actual']:.2f}",
                "consumo": f"{f['consumo']:.2f}", "fecha_lectura": _fecha(f["emision"] - timedelta(days=5)),
            })
    filas.reverse()
    return filas if estado == "pendientes" else filas[:12]


def _pdf(lineas: list[str]) -> bytes:
    """Un PDF de una página con texto plano (suficiente para ver que el botón PDF funciona)."""
    def esc(t: str) -> str:
        return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    texto = "BT /F1 12 Tf 60 780 Td 16 TL " + " ".join(f"({esc(l)}) '" for l in lineas) + " ET"
    contenido = texto.encode("latin-1", "replace")
    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(contenido) + contenido + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    salida, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, obj in enumerate(objetos, 1):
        offsets.append(len(salida))
        salida += b"%d 0 obj\n" % n + obj + b"\nendobj\n"
    xref = len(salida)
    salida += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objetos) + 1)
    salida += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    salida += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objetos) + 1, xref)
    return bytes(salida)


def _comprobante(item: dict) -> bytes | None:
    c = _cliente(str(item.get("nro_cliente", "")))
    if c is None:
        return None
    nro = str(item.get("nro_comprobante", ""))
    todos = {i["nro_comprobante"]: i for i in c["deuda"] + c["pagadas"]}
    i = todos.get(nro)
    if i is None:
        return None
    pago = f"Pagada el {i['pago_fecha'][6:]}/{i['pago_fecha'][4:6]}/{i['pago_fecha'][:4]}" if "pago_fecha" in i else "Pendiente"
    return _pdf([
        "CESSA - FACTURA (SIIC SIMULADO, SIN VALOR)", "",
        f"Cliente: {c['nro']} - {c['nombre']}", f"NIT/CI: {c['nit']}", f"Direccion: {c['direccion']}", "",
        f"Comprobante N {nro}", f"Concepto: {i['detalle']}", f"Importe: Bs {i['importe']}", pago,
    ])


def _marcar_pagados(items: list[dict], uuid: str = "") -> int:
    """Lo pagado deja de ser deuda. Con `uuid` (la transacción del "banco" simulado) además se
    guarda el comprobante de esa transacción, con un N° de factura correlativo."""
    ahora = datetime.now()
    with _candado:
        for item in items:
            nro_cliente, nro = str(item.get("nro_cliente", "")), str(item.get("nro_comprobante", ""))
            if nro_cliente and nro:
                ESTADO["pagados"].setdefault(nro_cliente, {})[nro] = {
                    "fecha": ahora.strftime("%Y%m%d"), "hora": ahora.strftime("%H:%M:%S")}
        if uuid and uuid not in ESTADO.setdefault("transacciones", {}):
            ESTADO["ultima_factura"] = ESTADO.get("ultima_factura", 70000) + 1
            ESTADO["transacciones"][uuid] = {
                "nro_factura": str(ESTADO["ultima_factura"]), "fecha": ahora.strftime("%d/%m/%Y %H:%M:%S"),
                "items": items,
            }
        _guardar_estado(ESTADO)
    return len(items)


def _comprobante_transaccion(uuid: str) -> dict | None:
    """Lo que devolvería api-cobranzas-bancos como comprobante JSON de una transacción pagada."""
    t = ESTADO.get("transacciones", {}).get(uuid)
    if t is None:
        return None
    nro_cliente = str((t["items"] or [{}])[0].get("nro_cliente", ""))
    c = _cliente(nro_cliente) or {"nombre": "", "nit": "", "direccion": ""}
    total = sum((Decimal(str(i.get("importe", 0))) * (-1 if i.get("debito_credito") == "CREDITO" else 1)
                 for i in t["items"]), Decimal("0"))
    return {
        "nro_factura": t["nro_factura"], "fecha_emision": t["fecha"], "nro_cliente": nro_cliente,
        "cliente_nombre": c["nombre"], "nit": c["nit"], "direccion": c["direccion"],
        "cuf": f"SIMULADO{t['nro_factura']}", "total_pagar": f"{total:.2f}",
        "detalle": [{"descripcion": i.get("detalle", "Concepto"), "subtotal": i.get("importe", "0")} for i in t["items"]],
    }


def _pdf_transaccion(uuid: str) -> bytes | None:
    doc = _comprobante_transaccion(uuid)
    if doc is None:
        return None
    return _pdf([
        "CESSA - FACTURA (SIMULADA, SIN VALOR FISCAL)", "",
        f"Factura N {doc['nro_factura']}   Fecha: {doc['fecha_emision']}",
        f"Cliente: {doc['nro_cliente']} - {doc['cliente_nombre']}", f"NIT/CI: {doc['nit']}", "",
        *[f"{d['descripcion']}: Bs {d['subtotal']}" for d in doc["detalle"]],
        "", f"TOTAL: Bs {doc['total_pagar']}",
    ])


EJEMPLOS = {
    "100001": "deuda corta", "115997": "varios meses", "123457": "varios meses", "200003": "deuda corta",
    "777777": "20 meses de deuda", "100010": "sin deuda", "999999": "no existe",
}


class Manejador(BaseHTTPRequestHandler):
    def _json(self, status: int, cuerpo) -> None:
        datos = json.dumps(cuerpo, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def _cuerpo(self) -> dict:
        largo = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(largo) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/v1/consulta/cliente":
            return self._json(*_consulta_cliente(_nro(q)))
        m = re.fullmatch(r"/v1/clientes/(\w+)/pagos", url.path)
        if m:
            c = _cliente(m.group(1))
            items = c["pagadas"] if c else []
            limite = int(q.get("limit", "-1") or -1)
            return self._json(200, {"items": items if limite < 0 else items[:limite]})
        m = re.fullmatch(r"/v1/clientes/(\w+)/facturas", url.path)
        if m:
            return self._json(200, _facturas(m.group(1), q.get("estado", "pendientes")))
        if url.path == "/_simulador/clientes":
            return self._json(200, EJEMPLOS)
        m = re.fullmatch(r"/_simulador/transacciones/([\w-]+)(/pdf)?", url.path)
        if m:
            if m.group(2):
                pdf = _pdf_transaccion(m.group(1))
                if pdf is None:
                    return self._json(404, {"error": "transacción desconocida"})
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(pdf)))
                self.end_headers()
                self.wfile.write(pdf)
                return
            doc = _comprobante_transaccion(m.group(1))
            return self._json(200, doc) if doc else self._json(404, {"error": "transacción desconocida"})
        self._json(404, {"error": "ruta no simulada", "ruta": url.path})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/v1/comprobantes":
            items = self._cuerpo().get("items") or []
            pdf = _comprobante(items[0]) if items else None
            if pdf is None:
                return self._json(404, {"error": "No existe registro del comprobante"})
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(pdf)))
            self.end_headers()
            self.wfile.write(pdf)
            return
        if url.path == "/_simulador/pagar":
            cuerpo = self._cuerpo()
            return self._json(200, {"pagados": _marcar_pagados(cuerpo.get("items") or [], str(cuerpo.get("uuid") or ""))})
        if url.path == "/_simulador/reiniciar":
            with _candado:
                ESTADO["pagados"] = {}
                ESTADO["transacciones"] = {}
                ESTADO["nest_cajas"] = {}
                ESTADO["nest_transacciones"] = {}
                _guardar_estado(ESTADO)
            return self._json(200, {"ok": True})
        self._json(404, {"error": "ruta no simulada", "ruta": url.path})

    def log_message(self, formato, *args):
        print(f"[siic] {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}", flush=True)


# ---------------------------------------------------------------------------------------------
# Nest de cobranzas simulada (:6014), el reemplazo de api-cobranzas: usuarios con su token OAuth,
# caja del día por usuario y transacciones con lote. Mismo contrato (rutas, campos y mensajes)
# que src/cobranzas/ de cessa-api-siic-nest, sin Postgres ni AS400. Todos los usuarios tienen la
# clave "local"; el cliente OAuth es id 2 con secreto "local".
# ---------------------------------------------------------------------------------------------
PUERTO_COBRANZAS = int(os.environ.get("COBRANZAS_SIMULADO_PUERTO", "6014"))
CLAVE_SIMULADA = "local"
CLIENTE_OAUTH = ("2", "local")
USUARIOS_NEST = {
    # username: (id, nombre, rol, entidad_financiera_id, habilitado)
    "CABISAQR": (1, "CAJERO PAGOS WEB", "CAJA", 1, True),
    "CAJERA1": (2, "MARIA QUISPE MAMANI", "CAJA", 1, True),
    "CAJERA2": (3, "ROSA FLORES CHOQUE", "CAJA", 1, True),
    "CAJABNB": (4, "LUIS ARCE VARGAS", "CAJA", 4, True),
    "SUPERV": (5, "CARMEN ROJAS PAREDES", "COBRANZA", 1, True),
    "SISTEMAS1": (6, "JORGE TORREZ ZURITA", "SISTEMAS", 1, True),
    "CAJABAJA": (7, "PEDRO CHOQUE ARCE", "CAJA", 1, False),
}
ENTIDADES = {1: "CESSA", 4: "BANCO NACIONAL DE BOLIVIA"}
AGENCIAS = {
    1: [("CEN", "OFICINA CENTRAL", "CALLE ESPAÑA 73", [101, 102, 103]),
        ("WEB", "PAGOS WEB", "-", [8197]),
        ("ZNO", "AGENCIA ZONA NORTE", "AV. LAS AMERICAS 120", [201, 202])],
    4: [("BNB1", "BNB AGENCIA CENTRAL", "CALLE BOLIVAR 50", [401, 402])],
}
TOKENS: dict[str, dict] = {}  # token -> {"usuario": username | None, "refresh": token}
DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
# Altas y cambios hechos desde el panel (sobreviven a reinicios): username -> [id, nombre, rol, entidad, habilitado]
for _u, _f in ESTADO.get("nest_usuarios", {}).items():
    USUARIOS_NEST[_u] = tuple(_f)


def _clave(username: str) -> str:
    return ESTADO.get("nest_claves", {}).get(username, CLAVE_SIMULADA)


def _ficha_operador(username: str) -> dict:
    """OPERCOB del SIIC: horario como número HHMMSS sin cero inicial y vigencia AAAAMMDD."""
    base = {"hora_inicial": "80000", "hora_final": "180000", "habilitacion_inicio": "20260101",
            "habilitacion_fin": "20261231", **{f"habilitado_{d}": d not in ("sabado", "domingo") for d in DIAS}}
    return base | ESTADO.get("nest_fichas", {}).get(username, {})


def _guardar_usuario(username: str, fila: tuple, ficha: dict | None = None, clave: str | None = None) -> None:
    USUARIOS_NEST[username] = fila
    ESTADO.setdefault("nest_usuarios", {})[username] = list(fila)
    if ficha:
        ESTADO.setdefault("nest_fichas", {}).setdefault(username, {}).update(ficha)
    if clave:
        ESTADO.setdefault("nest_claves", {})[username] = clave
    _guardar_estado(ESTADO)


def _ficha_desde_cuerpo(d: dict) -> dict:
    ficha = {}
    for campo in ("hora_inicial", "hora_final"):
        if d.get(campo):
            ficha[campo] = str(int(str(d[campo]).replace(":", "")))
    for campo in ("habilitacion_inicio", "habilitacion_fin"):
        if d.get(campo):
            ficha[campo] = str(d[campo])
    for dia in DIAS:
        if f"habilitado_{dia}" in d:
            ficha[f"habilitado_{dia}"] = bool(d[f"habilitado_{dia}"])
    return ficha
# Catálogos GET /v1/entes y /v1/bancos (los ids reales salen del SIIC: estos son de ejemplo).
ENTES = {1: "CHEQUE", 2: "SIGMA", 3: "DEPOSITO", 4: "DIVIDENDO", 5: "TARJETA", 6: "DOLARES", 7: "EFECTIVO", 8: "PAGO POR QR"}
BANCOS = {1: "OTRO", 2: "BANCO BISA", 3: "BANCO NACIONAL DE BOLIVIA", 4: "BANCO MERCANTIL SANTA CRUZ", 5: "BANCO UNION"}
ENTE_EFECTIVO = {"B": 7, "D": 6}
LOTE_ENTES, LOTE_EFECTIVO = 9000, {"B": 9001, "D": 9002}
COTIZACION = Decimal("6.96")
CORTES = {"B": [("200", 200), ("100", 100), ("50", 50), ("20", 20), ("10", 10), ("5", 5), ("2", 2), ("1", 1),
                ("050", Decimal("0.5")), ("020", Decimal("0.2")), ("010", Decimal("0.1"))],
          "D": [("100", 100), ("50", 50), ("20", 20), ("10", 10), ("5", 5), ("2", 2), ("1", 1)]}


def _usuario_nest(username: str) -> dict | None:
    fila = USUARIOS_NEST.get(username)
    if fila is None:
        return None
    id_, nombre, rol, entidad, habilitado = fila
    caja = _caja_hoy(username)
    return {
        "id": id_, "nombre": nombre, "username": username, "rol": rol, "habilitado": habilitado,
        "entidad_financiera_id": entidad, "caja_codigo": caja["caja_codigo"] if caja else None,
        "fecha_apertura": caja["fecha_apertura"][:10] if caja else None, "caja_id": caja["id"] if caja else None,
    }


def _cajas() -> dict:
    return ESTADO.setdefault("nest_cajas", {})


def _caja_hoy(username: str) -> dict | None:
    return _cajas().get(f"{date.today():%Y-%m-%d}:{username}")


def _nuevo_token(usuario: str | None) -> dict:
    acceso, refresh = os.urandom(24).hex(), os.urandom(24).hex()
    TOKENS[acceso] = {"usuario": usuario, "refresh": refresh}
    TOKENS[f"refresh:{refresh}"] = {"usuario": usuario, "acceso": acceso}
    cuerpo = {"token_type": "Bearer", "expires_in": 31536000, "access_token": acceso}
    return cuerpo | ({"refresh_token": refresh} if usuario else {})


def _fila_ente(c: dict, d: dict, usuario: str) -> dict:
    return {"codigo_sucursal": "1", "fecha": c["fecha_apertura"][:10].replace("-", ""), "codigo_cajero": usuario,
            "caja_codigo": str(c["caja_codigo"]), "lote": str(d["lote"]), "indice": str(d["indice"]), "moneda": d["moneda"],
            "importe": f"{Decimal(str(d['importe'])):.2f}", "numero": str(d.get("numero") or ""),
            "fecha_registro": str(d.get("fecha_registro") or "0"), "fecha_vencimiento": str(d.get("fecha_vencimiento") or "0"),
            "ente_descripcion": ENTES.get(int(d["ente_id"]), "?"), "banco_descripcion": BANCOS.get(int(d["banco_id"]), "OTRO")}


def _arqueo(c: dict) -> dict:
    """Como CierreService de la Nest: efectivo (entes 7/6), cheques (1), dividendos (4), otros valores (el resto)."""
    suma = {k: Decimal(0) for k in ("efectivo_bs", "efectivo_sus", "cheques_bs", "cheques_sus", "dividendos_bs", "otros_valores")}
    for d in c.get("documentos", []):
        imp, ente, bs = Decimal(str(d["importe"])), int(d["ente_id"]), d["moneda"] == "B"
        if ente == 7:
            suma["efectivo_bs"] += imp
        elif ente == 6:
            suma["efectivo_sus"] += imp
        elif ente == 1:
            suma["cheques_bs" if bs else "cheques_sus"] += imp
        elif ente == 4:
            suma["dividendos_bs"] += imp
        else:
            suma["otros_valores"] += imp if bs else imp * COTIZACION
    fisico = (suma["efectivo_bs"] + suma["efectivo_sus"] * COTIZACION + suma["cheques_bs"] + suma["cheques_sus"] * COTIZACION
              + suma["otros_valores"] - suma["dividendos_bs"])
    recaudado = Decimal(c.get("total_cobrado", "0"))
    return {k: f"{v:.2f}" for k, v in suma.items()} | {"recaudado_bs": f"{recaudado:.2f}", "diferencia_bs": f"{fisico - recaudado:.2f}"}


class ManejadorCobranzas(Manejador):
    """Las rutas de la Nest de cobranzas que usa el gateway."""

    def _usuario(self) -> tuple[str | None, bool]:
        """(username del token o None si es de cliente, token válido)."""
        token = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        info = TOKENS.get(token)
        if not info:
            return None, False
        u = info["usuario"]
        if u and not USUARIOS_NEST[u][4]:
            return None, False
        return u, True

    def _cuerpo_form(self) -> dict:
        if "json" in (self.headers.get("Content-Type") or ""):
            return self._cuerpo()
        largo = int(self.headers.get("Content-Length") or 0)
        return {k: v[0] for k, v in parse_qs(self.rfile.read(largo).decode()).items()}

    def _pdf_ok(self, pdf: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(pdf)))
        self.end_headers()
        self.wfile.write(pdf)

    def _no_autenticado(self) -> None:
        self.send_response(401)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Unauthorized.")

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        m = re.fullmatch(r"/transacciones/([\w-]+)/documentos", url.path)
        if m:  # enlace público (solo PAGADAS)
            return self._documentos(m.group(1), q.get("formato", "json"), None)
        usuario, valido = self._usuario()
        if not valido:
            return self._no_autenticado()
        if url.path == "/v1/usuarios/existe":
            u = _usuario_nest(q.get("username", ""))
            return self._json(200, u) if u else self._json(404, {"error": "No existe un usuario con este username", "code": 404})
        if url.path == "/v1/entidades-financieras":
            return self._json(200, [{"id": k, "descripcion": v} for k, v in ENTIDADES.items()])
        m = re.fullmatch(r"/v1/entidades-financieras/(\d+)/agencias", url.path)
        if m:
            return self._json(200, [
                {"nro_agencia": i + 1, "sigla_agencia": s, "descripcion": d, "direccion": dir_, "entidad_financiera_id": int(m.group(1))}
                for i, (s, d, dir_, _) in enumerate(AGENCIAS.get(int(m.group(1)), []))
            ])
        if url.path == "/v1/consulta/deuda":
            return self._json(*_consulta_cliente(_nro(q)))
        m = re.fullmatch(r"/v1/proformas/(\d+)(/deudas)?", url.path)
        if m:
            return self._proforma(int(m.group(1)), bool(m.group(2)), q)
        if url.path in ("/v1/entes", "/v1/bancos"):
            return self._json(200, [{"id": k, "descripcion": v} for k, v in (ENTES if url.path == "/v1/entes" else BANCOS).items()])
        if usuario is None:
            return self._no_autenticado()
        if url.path == "/v1/usuarios" or url.path == "/v1/usuarios/caja-aperturada" or re.fullmatch(r"/v1/usuarios/\d+", url.path):
            return self._usuarios_get(usuario, url.path, q)
        if url.path == "/v1/cajas/entes-cierre-caja":
            c = _caja_hoy(usuario)
            return self._json(200, [_fila_ente(c, d, usuario) for d in c.get("documentos", [])] if c else [])
        if url.path == "/v1/cajas/datos-cierre":
            c = _caja_hoy(usuario)
            if c is None:
                return self._json(422, {"error": "El usuario no tiene una caja aperturada en la fecha de hoy. Debe primero aperturar la caja.", "code": 422})
            return self._json(200, _arqueo(c))
        if url.path == "/v1/cajas/reporte-cierre":
            return self._reporte(usuario, q.get("fecha", ""))
        if url.path == "/v1/cajas/existe":
            caja = _caja_hoy(usuario)
            if caja is None:
                return self._json(404, {"error": "El usuario no tiene una caja aperturada en esta fecha", "code": 404})
            return self._json(200, {k: caja[k] for k in ("id", "fecha_apertura", "fecha_cierre", "monto_inicial", "monto_final", "estado")} | {
                "codigo_cajero": usuario, "entidad_financiera_id": USUARIOS_NEST[usuario][3],
                "entidad_financiera_descripcion": ENTIDADES[USUARIOS_NEST[usuario][3]], "agencia_sigla": caja["agencia_sigla"],
                "agencia_descripcion": caja["agencia_descripcion"], "dispositivo_id": caja["caja_codigo"],
            })
        if url.path == "/v1/transacciones":
            return self._transacciones(usuario, q)
        m = re.fullmatch(r"/v1/transacciones/([\w-]+)", url.path)
        if m:
            t = ESTADO.get("nest_transacciones", {}).get(m.group(1))
            if t is None:
                return self._json(400, {"error": f"No existe el pago con el UUID {m.group(1)}", "code": 400})
            if USUARIOS_NEST[usuario][2] == "CAJA" and t["usuario"] != usuario:
                return self._json(403, {"error": "Usted no está autorizado para ver el detalle de esta transacción", "code": 403})
            items = ESTADO.get("transacciones", {}).get(t["uuid"], {}).get("items", [])
            return self._json(200, t | {"detalle": items})
        m = re.fullmatch(r"/v1/transacciones/([\w-]+)/(estado|documentos)", url.path)
        if m:
            t = ESTADO.get("nest_transacciones", {}).get(m.group(1))
            if t is None:
                return self._json(400, {"error": f"No existe el pago con el UUID {m.group(1)}", "code": 400})
            if m.group(2) == "estado":
                return self._json(200, {k: t[k] for k in ("uuid", "estado", "lote", "caja_codigo", "fecha_creacion", "fecha_pago")})
            return self._documentos(m.group(1), q.get("formato", "json"), usuario)
        self._json(404, {"error": "ruta no simulada", "ruta": url.path})

    def _documentos(self, uuid: str, formato: str, usuario: str | None):
        t = ESTADO.get("nest_transacciones", {}).get(uuid)
        if t is None or t["estado"] != "PAGADA":
            return self._json(403, {"error": "No se puede visualizar los comprobantes de la transacción.", "code": 403})
        if usuario and t["usuario"] != usuario:
            return self._json(403, {"error": "Usted no está autorizado para acceder a los comprobantes de la transacción.", "code": 403})
        if formato == "pdf":
            return self._pdf_ok(_pdf_transaccion(uuid) or _pdf(["Sin comprobante"]))
        return self._json(200, [_comprobante_transaccion(uuid) or {}])

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/oauth/token":
            return self._token(self._cuerpo_form())
        usuario, valido = self._usuario()
        if not valido or usuario is None:
            return self._no_autenticado()
        if url.path == "/v1/cajas/aperturar":
            return self._aperturar(usuario, self._cuerpo())
        if url.path == "/v1/usuarios":
            return self._usuario_alta(usuario, self._cuerpo())
        if url.path == "/v1/cambiar-contrasenia":
            d = self._cuerpo()
            if len(str(d.get("password_nuevo") or "")) < 6:
                return self._json(422, {"error": {"password_nuevo": ["The password nuevo must be at least 6 characters."]}, "code": 422})
            if d.get("password_actual") != _clave(usuario):
                return self._json(401, {"error": "El usuario o contraseña son incorrectos", "code": 401})
            _guardar_usuario(usuario, USUARIOS_NEST[usuario], clave=str(d["password_nuevo"]))
            return self._json(200, _usuario_nest(usuario))
        if url.path in ("/v1/cajas/entes-cierre-caja/agregar", "/v1/cajas/entes-cierre-caja/modificar"):
            return self._ente(usuario, self._cuerpo(), url.path.endswith("modificar"))
        if url.path == "/v1/cajas/cerrar":
            return self._cerrar(usuario, self._cuerpo())
        m = re.fullmatch(r"/v1/transacciones/([\w-]+)/anular", url.path)
        if m:
            return self._anular(usuario, m.group(1))
        if url.path == "/v1/transacciones":
            caja = _caja_hoy(usuario)
            if USUARIOS_NEST[usuario][2] != "CAJA":
                return self._json(403, {"error": "El rol de usuario no está autorizado para crear una transacción", "code": 403})
            if caja is None:
                return self._json(422, {"error": "Para realizar transacciones debe primero aperturar una caja", "code": 422})
            if caja["estado"] == "CERRADA":
                return self._json(422, {"error": "Ya no puede crear una transacción debido a que la caja del día de hoy ha sido cerrada.", "code": 422})
            with _candado:
                caja["ultimo_lote"] += 1
                uuid = str(__import__("uuid").uuid4())
                ESTADO.setdefault("nest_transacciones", {})[uuid] = {
                    "uuid": uuid, "estado": "CREADA", "usuario": usuario, "caja_codigo": caja["caja_codigo"],
                    "lote": caja["ultimo_lote"], "fecha_creacion": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "fecha_pago": None,
                }
                _guardar_estado(ESTADO)
            t = ESTADO["nest_transacciones"][uuid]
            return self._json(201, {"uuid": uuid, "estado": "CREADA", "usuario_id": USUARIOS_NEST[usuario][0],
                                    "caja_codigo": t["caja_codigo"], "fecha_creacion": t["fecha_creacion"], "lote": t["lote"]})
        self._json(404, {"error": "ruta no simulada", "ruta": url.path})

    def do_PUT(self):
        url = urlparse(self.path)
        usuario, valido = self._usuario()
        if not valido or usuario is None:
            return self._no_autenticado()
        if url.path == "/v1/cajas/entes-cierre-caja/actualizar-efectivo":
            return self._efectivo(usuario, self._cuerpo())
        m = re.fullmatch(r"/v1/transacciones/([\w-]+)/(pagar|pagar-otro-documento)", url.path)
        if not m:
            return self._json(404, {"error": "ruta no simulada", "ruta": url.path})
        cuerpo = self._cuerpo()
        if not cuerpo.get("detalle"):
            return self._json(422, {"error": {"detalle": ["The detalle field is required."]}, "code": 422})
        caja = _caja_hoy(usuario)
        if caja is None or caja["estado"] != "ABIERTA":
            return self._json(422, {"error": "Para realizar transacciones debe primero aperturar una caja", "code": 422})
        t = ESTADO.get("nest_transacciones", {}).get(m.group(1))
        if t is None:
            return self._json(400, {"error": f"No existe el pago con el UUID {m.group(1)}", "code": 400})
        if t["usuario"] != usuario:
            return self._json(401, {"error": "No está autorizado, sus credenciales no son las correctas", "code": 401})
        if t["estado"] != "CREADA":
            return self._json(401, {"error": f"La transacción no se puede completar porque ya está {t['estado']}.", "code": 401})
        # El SIIC exige pagar desde el comprobante más antiguo de cada cliente.
        for nro in {str(i.get("nro_cliente")) for i in cuerpo["detalle"]}:
            deuda = [i["nro_comprobante"] for i in (_cliente(nro) or {"deuda": []})["deuda"]]
            pedidos = [str(i.get("nro_comprobante")) for i in cuerpo["detalle"] if str(i.get("nro_cliente")) == nro]
            if pedidos != deuda[: len(pedidos)]:
                return self._json(422, {"error": {"nro_cliente": nro, "error_mensaje": "La deuda ha cambiado o no se paga desde la más antigua."}, "code": 422})
        _marcar_pagados(cuerpo["detalle"], t["uuid"])
        total = sum((Decimal(str(i.get("importe", 0))) * (-1 if i.get("debito_credito") == "CREDITO" else 1) for i in cuerpo["detalle"]), Decimal("0"))
        with _candado:
            t.update(estado="PAGADA", fecha_pago=f"{datetime.now():%Y-%m-%d %H:%M:%S}", total_pagado=f"{total:.2f}",
                     total_comprobantes=len(cuerpo["detalle"]), documento=cuerpo.get("documento"))
            caja["total_cobrado"] = f"{Decimal(caja.get('total_cobrado', '0')) + total:.2f}"
            doc = cuerpo.get("documento") if m.group(2) == "pagar-otro-documento" else None
            if doc:  # TCNN3051: el documento queda en el cierre con el lote del pago
                caja.setdefault("documentos", []).append({**doc, "lote": t["lote"], "indice": 1})
            _guardar_estado(ESTADO)
        return self._json(200, {k: t[k] for k in ("estado", "fecha_pago", "total_comprobantes", "total_pagado", "lote")})

    def _caja_abierta(self, usuario: str) -> dict | None:
        c = _caja_hoy(usuario)
        if c is None:
            self._json(422, {"error": "El usuario no tiene una caja aperturada en la fecha de hoy. Debe primero aperturar la caja.", "code": 422})
            return None
        if c["estado"] != "ABIERTA":
            self._json(422, {"error": "La caja ya fue cerrada: no se puede modificar.", "code": 422})
            return None
        return c

    def _efectivo(self, usuario: str, d: dict):
        moneda = d.get("moneda")
        if moneda not in CORTES:
            return self._json(422, {"error": {"moneda": ["The selected moneda is invalid."]}, "code": 422})
        prefijo = "corte_bs_" if moneda == "B" else "corte_sus_"
        faltan = {f"{prefijo}{c}": [f"The {prefijo}{c} field is required."]
                  for c, _ in CORTES[moneda] if not str(d.get(f"{prefijo}{c}", "")).strip().isdigit()}
        if faltan:
            return self._json(422, {"error": faltan, "code": 422})
        c = self._caja_abierta(usuario)
        if c is None:
            return None
        importe = sum((int(d[f"{prefijo}{k}"]) * Decimal(str(v)) for k, v in CORTES[moneda]), Decimal(0))
        with _candado:
            docs = [x for x in c.setdefault("documentos", []) if x["lote"] != LOTE_EFECTIVO[moneda]]
            docs.insert(0, {"ente_id": ENTE_EFECTIVO[moneda], "moneda": moneda, "banco_id": 1, "numero": "",
                            "importe": f"{importe:.2f}", "lote": LOTE_EFECTIVO[moneda], "indice": 1})
            c["documentos"] = docs
            c.setdefault("cortes", {}).update({k: int(v) for k, v in d.items() if k.startswith(prefijo)})
            _guardar_estado(ESTADO)
        campo = "monto_efectivo_bs" if moneda == "B" else "monto_efectivo_sus"
        return self._json(200, {"id": c["id"], "estado": c["estado"], campo: f"{importe:.2f}", **c["cortes"]})

    def _ente(self, usuario: str, d: dict, modificar: bool):
        errores = {k: [f"The {k} field is required."] for k in ("ente_id", "moneda", "banco_id", "importe", "numero")
                   if str(d.get(k, "")).strip() == ""}
        if modificar and str(d.get("indice", "")).strip() == "":
            errores["indice"] = ["The indice field is required."]
        if not errores and Decimal(str(d["importe"])) < 1:
            errores["importe"] = ["The importe must be at least 1."]
        if errores:
            return self._json(422, {"error": errores, "code": 422})
        if int(d["ente_id"]) not in ENTES:
            return self._json(403, {"error": "El Ente no se encuentra registrado.", "code": 403})
        c = self._caja_abierta(usuario)
        if c is None:
            return None
        doc = {k: d.get(k) for k in ("ente_id", "moneda", "banco_id", "importe", "numero", "fecha_registro", "fecha_vencimiento")}
        with _candado:
            docs = c.setdefault("documentos", [])
            if modificar:
                previo = next((x for x in docs if x["lote"] == LOTE_ENTES and str(x["indice"]) == str(d["indice"])), None)
                if previo is None:
                    return self._json(404, {"error": "No existe el documento del cierre.", "code": 404})
                previo.update(doc)
                indice = previo["indice"]
            else:
                indice = 1 + max((x["indice"] for x in docs if x["lote"] == LOTE_ENTES), default=0)
                docs.append({**doc, "lote": LOTE_ENTES, "indice": indice})
            _guardar_estado(ESTADO)
        return self._json(200, {**d, "lote": str(LOTE_ENTES), "indice": str(indice)})

    def _cerrar(self, usuario: str, d: dict):
        c = self._caja_abierta(usuario)
        if c is None:
            return None
        with _candado:
            c.update(estado="CERRADA", fecha_cierre=f"{datetime.now():%Y-%m-%d %H:%M:%S}",
                     descripcion_cierre=str(d.get("descripcion_cierre") or ""))
            _guardar_estado(ESTADO)
        a = _arqueo(c)
        return self._json(200, {"id": c["id"], "estado": "CERRADA", "fecha_cierre": c["fecha_cierre"],
                                "monto_final": a["recaudado_bs"], "monto_efectivo_bs": a["efectivo_bs"]})

    def _reporte(self, usuario: str, f: str):
        c = _cajas().get(f"{f[:4]}-{f[4:6]}-{f[6:8]}:{usuario}") if re.fullmatch(r"\d{8}", f) else None
        if c is None:
            return self._json(422, {"error": "No tiene una caja abierta en la fecha seleccionada.", "code": 422})
        if c["estado"] == "ABIERTA":
            return self._json(422, {"error": "No se puede obtener el reporte de cierre porque la caja aun está abierta.", "code": 422})
        a = _arqueo(c)
        filas = [_fila_ente(c, d, usuario) for d in c.get("documentos", [])]
        return self._pdf_ok(_pdf([
            "CESSA - REPORTE DE CIERRE DE CAJA (SIMULADO)", "",
            f"Cajero: {usuario}   Caja: {c['caja_codigo']}   Agencia: {c['agencia_descripcion']}",
            f"Apertura: {c['fecha_apertura']}   Cierre: {c['fecha_cierre']}", "",
            *[f"{x['ente_descripcion']} {x['numero']} lote {x['lote']}: Bs {x['importe']}" for x in filas],
            "", f"Efectivo Bs: {a['efectivo_bs']}   Otros valores: {a['otros_valores']}",
            f"Total contable (recaudado): {a['recaudado_bs']}", f"Diferencia: {a['diferencia_bs']}",
        ]))

    def do_PATCH(self):
        url = urlparse(self.path)
        usuario, valido = self._usuario()
        if not valido or usuario is None:
            return self._no_autenticado()
        m = re.fullmatch(r"/v1/usuarios/(\w+)", url.path)
        if not m:
            return self._json(404, {"error": "ruta no simulada", "ruta": url.path})
        if USUARIOS_NEST[usuario][2] != "SISTEMAS":
            return self._json(403, {"error": "El rol de usuario no está autorizado para actualizar datos de usuario", "code": 403})
        objetivo = m.group(1)
        if objetivo not in USUARIOS_NEST:
            return self._json(404, {"error": f"No existe el usuario con el username {objetivo}", "code": 404})
        d = self._cuerpo()
        id_, nombre, rol, entidad, habilitado = USUARIOS_NEST[objetivo]
        fila = (id_, str(d.get("nombre") or nombre), str(d.get("rol") or rol),
                int(d.get("entidad_financiera_id") or entidad), bool(d["habilitado"]) if "habilitado" in d else habilitado)
        _guardar_usuario(objetivo, fila, _ficha_desde_cuerpo(d), str(d["password"]) if d.get("password") else None)
        if not fila[4] or d.get("password"):  # la Nest corta las sesiones al deshabilitar o cambiar la clave
            for token in [t for t, info in TOKENS.items() if info.get("usuario") == objetivo]:
                TOKENS.pop(token, None)
        return self._json(200, _usuario_nest(objetivo))

    def _usuarios_get(self, usuario: str, ruta: str, q: dict):
        rol, entidad = USUARIOS_NEST[usuario][2], USUARIOS_NEST[usuario][3]
        if rol not in ("SISTEMAS", "COBRANZA", "ENTIDAD_FINANCIERA"):
            return self._json(403, {"error": "El rol de usuario no está autorizado para listar usuarios", "code": 403})
        filtro = entidad if rol == "ENTIDAD_FINANCIERA" else int(q.get("entidad_financiera_id") or 0)
        if ruta == "/v1/usuarios/caja-aperturada":
            return self._json(200, [
                {"id": f[0], "username": u, "caja_codigo": _caja_hoy(u)["caja_codigo"], "entidad_financiera_id": f[3]}
                for u, f in USUARIOS_NEST.items()
                if f[2] == "CAJA" and _caja_hoy(u) and _caja_hoy(u)["estado"] == "ABIERTA" and (not filtro or f[3] == filtro)
            ])
        if ruta == "/v1/usuarios":
            return self._json(200, [_usuario_nest(u) for u, f in USUARIOS_NEST.items() if not filtro or f[3] == filtro])
        id_ = int(ruta.rsplit("/", 1)[1])
        username = next((u for u, f in USUARIOS_NEST.items() if f[0] == id_), None)
        if username is None or (filtro and USUARIOS_NEST[username][3] != filtro):
            return self._json(404, {"error": "Does not exist any instance of usuario with the given id", "code": 404})
        return self._json(200, _usuario_nest(username) | _ficha_operador(username) | {"codigo_entidad_financiera_id": USUARIOS_NEST[username][3]})

    def _usuario_alta(self, usuario: str, d: dict):
        if USUARIOS_NEST[usuario][2] != "SISTEMAS":
            return self._json(403, {"error": "El rol de usuario no está autorizado para guardar datos de usuario", "code": 403})
        requeridos = ["nombre", "username", "password", "rol", "hora_inicial", "hora_final", "habilitacion_inicio",
                      "habilitacion_fin", "entidad_financiera_id"]
        errores = {c: [f"The {c.replace('_', ' ')} field is required."] for c in requeridos if str(d.get(c, "")).strip() == ""}
        if errores:
            return self._json(422, {"error": errores, "code": 422})
        username = str(d["username"]).upper()
        if len(username) > 10:
            return self._json(422, {"error": {"username": ["The username may not be greater than 10 characters."]}, "code": 422})
        if username in USUARIOS_NEST:
            return self._json(422, {"error": {"username": ["The username has already been taken."]}, "code": 422})
        if int(d["entidad_financiera_id"]) not in ENTIDADES:
            return self._json(400, {"error": "No existe la entidad financiera.", "code": 400})
        fila = (max(f[0] for f in USUARIOS_NEST.values()) + 1, str(d["nombre"]), str(d["rol"]), int(d["entidad_financiera_id"]),
                bool(d.get("habilitado", True)))
        _guardar_usuario(username, fila, _ficha_desde_cuerpo(d), str(d["password"]))
        return self._json(201, _usuario_nest(username))

    def _transacciones(self, usuario: str, q: dict):
        rol = USUARIOS_NEST[usuario][2]
        if rol in ("COBRANZA", "ENTIDAD_FINANCIERA"):
            if not (q.get("usuario_id") and q.get("fecha_inicio") and q.get("fecha_fin")):
                return self._json(400, {"error": "Los parámetros utilizados son incorrectos", "code": 400})
            dueno = next((u for u, f in USUARIOS_NEST.items() if str(f[0]) == str(q["usuario_id"])), None)
            if dueno is None:
                return self._json(401, {"error": f"No existe el usuario con ID #{q['usuario_id']}", "code": 401})
        elif rol == "SISTEMAS":
            dueno = next((u for u, f in USUARIOS_NEST.items() if str(f[0]) == str(q.get("usuario_id"))), usuario)
        else:
            dueno = usuario
        desde, hasta = q.get("fecha_inicio", ""), q.get("fecha_fin", "")
        filas = [
            {k: t.get(k) for k in ("uuid", "estado", "lote", "caja_codigo", "fecha_creacion", "fecha_pago", "fecha_anulacion", "total_pagado")}
            | {"usuario": {"id": USUARIOS_NEST[dueno][0], "username": dueno}}
            for t in ESTADO.get("nest_transacciones", {}).values()
            if t["usuario"] == dueno and desde <= t["fecha_creacion"][:10] <= hasta
        ]
        return self._json(200, sorted(filas, key=lambda f: f["fecha_creacion"], reverse=True))

    def _proforma(self, numero: int, deudas: bool, q: dict):
        """Proforma N: 6 medidores de una empresa inventada (clientes 1NN001..1NN006 con NN = N)."""
        if not 1 <= numero <= 99:
            return self._json(404, {"error": f"No existe ninguna proforma con número {numero}", "code": 404})
        nros = [str(100000 + numero * 1000 + i) for i in range(1, 7)]
        if not deudas:
            clientes = []
            for nro in nros:
                _, datos = _consulta_cliente(nro)
                clientes.append({k: datos.get(k) for k in ("nro_cliente", "nombre", "direccion", "categoria_descripcion", "nro_medidor")})
            return self._json(200, {"numero": str(numero), "descripcion": f"EMPRESA DEMO {numero} S.R.L.", "clientes": clientes})
        try:
            hasta = int(q.get("anio", "0")) * 100 + int(q.get("mes", "0"))
        except ValueError:
            return self._json(422, {"error": {"anio": ["The anio must be a number."]}, "code": 422})
        filas = []
        for nro in nros:
            items = [i for i in (_cliente(nro) or {"deuda": []})["deuda"] if int(i["anio"]) * 100 + int(i["mes"]) <= hasta]
            if items:
                filas.append({"nro_cliente": nro, "deudas": items})
        if not filas:
            return self._json(404, {"error": f"No existe deuda pendiente en la proforma #{numero}", "code": 404})
        return self._json(200, filas)

    def _anular(self, usuario: str, uuid: str):
        if USUARIOS_NEST[usuario][2] == "CAJA":
            return self._json(403, {"error": "El rol de usuario no está autorizado para anular una transacción", "code": 403})
        t = ESTADO.get("nest_transacciones", {}).get(uuid)
        if t is None:
            return self._json(400, {"error": f"No existe el pago con el UUID {uuid}", "code": 400})
        if t["fecha_creacion"][:10] != f"{date.today():%Y-%m-%d}":
            return self._json(401, {"error": f"La transacción ha expirado, ésta ha sido creada en fecha y hora {t['fecha_creacion']}", "code": 401})
        if t["estado"] == "ANULADA":
            return self._json(401, {"error": "La transacción no se puede anular porque ya ha sido anulada", "code": 401})
        if t["estado"] != "PAGADA":
            return self._json(200, t)
        with _candado:
            items = ESTADO.get("transacciones", {}).get(uuid, {}).get("items", [])
            for item in items:  # la deuda vuelve a quedar pendiente
                ESTADO["pagados"].get(str(item.get("nro_cliente")), {}).pop(str(item.get("nro_comprobante")), None)
            caja = _cajas().get(f"{t['fecha_creacion'][:10]}:{t['usuario']}")
            if caja:
                caja["total_cobrado"] = f"{Decimal(caja.get('total_cobrado', '0')) - Decimal(t.get('total_pagado', '0')):.2f}"
                caja["documentos"] = [d for d in caja.get("documentos", []) if d["lote"] != t["lote"]]
            t.update(estado="ANULADA", fecha_anulacion=f"{datetime.now():%Y-%m-%d %H:%M:%S}", anulada_por=usuario)
            _guardar_estado(ESTADO)
        return self._json(200, {k: t.get(k) for k in ("uuid", "estado", "lote", "caja_codigo", "fecha_creacion", "fecha_anulacion")})

    def _token(self, d: dict):
        if (str(d.get("client_id")), str(d.get("client_secret"))) != CLIENTE_OAUTH:
            return self._json(401, {"error": "invalid_client", "error_description": "Client authentication failed", "message": "Client authentication failed"})
        grant = d.get("grant_type")
        if grant == "client_credentials":
            return self._json(200, _nuevo_token(None))
        if grant == "password":
            fila = USUARIOS_NEST.get(str(d.get("username", "")))
            if fila is None or not fila[4] or d.get("password") != _clave(str(d["username"])):
                return self._json(400, {"error": "invalid_grant", "error_description": "The user credentials were incorrect.", "message": "The user credentials were incorrect."})
            return self._json(200, _nuevo_token(str(d["username"])))
        if grant == "refresh_token":
            info = TOKENS.pop(f"refresh:{d.get('refresh_token')}", None)
            if info is None or not USUARIOS_NEST[info["usuario"]][4]:
                return self._json(401, {"error": "invalid_request", "error_description": "The refresh token is invalid.", "message": "The refresh token is invalid."})
            TOKENS.pop(info["acceso"], None)
            return self._json(200, _nuevo_token(info["usuario"]))
        return self._json(400, {"error": "unsupported_grant_type", "message": "The authorization grant type is not supported by the authorization server."})

    def _aperturar(self, usuario: str, d: dict):
        id_, _, rol, entidad, _ = USUARIOS_NEST[usuario]
        if rol != "CAJA":
            return self._json(403, {"error": "El rol de usuario no está autorizado para aperturar cajas", "code": 403})
        if not d.get("agencia_sigla"):
            return self._json(422, {"error": {"agencia_sigla": ["The agencia sigla field is required."]}, "code": 422})
        previa = _caja_hoy(usuario)
        if previa:
            hora = (previa["fecha_cierre"] or previa["fecha_apertura"])[11:19]
            return self._json(422, {"error": (f"No puede aperturar caja porque ya cuenta con una caja abierta a las {hora}"
                                              if previa["estado"] == "ABIERTA" else
                                              f"No puede aperturar caja porque su caja ya fue cerrada a las {hora}"), "code": 422})
        agencia = next((a for a in AGENCIAS.get(entidad, []) if a[0] == d["agencia_sigla"]), None)
        if agencia is None:
            return self._json(400, {"error": f"No existe la agencia {d['agencia_sigla']} para su entidad financiera.", "code": 400})
        hoy_txt = f"{date.today():%Y-%m-%d}"
        usadas = {c["caja_codigo"] for k, c in _cajas().items() if k.startswith(hoy_txt)}
        libre = next((c for c in agencia[3] if c not in usadas), None)
        if libre is None:
            return self._json(400, {"error": f"No se puede aperturar porque no existen cajas asignadas para la agencia {agencia[1]}.", "code": 400})
        with _candado:
            _cajas()[f"{hoy_txt}:{usuario}"] = {
                "id": len(_cajas()) + 1, "caja_codigo": libre, "agencia_sigla": agencia[0], "agencia_descripcion": agencia[1],
                "fecha_apertura": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "fecha_cierre": None,
                "monto_inicial": f"{Decimal(str(d.get('monto_inicial') or 0)):.2f}", "monto_final": "0.00",
                "estado": "ABIERTA", "ultimo_lote": 0, "total_cobrado": "0.00",
            }
            _guardar_estado(ESTADO)
        return self._json(201, {"fecha": f"{date.today():%Y%m%d}", "operador_codigo": usuario, "caja_codigo": libre,
                                "agencia_sigla": agencia[0], "entidad_codigo": entidad, "entidad_descripcion": ENTIDADES[entidad]})


if __name__ == "__main__":
    print(f"SIIC simulado en :{PUERTO} y Nest de cobranzas en :{PUERTO_COBRANZAS} (estado en {ESTADO_ARCHIVO})", flush=True)
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", PUERTO_COBRANZAS), ManejadorCobranzas).serve_forever,
                     daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PUERTO), Manejador).serve_forever()
