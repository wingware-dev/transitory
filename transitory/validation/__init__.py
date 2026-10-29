"""Known-planet validation: run the pipeline on stars with published answers and report.

    python -m transitory.validation                  # the live set, all data, report in reports/
    python -m transitory.validation --publish        # ...and copy it to docs/validation/
    python -m transitory.validation --demo           # the synthetic demo stars, no downloads
"""

from .report import publish, write_report
from .targets import ValidationTarget, validation_targets
from .validate import ValidationRun, archive_provider, run_validation, synthetic_provider, synthetic_targets

__all__ = [
    "ValidationRun", "ValidationTarget", "archive_provider", "publish", "run_validation",
    "synthetic_provider", "synthetic_targets", "validation_targets", "write_report",
]
