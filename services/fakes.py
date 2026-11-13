"""Implementaciones de prueba para desarrollo local sin credenciales reales.

Apuntar acá `MC4_CLIENT_CLASS`/`DEUDA_CLIENT_CLASS` (vía `.env`) para poder
correr el flujo completo (consultar deuda -> generar QR -> confirmar pago ->
factura) sin red ni credenciales de MC4/SIIC. Nunca usar en producción.
"""
from __future__ import annotations

import base64
import hashlib
import io
import random
from datetime import date, datetime
from decimal import Decimal

from .cobranzas_banco_client import CobranzasBancoClientInterface
from .deuda_client import DeudaClientInterface, ItemDeuda, ResultadoConsultaDeuda
from .mc4_client import (
    EstadoPagoMC4,
    EstadoTransaccionQR,
    MC4ClientInterface,
    ResultadoQR,
    SolicitudQR,
)

# Compartido con el comando `generar_datos_demo`: nombres de ejemplo para que
# las demos no muestren siempre el mismo cliente.
NOMBRES_DEMO = [
    "Maria Fernandez Quispe", "Juan Perez Mamani", "Rosa Quispe Choque", "Carlos Mamani Condori",
    "Ana Flores Rojas", "Pedro Choque Huanca", "Lucia Condori Apaza", "Miguel Rios Vargas",
    "Elena Vargas Torrez", "Jorge Torrez Gutierrez", "Patricia Gutierrez Salazar", "Fernando Salazar Aguilar",
    "Carmen Aguilar Rocha", "Ricardo Rocha Penaranda", "Silvia Penaranda Escobar", "Alberto Escobar Cardenas",
    "Teresa Cardenas Guzman", "Oscar Guzman Zambrana", "Beatriz Zambrana Ortiz", "Raul Ortiz Medina",
    "Gloria Medina Chavez", "Hugo Chavez Ferrufino", "Marta Ferrufino Villca", "Victor Villca Poma",
    "Nora Poma Yucra", "Ramiro Yucra Callisaya", "Isabel Callisaya Mollo", "Sergio Mollo Colque",
    "Veronica Colque Alvarez", "Freddy Alvarez Baptista", "Cecilia Baptista Vaca", "Marco Vaca Justiniano",
    "Gabriela Justiniano Suarez", "Edwin Suarez Rivero", "Daniela Rivero Montano", "Luis Montano Cuellar",
    "Andrea Cuellar Anez", "Julio Anez Melgar", "Paola Melgar Rivera", "David Rivera Soliz",
]


class FakeMC4Client(MC4ClientInterface):
    """Genera un QR falso (se ve como uno real, pero solo dice que es de
    prueba: ningún banco lo acepta); la primera vez que se consulta el estado de un
    alias reporta "pendiente", de ahí en adelante "pagado" -- simula el
    tiempo real que tarda un cliente en escanear y pagar. El set es de
    clase (no de instancia) porque `get_mc4_client()` crea una instancia
    nueva por llamado y necesitamos que el "ya lo consulté antes" persista
    entre llamados, como pasaría con el estado real de MC4."""

    _alias_ya_consultados: set[str] = set()

    def generar_qr(self, solicitud: SolicitudQR) -> ResultadoQR:
        import segno

        imagen = io.BytesIO()
        texto = f"QR DE PRUEBA CESSA - NO VALIDO PARA PAGAR - {solicitud.alias} - Bs {solicitud.monto}"
        segno.make(texto, error="m").save(imagen, kind="png", scale=8, border=2)
        return ResultadoQR(
            imagen_qr_base64=base64.b64encode(imagen.getvalue()).decode(),
            id_qr=f"fake-qr-{random.randint(1000, 9999)}",
            id_transaccion=f"fake-op-{random.randint(1000, 9999)}",
            fecha_vencimiento=datetime.strptime(solicitud.fecha_vencimiento.strftime("%d/%m/%Y"), "%d/%m/%Y"),
            banco_destino="Banco BISA (demo)",
            cuenta_destino="0000000000",
        )

    def inhabilitar_qr(self, alias: str) -> None:
        self._alias_ya_consultados.discard(alias)

    def consultar_estado(self, alias: str) -> EstadoTransaccionQR:
        ya_consultado = alias in self._alias_ya_consultados
        self._alias_ya_consultados.add(alias)
        estado = EstadoPagoMC4.PAGADO if ya_consultado else EstadoPagoMC4.PENDIENTE

        return EstadoTransaccionQR(
            alias=alias,
            estado=estado,
            fecha_procesamiento=datetime.now() if ya_consultado else None,
            monto=None,
            numero_orden_originante="demo",
            id_qr=None,
        )


class FakeDeudaClient(DeudaClientInterface):
    """Devuelve una deuda con nombre/monto variados pero estables por código
    (el mismo código siempre da el mismo resultado) -- para que una demo no
    muestre siempre el mismo cliente y el mismo monto."""

    def consultar_deuda(self, codigo_externo: str) -> ResultadoConsultaDeuda:
        semilla = int(hashlib.sha256(codigo_externo.encode()).hexdigest(), 16)
        nombre = NOMBRES_DEMO[semilla % len(NOMBRES_DEMO)]
        importe = Decimal(5000 + semilla % 75000) / 100  # Bs. 50.00 - 800.00
        hoy = date.today()

        return ResultadoConsultaDeuda(
            codigo_externo_cliente=codigo_externo,
            nombre_cliente=nombre,
            items=[
                ItemDeuda(
                    codigo_sucursal="01",
                    nro_comprobante="000001",
                    nro_suministro=codigo_externo,
                    fecha=hoy.strftime("%Y%m%d"),
                    tipo="FC",
                    letra_comprobante="A",
                    nro_autorizacion="0",
                    nro_cliente=codigo_externo,
                    anio=hoy.year,
                    mes=hoy.month,
                    importe=importe,
                    detalle="Consumo de energía (demo)",
                    debito_credito="DEBITO",
                )
            ],
        )


class FakeCobranzasBancoClient(CobranzasBancoClientInterface):
    """Simula api-cobranzas-bancos: siempre tiene caja abierta, genera un
    uuid falso por transacción y un PDF stub -- no valida `detalle`/
    `documento` contra ningún catálogo real (ente_id/banco_id de settings
    igual se usan tal cual, para poder probar que llegan armados)."""

    def asegurar_caja_abierta(self) -> None:
        return None

    def crear_transaccion(self) -> str:
        return f"fake-transaccion-{random.randint(100000, 999999)}"

    def pagar_transaccion(self, uuid: str, detalle: list[dict], documento: dict) -> None:
        return None

    def pagar_transaccion_propia(self, uuid: str, detalle: list[dict]) -> None:
        return None

    def obtener_comprobante_pdf(self, uuid: str) -> bytes:
        # Stub mínimo válido como bytes de PDF -- no se renderiza nunca en la
        # demo, solo hace falta que sea un `bytes` no vacío para almacenar.
        return b"%PDF-1.4 (comprobante demo)\n%%EOF"

    def obtener_comprobante_json(self, uuid: str) -> dict:
        return {
            "nro_factura": f"DEMO-{uuid}",
            "cliente_nombre": "Cliente Demo",
            "total_pagar": 0,
            "detalle": [],
        }


class SimuladorCobranzasBancoClient(FakeCobranzasBancoClient):
    """Como FakeCobranzasBancoClient, pero lo pagado se avisa al SIIC simulado de
    local/siic_simulado.py (SIIC_DEUDA_BASE_URL): el comprobante deja de figurar como deuda
    y pasa a "Facturas anteriores", igual que en el SIIC real. El comprobante (PDF y JSON) de
    cada transacción también sale del simulador, con N° de factura. Solo para desarrollo local."""

    def _url(self, ruta: str) -> str:
        from django.conf import settings

        return f"{settings.SIIC_DEUDA_BASE_URL}/_simulador/{ruta}"

    def _request_autenticado(self, metodo: str, ruta: str, **kwargs):
        """Como api-cobranzas-bancos: `/v1/consulta/deuda` es un pase directo al
        `/v1/consulta/cliente` del SIIC (acá, el simulado). Lo usan la consulta de deuda "como
        banco" (web, bot, kiosco) y la verificación de deuda antes de liquidar un pago web."""
        import requests
        from django.conf import settings

        from services.cobranzas_banco_client import CobranzasBancoError

        if metodo.lower() == "get" and ruta == "/v1/consulta/deuda":
            return requests.get(
                f"{settings.SIIC_DEUDA_BASE_URL}/v1/consulta/cliente", params=kwargs.get("params"), timeout=10
            )
        raise CobranzasBancoError(f"El banco simulado no implementa {metodo.upper()} {ruta}")

    def _avisar_pago(self, uuid: str, detalle: list[dict]) -> None:
        import requests

        requests.post(self._url("pagar"), json={"uuid": uuid, "items": detalle}, timeout=5)

    def pagar_transaccion(self, uuid: str, detalle: list[dict], documento: dict) -> None:
        self._avisar_pago(uuid, detalle)

    def pagar_transaccion_propia(self, uuid: str, detalle: list[dict]) -> None:
        self._avisar_pago(uuid, detalle)

    def obtener_comprobante_pdf(self, uuid: str) -> bytes:
        import requests

        r = requests.get(self._url(f"transacciones/{uuid}/pdf"), timeout=5)
        return r.content if r.ok else super().obtener_comprobante_pdf(uuid)

    def obtener_comprobante_json(self, uuid: str) -> dict:
        import requests

        r = requests.get(self._url(f"transacciones/{uuid}"), timeout=5)
        return r.json() if r.ok else super().obtener_comprobante_json(uuid)
