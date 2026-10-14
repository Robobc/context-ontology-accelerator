"""Public API for the outer-agent composition runtime."""

from .evidence import Evidence
from .identity import TrustedIdentity
from .orchestrator import OuterAgent
from .plan_models import SourceDirective
from .source_adapters import SourceAdapter, SourceAdapterRegistry, SourceTargetConfig

__all__ = [
    "Evidence",
    "OuterAgent",
    "SourceAdapter",
    "SourceAdapterRegistry",
    "SourceDirective",
    "SourceTargetConfig",
    "TrustedIdentity",
]
