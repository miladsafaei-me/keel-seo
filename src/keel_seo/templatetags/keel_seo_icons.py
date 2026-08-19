"""Inline-SVG icon tag set for the GSC dashboard template.

Re-exported from keel-web (``keel_web.auth.templatetags.keel_icons``) rather than
carrying keel-seo's own copy — every current Keel consumer already has keel-web
installed (it is the platform's auth foundation), so this gives the dashboard
template's ``{% icon %}`` calls the same icon set for free. ``register`` is
keel-web's ``template.Library`` with the ``icon`` tag already attached, so loading
this library (``{% load keel_seo_icons %}``) exposes that tag under a load-name that
won't collide with a host's own ``{% load icons %}`` re-export.

A host without keel-web installed gets an empty tag library instead of an
ImportError at Django startup — ``{% icon %}`` calls in the shipped template then
fail at template-render time with "invalid tag", the same failure mode as any other
missing template dependency, until keel-web is installed or the template is
overridden (Django's normal per-app template-loader precedence lets a host place its
own ``keel_seo/gsc/search_console.html`` ahead of this one).
"""
try:
    from keel_web.auth.templatetags.keel_icons import register  # noqa: F401
except ImportError:
    from django import template

    register = template.Library()
