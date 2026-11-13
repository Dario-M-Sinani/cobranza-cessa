import pytest


@pytest.fixture(autouse=True)
def _cache_en_memoria(settings):
    """Los tests no dependen de que haya un Redis levantado (la caché por defecto es Redis)."""
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    from django.core.cache import cache

    cache.clear()
