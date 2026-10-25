class SourceEventIncomplete(Exception):
    """The source explicitly withholds required information; retry on rediscovery."""


class SourceEventOutOfScope(SourceEventIncomplete):
    """Published source evidence identifies a non-target or cancelled event."""
