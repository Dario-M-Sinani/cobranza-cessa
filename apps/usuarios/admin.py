from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Usuario


@admin.register(Usuario)
class UsuarioAdmin(UserAdmin):
    list_display = ("username", "email", "rol", "activo", "is_active", "is_staff")
    list_filter = ("rol", "activo", "is_staff", "is_superuser")
    fieldsets = UserAdmin.fieldsets + (
        ("CESSA", {"fields": ("rol", "activo")}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("CESSA", {"fields": ("rol", "activo")}),
    )
