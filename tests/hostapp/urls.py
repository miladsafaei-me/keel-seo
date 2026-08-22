from django.urls import path

from . import views

urlpatterns = [
    path("page/", views.page, name="page"),
    path("broken/", views.broken, name="broken"),
]
