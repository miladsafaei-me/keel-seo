from django.http import HttpResponseServerError
from django.shortcuts import render


def page(request):
    body = request.GET.get("body", "Stable body copy that should not change between renders.")
    return render(request, "hostapp/page.html", {"body": body})


def broken(request):
    return HttpResponseServerError("boom")
