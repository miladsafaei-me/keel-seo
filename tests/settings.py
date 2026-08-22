"""Minimal Django settings to exercise keel-seo standalone.

Run with:

    DJANGO_SETTINGS_MODULE=tests.settings python -m django test tests

from the repo root, with keel-seo installed (editable is fine) into whatever
interpreter runs the command.
"""
SECRET_KEY = "keel-seo-test-suite"
DEBUG = True
ALLOWED_HOSTS = ["testserver"]
USE_TZ = True
SITE_ID = 1

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sites",
    "django.contrib.sitemaps",
    "keel_seo",
    "tests.hostapp",
]

ROOT_URLCONF = "tests.hostapp.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "keel_seo.context_processors.landing",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
}

KEEL_SEO = {
    "freshness_enabled": True,
}
