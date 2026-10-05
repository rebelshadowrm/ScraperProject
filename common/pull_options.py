from dataclasses import dataclass
from datetime import date

from common.validation import is_future_event, parse_event_date


@dataclass(frozen=True)
class PullOptions:
    mode: str = "incremental"
    start_date: date | None = None
    end_date: date | None = None

    def __post_init__(self):
        if self.mode not in ("incremental", "refresh", "snapshot"):
            raise ValueError("Pull mode must be incremental, refresh, or snapshot")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("Season start must be on or before season end")

    def includes(self, payload):
        start = parse_event_date(payload.get("Start-date"))
        return bool(start and is_future_event(payload)
                    and (not self.start_date or start >= self.start_date)
                    and (not self.end_date or start <= self.end_date))
