"""Estado de una impresora térmica de red (ESC/POS, puerto 9100), como la ZKTeco ZKP8008.

Se le pregunta con DLE EOT (comando de estado en tiempo real de ESC/POS) y responde un byte por
pregunta, aunque esté sin papel o con la tapa abierta. Solo lee: nunca imprime nada.

    DLE EOT 1  estado de la impresora    bit 3 = fuera de línea
    DLE EOT 2  por qué está fuera de línea bit 2 = tapa abierta, bit 5 = se acabó el papel, bit 6 = error
    DLE EOT 4  sensor del rollo          bits 2-3 = queda poco papel, bits 5-6 = sin papel

En cada byte el bit 4 vale 1 y los bits 0 y 7 valen 0: así se distingue una impresora de
cualquier otro equipo que conteste en ese puerto.

Si una impresora no contesta DLE EOT (o lo hace distinto), queda como "respuesta desconocida":
hay que probarlo con la impresora real antes de confiar en el estado del papel.
"""
from __future__ import annotations

import socket

DLE_EOT = b"\x10\x04"

OK = "ok"
POCO_PAPEL = "poco_papel"
SIN_PAPEL = "sin_papel"
TAPA_ABIERTA = "tapa_abierta"
ERROR = "error"
NO_RESPONDE = "no_responde"
DESCONOCIDA = "respuesta_desconocida"

DESCRIPCION = {
    OK: "lista",
    POCO_PAPEL: "queda poco papel",
    SIN_PAPEL: "sin papel",
    TAPA_ABIERTA: "tapa abierta",
    ERROR: "en error (atasco o cortador)",
    NO_RESPONDE: "no responde (apagada o sin red)",
    DESCONOCIDA: "respuesta desconocida a la consulta de estado",
}


def _valido(byte: int) -> bool:
    return byte & 0x93 == 0x12


def _preguntar(conexion: socket.socket, n: int) -> int:
    conexion.sendall(DLE_EOT + bytes([n]))
    respuesta = conexion.recv(1)
    if not respuesta:
        raise ConnectionError("la impresora cerró la conexión")
    return respuesta[0]


def estado(direccion: str, timeout: float = 2.0) -> str:
    """Estado de la impresora en "host:puerto" (puerto 9100 si falta). Nunca lanza."""
    host, _, puerto = direccion.strip().rpartition(":")
    if not host:
        host, puerto = direccion.strip(), "9100"
    try:
        with socket.create_connection((host, int(puerto or 9100)), timeout=timeout) as conexion:
            conexion.settimeout(timeout)
            general = _preguntar(conexion, 1)
            causa = _preguntar(conexion, 2)
            papel = _preguntar(conexion, 4)
    except (OSError, ValueError):
        return NO_RESPONDE

    if not all(_valido(b) for b in (general, causa, papel)):
        return DESCONOCIDA
    if papel & 0x60 == 0x60 or causa & 0x20:
        return SIN_PAPEL
    if causa & 0x04:
        return TAPA_ABIERTA
    if causa & 0x40 or general & 0x08:
        return ERROR
    if papel & 0x0C == 0x0C:
        return POCO_PAPEL
    return OK
