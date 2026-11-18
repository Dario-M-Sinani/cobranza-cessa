from django import forms
from django.contrib import admin
from django.utils.html import format_html, format_html_join

from .models import DisenoKiosco, EnvioFactura, FranjaDiseno, Modulo, PagoKiosco, PantallaKiosco, VideoKiosco

COLORES = {
    Modulo.CAJERO: ("#004c98", "#fff"),
    Modulo.VIDEO: ("#111827", "#fff"),
    Modulo.CORTES: ("#ffe71e", "#00335f"),
    Modulo.PREGUNTAS: ("#dceefc", "#00335f"),
}


def vista_previa(diseno: dict | None, alto_px: int = 320) -> str:
    """Dibujo del diseño en proporción 9:16 (tótem vertical)."""
    if not diseno or not diseno.get("franjas"):
        franjas = [{"alto": 1, "celdas": [{"modulo": "cajero", "ancho": 100}]}]
    else:
        franjas = diseno["franjas"]
    total = sum(max(f["alto"], 1) for f in franjas) or 1
    filas = []
    for f in franjas:
        celdas = format_html_join(
            "", '<div style="flex:{} 1 0;background:{};color:{};display:flex;align-items:center;justify-content:center;'
                'text-align:center;font:600 11px sans-serif;border:1px solid #fff;padding:2px">{}</div>',
            ((c["ancho"], *COLORES.get(c["modulo"], ("#999", "#fff")), Modulo(c["modulo"]).label.split(" (")[0])
             for c in f["celdas"]),
        )
        filas.append(format_html('<div style="display:flex;height:{}%">{}</div>', round(max(f["alto"], 1) * 100 / total, 2), celdas))
    return format_html(
        '<div style="width:{}px;height:{}px;border:3px solid #333;border-radius:8px;overflow:hidden;background:#333">{}</div>',
        round(alto_px * 9 / 16), alto_px, format_html_join("", "{}", ((f,) for f in filas)),
    )


class FranjasFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        usados = []
        for form in self.forms:
            datos = getattr(form, "cleaned_data", None) or {}
            if not datos or datos.get("DELETE"):
                continue
            usados.append(datos["modulo"])
            if datos.get("modulo_2"):
                usados.append(datos["modulo_2"])
                if not 15 <= (datos.get("ancho") or 0) <= 85:
                    raise forms.ValidationError("Con dos cuadros en una franja, el ancho del primero va entre 15 y 85 %.")
            if not 5 <= (datos.get("alto") or 0) <= 100:
                raise forms.ValidationError("El alto de cada franja va entre 5 y 100.")
        if usados.count(Modulo.CAJERO) != 1:
            raise forms.ValidationError("El diseño tiene que tener el cajero exactamente una vez.")
        repetidos = {m for m in usados if usados.count(m) > 1}
        if repetidos:
            raise forms.ValidationError("Cada cuadro va una sola vez: " + ", ".join(Modulo(m).label for m in repetidos))


class FranjaInline(admin.TabularInline):
    model = FranjaDiseno
    formset = FranjasFormSet
    extra = 0
    fields = ("orden", "alto", "modulo", "modulo_2", "ancho")


@admin.register(DisenoKiosco)
class DisenoKioscoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "previa_chica", "pantallas_que_lo_usan", "modificado_en")
    readonly_fields = ("previa",)
    fields = ("nombre", "previa")
    inlines = [FranjaInline]

    @admin.display(description="Vista previa (tótem vertical)")
    def previa(self, obj):
        if not obj.pk:
            return "Se ve al guardar."
        return format_html("{}<p style='margin-top:8px;color:#666'>Se aplica en las pantallas al minuto, "
                           "cuando vuelven al inicio (nunca en medio de un pago).</p>", vista_previa(obj.para_pantalla()))

    @admin.display(description="Vista")
    def previa_chica(self, obj):
        return vista_previa(obj.para_pantalla(), 120)

    @admin.display(description="Pantallas")
    def pantallas_que_lo_usan(self, obj):
        return ", ".join(obj.pantallas.values_list("nombre", flat=True)) or "—"


@admin.register(VideoKiosco)
class VideoKioscoAdmin(admin.ModelAdmin):
    list_display = ("titulo", "orden", "activo", "con_sonido", "subido_en")
    list_editable = ("orden", "activo", "con_sonido")


@admin.register(PantallaKiosco)
class PantallaKioscoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "diseno", "ultimo_contacto", "revisada_en", "problemas_avisados")
    list_editable = ("diseno",)
    # Lo único que se elige a mano es el diseño; el resto lo llena la vigilancia.
    readonly_fields = [f.name for f in PantallaKiosco._meta.fields if f.name != "diseno"] + ["previa"]

    @admin.display(description="Vista previa")
    def previa(self, obj):
        return vista_previa(obj.diseno.para_pantalla() if obj.diseno else None)

    def has_add_permission(self, request):
        return False


class EnvioFacturaInline(admin.TabularInline):
    model = EnvioFactura
    extra = 0
    readonly_fields = ("canal", "destino", "registrado", "exitoso", "error", "enviado_en")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(PagoKiosco)
class PagoKioscoAdmin(admin.ModelAdmin):
    list_display = ("alias", "kiosco", "nro_cliente", "monto", "estado", "creado_en", "pagado_en", "nro_factura")
    list_filter = ("estado", "kiosco", "creado_en")
    search_fields = ("alias", "nro_cliente")
    # Lo crea y lo mueve solo apps.kiosco.services: de solo lectura, como SolicitudLiquidacion.
    readonly_fields = [f.name for f in PagoKiosco._meta.fields]
    inlines = [EnvioFacturaInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
