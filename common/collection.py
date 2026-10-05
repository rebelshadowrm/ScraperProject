class CollectedLinks(list):
    """List-compatible discovery results that expose incomplete source coverage."""

    def __init__(self, links=(), *, complete=True, error="", pages=0, non_future_skipped=0):
        super().__init__(links)
        self.complete = complete
        self.error = error
        self.pages = pages
        self.non_future_skipped = non_future_skipped


def validated_records(payload, *, field=None, source="Provider"):
    """A missing/null/error payload is not a successfully empty record list."""
    if field is not None:
        if not isinstance(payload, dict) or field not in payload:
            raise ValueError(f"{source} response has no {field} list")
        payload = payload[field]
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise ValueError(f"{source} response must contain a list of record objects")
    return payload
