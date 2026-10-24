"""The one shape every tracker's bodyweight reader returns (TASK-343).

Liftosaur, Wger, and Hevy each expose a bodyweight figure through a
completely different endpoint, envelope, and unit convention. Each app's
service module owns that translation; this module owns only the result type
they all agree on, so ``accounts.services.sync_bodyweight_from_trackers`` can
compare readings across trackers without knowing which one produced any of
them.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class TrackerBodyweight:
    """One bodyweight reading pulled from a connected tracker.

    ``weight_kg`` is always kilograms -- every reader converts before
    returning, so no downstream caller has to carry a unit around.
    ``measured_at`` is when the LIFTER recorded it in that tracker (not when
    we fetched it), which is what makes readings from two different trackers
    comparable, and what lets a sync leave a hand-entered figure alone when
    the tracker's own measurement is older than it.
    """

    weight_kg: Decimal
    measured_at: datetime
