#!/usr/bin/env python3
"""Start the Transitory dashboard.

    python run_dashboard.py                              # TESS / Kepler data from MAST
    python run_dashboard.py --demo                       # made-up data, no downloads
    python run_dashboard.py --targets "Kepler-10, TOI-270" --max-sectors 10
"""

import sys

from transitory.gui.main_window import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
