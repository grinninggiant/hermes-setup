"""Skip gate for tests of retired fork-core-only behaviour.

The old fork core bound clarify owners, owned the interrupt seam, staged delivery and
honoured on_processing_start vetoes; upstream core has none of these. The plugin
already degrades on upstream (e.g. ``_core_binds_clarify_owner``).
"""

import inspect
import unittest


def _fork_core() -> bool:
    try:
        from tools import clarify_gateway

        return "turn_owner" in inspect.signature(clarify_gateway.register).parameters
    except (ImportError, AttributeError, TypeError, ValueError):
        return False


FORK_CORE = _fork_core()
fork_core_only = unittest.skipUnless(FORK_CORE, "fork-core-only behaviour; upstream core lacks it")


def require_clarify_owner_binding() -> None:
    if not FORK_CORE:
        raise unittest.SkipTest("fork-core clarify owner binding; upstream core has none")
