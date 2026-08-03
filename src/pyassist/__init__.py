"""pyassist: a beautiful terminal AI assistant.

Public surface is intentionally small; everything interesting lives in the
subpackages:

* :mod:`pyassist.core`    - storage, LLM client, agent engine, memory
* :mod:`pyassist.tools`   - tool protocol, registry and built-in tools
* :mod:`pyassist.plugins` - plugin API and loader
* :mod:`pyassist.ui`      - the Textual application
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
