"""Per-ATS adapters. Each adapter exports:
    detect(url) -> bool       — is this URL handled by this adapter
    apply(url, **kwargs) -> dict — run the full submission flow
"""
from . import greenhouse, lever, ashby, workday

ALL_ADAPTERS = [greenhouse, lever, ashby, workday]


def route(url: str):
    """Return the adapter module that should handle this URL, or None."""
    for a in ALL_ADAPTERS:
        if a.detect(url):
            return a
    return None
