"""Impresora térmica de red simulada, para probar el estado del kiosco sin la impresora real.

    python manage.py simular_impresora --estado sin_papel --puerto 9100

Contesta DLE EOT 1/2/4 como una impresora ESC/POS en el estado pedido y descarta (contando los
bytes) cualquier otra cosa que le manden. Para probar "apagada", basta con no correrla.
Después: KIOSCO_IMPRESORAS=<pantalla>=127.0.0.1:9100 en el .env.
"""
import socketserver

from django.core.management.base import BaseCommand

# Bytes de respuesta (DLE EOT 1, 2, 4) por estado. Bit 4 siempre en 1, bits 0 y 7 en 0.
RESPUESTAS = {
    "ok": {1: 0x12, 2: 0x12, 4: 0x12},
    "poco_papel": {1: 0x12, 2: 0x12, 4: 0x1E},
    "sin_papel": {1: 0x1A, 2: 0x32, 4: 0x72},
    "tapa_abierta": {1: 0x1A, 2: 0x16, 4: 0x12},
    "error": {1: 0x1A, 2: 0x52, 4: 0x12},
}


class Command(BaseCommand):
    help = "Simula una impresora térmica ESC/POS de red que responde consultas de estado."

    def add_arguments(self, parser):
        parser.add_argument("--estado", choices=sorted(RESPUESTAS), default="ok")
        parser.add_argument("--puerto", type=int, default=9100)
        parser.add_argument("--host", default="0.0.0.0")

    def handle(self, *args, estado, puerto, host, **options):
        respuestas = RESPUESTAS[estado]
        escribir = self.stdout.write

        class Atender(socketserver.BaseRequestHandler):
            def handle(self):
                datos = b""
                otros = 0
                while True:
                    trozo = self.request.recv(1024)
                    if not trozo:
                        break
                    datos += trozo
                    while datos:
                        if datos.startswith(b"\x10\x04") and len(datos) >= 3:
                            self.request.sendall(bytes([respuestas.get(datos[2], 0x12)]))
                            datos = datos[3:]
                        elif datos.startswith(b"\x10") and len(datos) < 3:
                            break  # falta el resto del comando
                        else:
                            otros += 1
                            datos = datos[1:]
                if otros:
                    escribir(f"recibidos {otros} bytes para imprimir (descartados)")

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        with socketserver.ThreadingTCPServer((host, puerto), Atender) as servidor:
            escribir(self.style.SUCCESS(f"Impresora simulada en {host}:{puerto}, estado {estado}. Ctrl+C para cortar."))
            try:
                servidor.serve_forever()
            except KeyboardInterrupt:
                pass
