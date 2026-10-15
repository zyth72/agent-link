"""Entry point for ``python3 -m agent_link``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
