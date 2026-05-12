"""InvenTree label plugin that emits a 2-color flat-tile 3MF.

Inventree3MFQRLabel is exposed via a lazy import so the package can be
imported for unit testing mesh.py without Django being installed.
"""

from .version import PLUGIN_VERSION

__all__ = ["Inventree3MFQRLabel", "PLUGIN_VERSION"]


def __getattr__(name):
    if name == "Inventree3MFQRLabel":
        from .plugin import Inventree3MFQRLabel as _cls
        return _cls
    raise AttributeError(name)
