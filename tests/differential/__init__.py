"""Differential harness: the same scenario measured in Direct and daemon mode."""

import time

#: The calendar clock's offset from the monotonic clock, taken when the harness
#: package is first imported: before any row actor exists. The watcher compares
#: every sample's offset with this one and trusts create-time order only while
#: they agree (``watcher.clock_offset_now``).
HARNESS_CLOCK_OFFSET = time.time() - time.monotonic()
