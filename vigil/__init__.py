"""Vigil: verification layer for scheduled agents."""

__version__ = "0.1.0"

from vigil.config import Job, load_config
from vigil.supervisor import Supervisor

__all__ = ["Job", "load_config", "Supervisor", "__version__"]
