import os

from django import forms
from django.conf import settings
from django.contrib import admin
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join

from . import disenos
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
        porcentaje = round(max(f["alto"], 1) * 100 / total)
        celdas = format_html_join(
            "", '<div style="flex:{} 1 0;background:{};color:{};display:flex;flex-direction:column;align-items:center;'
                'justify-content:center;text-align:center;font:600 11px sans-serif;border:1px solid #fff;padding:2px;'
                'overflow:hidden">{}<small style="font-weight:400;opacity:.8">{} %</small></div>',
            ((c["ancho"], *COLORES.get(c["modulo"], ("#999", "#fff")), Modulo(c["modulo"]).label.split(" (")[0], porcentaje)
             for c in f["celdas"]),
        )
        filas.append(format_html('<div style="display:flex;height:{}%">{}</div>', porcentaje, celdas))
    return format_html(
        '<div style="width:{}px;height:{}px;border:3px solid #333;border-radius:8px;overflow:hidden;background:#333">{}</div>',
        round(alto_px * 9 / 16), alto_px, format_html_join("", "{}", ((f,) for f in filas)),
    )


class FranjasFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        franjas = []
        for form in self.forms:
            datos = getattr(form, "cleaned_data", None) or {}
            if not datos or datos.get("DELETE"):
                continue
            franjas.append({"modulo": datos.get("modulo"), "alto": datos.get("alto") or 0,
                            "modulo_2": datos.get("modulo_2") or "", "ancho": datos.get("ancho") or 0})
        problemas = disenos.errores(franjas)
        if problemas:
            raise forms.ValidationError(problemas)


class FranjaForm(forms.ModelForm):
    class Meta:
        model = FranjaDiseno
        fields = ("orden", "modulo", "alto", "modulo_2", "ancho")
        labels = {
            "orden": "Posición",
            "modulo": "Qué muestra",
            "alto": "Alto (%)",
            "modulo_2": "Al lado, otro cuadro (opcional)",
            "ancho": "Ancho del primero (%)",
        }
        help_texts = {
            "orden": "1 = arriba del todo",
            "modulo": "",
            "alto": "Parte de la pantalla que ocupa",
            "modulo_2": "Parte la franja en dos columnas",
            "ancho": "Solo si hay otro cuadro al lado (el otro usa el resto)",
        }
        widgets = {
            "orden": forms.NumberInput(attrs={"style": "width:4em"}),
            "alto": forms.NumberInput(attrs={"style": "width:5em"}),
            "ancho": forms.NumberInput(attrs={"style": "width:5em"}),
        }


class FranjaInline(admin.TabularInline):
    model = FranjaDiseno
    form = FranjaForm
    formset = FranjasFormSet
    extra = 0
    verbose_name = "franja"
    verbose_name_plural = "Franjas de la pantalla (de arriba a abajo)"


def boton_previa(diseno) -> str:
    url = reverse("admin:kiosco_disenokiosco_previa", args=[diseno.pk])
    return format_html(
        '<a href="{}" target="kiosco-previa" class="button" style="padding:8px 14px;font-weight:700" '
        'onclick="window.open(this.href, \'kiosco-previa\', \'width=620,height=1000\'); return false;">'
        "▶ Ver cómo se verá</a>", url,
    )


@admin.register(DisenoKiosco)
class DisenoKioscoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "previa_chica", "pantallas_que_lo_usan", "ver", "modificado_en")
    readonly_fields = ("previa", "pantallas_que_lo_usan")
    fieldsets = [
        (None, {
            "fields": ("nombre", "previa", "pantallas_que_lo_usan"),
            "description": (
                "Un diseño dice cómo se reparte la pantalla del tótem: franjas de arriba a abajo. En cada franja "
                "se elige qué se muestra (el cajero, los videos, los cortes de energía o las preguntas "
                "frecuentes) y qué alto tiene. Una franja se puede partir en dos cuadros, uno al lado del otro. "
                "El cajero va siempre, una sola vez. Después de guardar, elige el diseño en "
                "<b>Kiosco › Pantalla kioscos</b> para cada tótem."
            ),
        }),
    ]
    inlines = [FranjaInline]

    def get_urls(self):
        propias = [path("<int:pk>/previa/", self.admin_site.admin_view(self.ventana_previa), name="kiosco_disenokiosco_previa")]
        return propias + super().get_urls()

    def ventana_previa(self, request, pk):
        diseno = get_object_or_404(DisenoKiosco, pk=pk)
        return render(request, "admin/kiosco/previa_diseno.html", {
            "diseno": diseno, "ancho": 1080, "alto": 1920,
            "url_pantalla": reverse("kiosco-pantalla") + f"?previa={diseno.pk}&impresion=ninguna",
        })

    @admin.display(description="Vista previa")
    def previa(self, obj):
        if not obj.pk:
            return "Se ve después de guardar."
        return format_html(
            '<div style="display:flex;gap:24px;align-items:flex-start;flex-wrap:wrap">{}<div style="max-width:340px">'
            "<p style='margin-top:0'>{}</p><p style='color:#666'>Los cambios llegan a los tótems en un minuto, cuando "
            "vuelven a la pantalla de inicio (nunca en medio de un pago). Los cambios sin guardar no se ven.</p></div></div>",
            vista_previa(obj.para_pantalla()), boton_previa(obj),
        )

    @admin.display(description="Vista")
    def previa_chica(self, obj):
        return vista_previa(obj.para_pantalla(), 120)

    @admin.display(description="")
    def ver(self, obj):
        return boton_previa(obj)

    @admin.display(description="Lo usan los tótems")
    def pantallas_que_lo_usan(self, obj):
        if not obj or not obj.pk:
            return "—"
        return ", ".join(obj.pantallas.values_list("nombre", flat=True)) or "Ninguno todavía"


class VideoForm(forms.ModelForm):
    class Meta:
        model = VideoKiosco
        fields = ("titulo", "archivo", "orden", "activo", "con_sonido")
        labels = {"titulo": "Nombre del spot", "archivo": "Archivo de video", "orden": "Orden en que pasa",
                  "activo": "Se muestra en los tótems", "con_sonido": "Con sonido"}
        help_texts = {"orden": "1 = el primero. Pasan uno tras otro y vuelven a empezar.",
                      "archivo": "MP4 (H.264) o WebM, hasta 300 MB. Mejor horizontal 16:9.",
                      "con_sonido": "Si no se marca, el video pasa en silencio."}
        # Sin el enlace "Actualmente: ..." (los videos no tienen URL pública; se ven abajo en "Ver").
        widgets = {"archivo": forms.FileInput(attrs={"accept": "video/mp4,video/webm"})}


@admin.register(VideoKiosco)
class VideoKioscoAdmin(admin.ModelAdmin):
    form = VideoForm
    list_display = ("titulo", "orden", "activo", "con_sonido", "peso", "subido_en")
    list_editable = ("orden", "activo", "con_sonido")
    readonly_fields = ("donde_se_guarda", "ver_video")
    fieldsets = [
        (None, {
            "fields": ("titulo", "archivo", "orden", "activo", "con_sonido"),
            "description": (
                "Los spots del cuadro de videos. Se suben aquí y se guardan en el servidor del gateway; los tótems "
                "los bajan una vez por la red interna y los guardan, así no dependen de internet."
            ),
        }),
        ("Archivo", {"fields": ("donde_se_guarda", "ver_video")}),
    ]

    @admin.display(description="Peso")
    def peso(self, obj):
        try:
            return f"{obj.archivo.size / 1048576:.1f} MB"
        except (OSError, ValueError):
            return "—"

    @admin.display(description="Dónde se guarda")
    def donde_se_guarda(self, obj):
        carpeta = os.path.join(settings.MEDIA_ROOT, "kiosco", "videos")
        if obj and obj.pk and obj.archivo:
            return format_html("<code>{}</code> ({})", os.path.join(settings.MEDIA_ROOT, obj.archivo.name), self.peso(obj))
        return format_html("Al guardar, en <code>{}</code> del servidor.", carpeta)

    @admin.display(description="Ver")
    def ver_video(self, obj):
        if not obj or not obj.pk or not obj.activo:
            return "Se puede ver cuando está guardado y activo."
        return format_html('<video src="{}" controls muted style="max-width:480px;width:100%;background:#000"></video>',
                           reverse("kiosco-video", args=[obj.pk]))


@admin.register(PantallaKiosco)
class PantallaKioscoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "diseno", "ultimo_contacto", "revisada_en", "problemas_avisados")
    list_editable = ("diseno",)
    # Lo único que se elige a mano es el diseño; el resto lo llena la vigilancia.
    readonly_fields = [f.name for f in PantallaKiosco._meta.fields if f.name != "diseno"] + ["previa"]

    @admin.display(description="Vista previa")
    def previa(self, obj):
        if not obj.diseno:
            return format_html("{}<p>Sin diseño: el cajero ocupa toda la pantalla.</p>", vista_previa(None))
        return format_html("{}<p>{}</p>", vista_previa(obj.diseno.para_pantalla()), boton_previa(obj.diseno))

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
