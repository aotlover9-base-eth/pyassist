"""Enables ``python -m pyassist``.

The ``pyassist`` console script is the normal entry point; this makes the
package runnable straight from a source checkout without installing it.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
