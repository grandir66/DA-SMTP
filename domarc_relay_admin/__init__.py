"""Domarc SMTP Relay — Admin Web (standalone).

Flask app factory + CLI per gestire il SMTP relay Domarc come prodotto autonomo.

Esporta:
- `create_app(config=None) -> Flask` — app factory standard Flask
- `__version__`
"""
__version__ = "0.4.0"

from .app import create_app

__all__ = ["create_app", "__version__"]
