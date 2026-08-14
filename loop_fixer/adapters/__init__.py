from __future__ import annotations

from .base import LanguageAdapter
from .java_maven import JavaMavenAdapter
from .python_pytest import PythonPytestAdapter

__all__ = ["LanguageAdapter", "PythonPytestAdapter", "JavaMavenAdapter"]

ADAPTERS: dict[str, type] = {
    "python": PythonPytestAdapter,
    "java": JavaMavenAdapter,
}
