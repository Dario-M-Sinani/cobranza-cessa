"""Muestra, SOLO LECTURA, las últimas liquidaciones de pagos web y su estado en api-cobranzas.

    python manage.py ver_liquidaciones                    # las 5 más nuevas
    python manage.py ver_liquidaciones --cliente 115997   # de un cliente
    python manage.py ver_liquidaciones --alias CESSA-WEB-20261006182749-PGD1 --detalle
    python manage.py ver_liquidaciones --cliente 115997 --remoto   # + GET de la transacción en api-cobranzas

Para diagnosticar un pago sin abrir el shell: estado acá, error, documentos enviados y (con
--remoto) lo que dice api-cobranzas-bancos de esa transacción (estado, lote, caja, total).
Nunca crea, paga ni anula nada.
"""

from django.core.management.base import BaseCommand

from apps.facturacion_externa.models import SolicitudLiquidacion
from services.cobranzas_banco_client import CobranzasBancoError, get_cobranzas_banco_client


class Command(BaseCommand):
    help = "Últimas liquidaciones de pagos web (solo lectura), opcionalmente con su transacción en api-cobranzas."

    def add_arguments(self, parser):
        parser.add_argument("--cliente", help="nro_cliente")
        parser.add_argument("--alias", help="alias exacto del recibo")
        parser.add_argument("-n", type=int, default=5, help="cuántas (por defecto 5)")
        parser.add_argument("--detalle", action="store_true", help="lista los documentos enviados a pagar")
        parser.add_argument("--remoto", action="store_true", help="consulta la transacción en api-cobranzas (GET)")

    def handle(self, *args, **opciones):
        qs = SolicitudLiquidacion.objects.order_by("-recibido_en")
        if opciones["cliente"]:
            qs = qs.filter(nro_cliente=opciones["cliente"])
        if opciones["alias"]:
            qs = qs.filter(alias=opciones["alias"])
        solicitudes = list(qs[: opciones["n"]])
        if not solicitudes:
            self.stdout.write("Sin liquidaciones para ese filtro.")
            return

        cliente = get_cobranzas_banco_client() if opciones["remoto"] else None
        for s in solicitudes:
            self.stdout.write(
                f"#{s.id} {s.recibido_en:%Y-%m-%d %H:%M} UTC  {s.alias}  cliente {s.nro_cliente}  "
                f"Bs {s.monto}  banco={s.banco or '-'}  estado={s.estado}  intentos={s.intentos}  "
                f"pdf={'sí' if s.comprobante_pdf else 'no'}  uuid={s.cobranzas_uuid or '-'}"
            )
            if s.error:
                self.stdout.write(f"    error: {s.error[:500]}")
            if opciones["detalle"]:
                for d in s.detalle or []:
                    c = {k: str(d.get(k) or "") for k in ("tipo", "nro_comprobante", "fecha", "importe", "debito_credito", "detalle")}
                    self.stdout.write(
                        f"    {c['tipo']:>3} {c['nro_comprobante']:>9} {c['fecha']:>8} "
                        f"{c['importe']:>10} {c['debito_credito']:<7} {c['detalle']}"
                    )
            if cliente and s.cobranzas_uuid:
                try:
                    r = cliente._request_autenticado("get", f"/v1/transacciones/{s.cobranzas_uuid}")
                    t = r.json() if r.ok else {"error": r.text[:300]}
                except (CobranzasBancoError, ValueError) as exc:
                    t = {"error": str(exc)}
                if "error" in t:
                    self.stdout.write(f"    api-cobranzas: {t['error']}")
                else:
                    self.stdout.write(
                        f"    api-cobranzas: id {t.get('id')} {t.get('estado')}  lote {t.get('lote')}  "
                        f"caja {t.get('caja_codigo')}  pagado Bs {t.get('total_pagado')}  "
                        f"fecha_pago {t.get('fecha_pago')}  anulación {t.get('fecha_anulacion')}"
                    )
