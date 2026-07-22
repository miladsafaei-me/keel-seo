from django.apps import AppConfig


class KeelSeoConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "keel_seo"
    verbose_name = "Keel SEO — Landing Registry"

    def ready(self):
        from . import signals  # noqa: F401  (wire the cache-invalidation receivers)
