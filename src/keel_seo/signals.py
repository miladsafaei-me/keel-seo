"""Invalidate the per-path Landing cache whenever a row changes."""
from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Landing


@receiver([post_save, post_delete], sender=Landing)
def _invalidate_landing_cache(sender, instance, **kwargs):
    cache.delete(f"landing:{instance.url}")
