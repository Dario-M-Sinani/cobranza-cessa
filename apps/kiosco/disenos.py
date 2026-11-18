"""Reglas de un diseño de pantalla (las usan el admin y el editor de /kiosco/editor/)."""
from __future__ import annotations

from django.db import transaction

from .models import DisenoKiosco, FranjaDiseno, Modulo

MODULOS = [m.value for m in Modulo]


def _entero(valor, por_defecto: int) -> int:
    try:
        return int(round(float(valor)))
    except (TypeError, ValueError):
        return por_defecto


def limpiar(franjas) -> list[dict]:
    """Lo que manda el editor -> [{modulo, alto, modulo_2, ancho}] con números enteros."""
    limpias = []
    for f in franjas if isinstance(franjas, list) else []:
        if not isinstance(f, dict):
            continue
        modulo_2 = str(f.get("modulo_2") or "")
        limpias.append({
            "modulo": str(f.get("modulo") or ""),
            "alto": _entero(f.get("alto"), 20),
            "modulo_2": modulo_2,
            "ancho": _entero(f.get("ancho"), 50) if modulo_2 else 100,
        })
    return limpias


def errores(franjas: list[dict]) -> list[str]:
    """Vacío si el diseño sirve: el cajero una sola vez, ningún cuadro repetido, altos y anchos
    razonables."""
    problemas = []
    if not franjas:
        return ["El diseño necesita al menos una franja (la del cajero)."]
    usados = []
    for i, f in enumerate(franjas, start=1):
        for clave in ("modulo", "modulo_2"):
            if f.get(clave) and f[clave] not in MODULOS:
                problemas.append(f"Franja {i}: cuadro desconocido «{f[clave]}».")
        usados.append(f.get("modulo"))
        if f.get("modulo_2"):
            usados.append(f["modulo_2"])
            if not 15 <= f.get("ancho", 0) <= 85:
                problemas.append("Con dos cuadros en una franja, el ancho del primero va entre 15 y 85 %.")
        if not 5 <= f.get("alto", 0) <= 100:
            problemas.append("El alto de cada franja va entre 5 y 100.")
    if usados.count(Modulo.CAJERO) != 1:
        problemas.append("El diseño tiene que tener el cajero exactamente una vez.")
    repetidos = sorted({m for m in usados if m and usados.count(m) > 1 and m != Modulo.CAJERO})
    if repetidos:
        problemas.append("Cada cuadro va una sola vez: " + ", ".join(Modulo(m).label for m in repetidos))
    return list(dict.fromkeys(problemas))


def proporciones(franjas: list[dict]) -> list[dict]:
    """Altos llevados a % que suman 100 (el editor arrastra con decimales)."""
    total = sum(f["alto"] for f in franjas) or 1
    salida = [dict(f, alto=max(5, round(f["alto"] * 100 / total))) for f in franjas]
    diferencia = 100 - sum(f["alto"] for f in salida)
    if salida and diferencia:
        mayor = max(range(len(salida)), key=lambda i: salida[i]["alto"])
        salida[mayor]["alto"] += diferencia
    return salida


@transaction.atomic
def guardar(diseno: DisenoKiosco, franjas: list[dict]) -> DisenoKiosco:
    """Reemplaza las franjas del diseño (ya validadas)."""
    diseno.save()
    diseno.franjas.all().delete()
    FranjaDiseno.objects.bulk_create([
        FranjaDiseno(diseno=diseno, orden=i, alto=f["alto"], modulo=f["modulo"],
                     modulo_2=f.get("modulo_2") or "", ancho=f["ancho"] if f.get("modulo_2") else 100)
        for i, f in enumerate(franjas, start=1)
    ])
    return diseno
