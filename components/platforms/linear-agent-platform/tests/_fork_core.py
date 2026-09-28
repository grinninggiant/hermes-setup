"""Skip gate for tests of retired fork-core-only behaviour.

The plugin itself disables owner-bound clarify on upstream core
(``_core_binds_clarify_owner``); these suites exercise that disabled path.
"""

import inspect
import unittest


def require_clarify_owner_binding() -> None:
    try:
        from tools import clarify_gateway

        bound = "turn_owner" in inspect.signature(clarify_gateway.register).parameters
    except (ImportError, AttributeError, TypeError, ValueError):
        bound = False
    if not bound:
        raise unittest.SkipTest("fork-core clarify owner binding; upstream core has none")
