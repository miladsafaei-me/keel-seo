from django.urls import include, path

from . import views

urlpatterns = [
    path("page/", views.page, name="page"),
    path("broken/", views.broken, name="broken"),
    path("search-console", include("keel_seo.gsc.urls")),
]
