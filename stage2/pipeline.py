"""Stage 2 Pipeline re-export for backwards compatibility and clean package structure."""
from service.pipeline import Stage2Pipeline, classify, changed_lines

__all__ = ["Stage2Pipeline", "classify", "changed_lines"]
