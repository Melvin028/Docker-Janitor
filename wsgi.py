"""WSGI entrypoint for Vercel (and other WSGI hosts)."""

from janitor.web import create_app

app = create_app()
