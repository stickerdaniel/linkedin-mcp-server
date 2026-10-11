"""The daemon's decisions, kept apart from everything that carries them out.

Each module here is a reducer, ``on_event(state, event) -> (state, effects)``:
time and identifiers arrive as event values, every effect names work for a shell
outside this package, and every completion event names the operation it answers.
Nothing here awaits, reads a clock, opens a file or a socket, or imports the
adapter modules beside this package. ``ruff.toml`` in this directory bans the
obvious ways to break that; it guards against accident and proves nothing.
"""
