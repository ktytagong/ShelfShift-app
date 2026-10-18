"""ShelfShift decision engine: pure Python, no I/O. The only place money is calculated."""
from .baseline import baseline_plan
from .canonical import canonical_json, from_dict, rec_version, to_dict
from .factchecks import fact_checks
from .limits import check_plan
from .model import ENGINE_VERSION, EngineInput, Plan
from .search import evaluate

__all__ = ["ENGINE_VERSION", "EngineInput", "Plan", "baseline_plan", "canonical_json", "check_plan", "evaluate",
           "fact_checks", "from_dict", "rec_version", "to_dict"]
