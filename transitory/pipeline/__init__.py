"""The analysis pipeline: fetch -> clean -> search -> fit -> vet.  Pure Python, no Qt.

``pipeline.run.analyze_star`` is the one entry point the GUI and validation use.
"""


class AnalysisCancelled(Exception):
    """Raised (typically by a progress callback) to stop an analysis in progress."""
