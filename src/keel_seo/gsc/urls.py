"""URL routes for the keel_seo.gsc dashboard.

keel-seo's first UI surface — a whole app_name namespace, meant to be mounted at
whatever path a host wants the dashboard to live at::

    # host urls.py
    from django.urls import include, path

    urlpatterns = [
        path("admin-os/search-console", include("keel_seo.gsc.urls")),
        ...
    ]

Every route below starts with a leading ``/`` (except the root) so it concatenates
correctly onto a mount prefix that itself carries no trailing slash — the mount
point above reproduces SignalBots' pre-migration path exactly:
``/admin-os/search-console``, ``/admin-os/search-console/dismiss``, etc.
"""
from django.urls import path
from django.views.generic import RedirectView

from . import views

app_name = "keel_seo_gsc"

urlpatterns = [
    path("", views.SearchConsoleView.as_view(), name="search_console"),
    path("/dismiss", views.dismiss, name="dismiss"),
    path("/restore", views.restore, name="restore"),
    path("/queue", views.queue, name="queue"),
    path("/dedicated/exclude", views.dedicated_exclude, name="dedicated_exclude"),
    path("/dedicated/queue", views.dedicated_queue, name="dedicated_queue"),
    path("/dedicated/cluster-exclude", views.cluster_exclude, name="cluster_exclude"),
    path("/dedicated/cluster-queue", views.cluster_queue, name="cluster_queue"),
    # Trailing-slash form 404s under APPEND_SLASH (the route is deliberately
    # slash-less); redirect it so a typed slash still lands on the dashboard.
    path("/", RedirectView.as_view(pattern_name="keel_seo_gsc:search_console", permanent=False)),
]
