"""Codex Token Bola local token analytics service."""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    __version__ = version("codex-token-bola")
except PackageNotFoundError:
    __version__ = "0+unknown"
