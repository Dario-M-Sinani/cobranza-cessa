from django.contrib import admin

from .models import LogAuditoria


@admin.register(LogAuditoria)
class LogAuditoriaAdmin(admin.ModelAdmin):
    list_display = ("creado_en", "usuario", "accion", "entidad_afectada")
    list_filter = ("creado_en",)
    search_fields = ("accion", "entidad_afectada", "usuario__username")
    readonly_fields = ("usuario", "accion", "entidad_afectada", "creado_en")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
