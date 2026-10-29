#!/usr/bin/env python3
"""Start the Transitory dashboard.  ``--demo`` uses made-up data (no downloads)."""

import sys

from transitory.gui.main_window import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
