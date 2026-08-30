# Status: production
# Path: imported by — watchdog submodules (checker, messenger, fixloop, loop)
"""Watchdog runtime state — module-level mutable singletons.

Avoids circular imports: submodules import _state/_test_active from here
instead of from __init__.py or each other.
"""

import time

from lib.watchdog.state import WatchdogState

_state = WatchdogState()
_running: bool = True
_start_time: float = time.monotonic()
_test_active: bool = False
_code_scan_counter: int = 0
CODE_SCAN_INTERVAL: int = 10
