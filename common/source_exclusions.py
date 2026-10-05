class SourceEventIncomplete(Exception):
    """The source explicitly withholds required information; retry on rediscovery."""
