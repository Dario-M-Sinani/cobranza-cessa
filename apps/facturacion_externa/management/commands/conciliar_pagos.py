"""Concilia (solo lectura) lo registrado en api-cobranzas-bancos con el usuario de banco del
gateway contra los pagos web y los cobros del panel. Ver apps/facturacion_externa/conciliacion.py.

    python manage.py conciliar_pagos                      # ayer
    python manage.py conciliar_pagos --desde 2026-10-01 --hasta 2026-10-08
    python manage.py conciliar_pagos --alertar            # además manda el reporte si hay diferencias

La tarea programada (07:30) concilia el día anterior y avisa solo si hay diferencias.
"""
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from services.alertas import notificar

from ...conciliacion import conciliar


class Command(BaseCommand):
    help = "Concilia la .102 contra los pagos web y los cobros del panel (solo lectura)."

    def add_arguments(self, parser):
        parser.add_argument("--desde", type=date.fromisoformat)
        parser.add_argument("--hasta", type=date.fromisoformat)
        parser.add_argument("--alertar", action="store_true")

    def handle(self, *args, **opciones):
        desde = opciones["desde"] or timezone.localdate() - timedelta(days=1)
        hasta = opciones["hasta"] or desde
        if hasta < desde:
            raise CommandError("--hasta no puede ser anterior a --desde.")
        reporte = conciliar(desde, hasta)
        self.stdout.write(reporte.texto())
        if opciones["alertar"] and reporte.diferencias:
            notificar(f"Conciliación de pagos: {len(reporte.diferencias)} diferencias", reporte.texto())
