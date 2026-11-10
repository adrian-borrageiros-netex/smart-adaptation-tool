"""Cliente de las APIs de smartED: SmartBackOffice y CMS (classroom)."""

from .autenticacion import ErrorAutenticacion, ProveedorDeTokens

__all__ = ["ProveedorDeTokens", "ErrorAutenticacion"]
