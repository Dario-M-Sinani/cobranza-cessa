from django.contrib import admin

from .models import Cliente


@admin.register(Cliente)
class ClienteAdmin(admin.ModelAdmin):
    list_display = ("codigo_externo", "nombre", "nit_ci")
    search_fields = ("codigo_externo", "nombre", "nit_ci")
