"""Built-in game detectors.

Each module exposes one `GameSessionDetector`. They are registered by
`app.core.detection.loader.build_host()`, which also loads user plugins.

Phase 8 coverage (see docs/PHASE8.md for the full matrix):

- `league_of_legends` — the fail-safe reference detector (Phase 6).
- `valorant`          — one process for menu AND match; waits while it runs.
- `repo`              — no public session signal; waits while the game runs.
- `teamfight_tactics` — shares the League match process; waits unless the live
  API proves a TFT match (and even then: WAIT — it can only never kill).
"""

from app.core.detection.games.league_of_legends import LeagueOfLegendsDetector
from app.core.detection.games.repo import RepoDetector
from app.core.detection.games.teamfight_tactics import TftDetector
from app.core.detection.games.valorant import ValorantDetector

__all__ = [
    "LeagueOfLegendsDetector",
    "RepoDetector",
    "TftDetector",
    "ValorantDetector",
]
