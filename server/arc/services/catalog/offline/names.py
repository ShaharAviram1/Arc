"""The offline import's job type, importable without its handler.

Same reason :mod:`arc.services.catalog.names` exists: importing
:mod:`arc.services.catalog.offline.jobs` registers a handler as a side effect,
which the API must not do. The worker's scheduler entry and the CLI both want
the name and neither wants the registration.
"""

from __future__ import annotations

#: Weekly: download both public datasets and replace the offline tables.
IMPORT_OFFLINE = "import_offline_catalogue"

#: The two sources, as they are keyed in ``offline_imports``.
MANAMI = "manami"
FRIBB = "fribb"

#: Queue priority (lower runs first; the default is 100). Behind the catalogue
#: sweeps for the same reason they are behind everything else: this is a
#: 60 MB download and a 41k-row replace on a weekly timer, and nobody is
#: waiting on it. It holds one worker slot for a minute or two.
OFFLINE_PRIORITY = 250

__all__ = ["FRIBB", "IMPORT_OFFLINE", "MANAMI", "OFFLINE_PRIORITY"]
