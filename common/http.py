"""Reusable transport for providers whose HTTP operations are GET-only."""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class BoundedRetry(Retry):
    def sleep_for_retry(self, response):
        delay = self.get_retry_after(response)
        if delay and delay > 30:
            # Fail visibly rather than wait indefinitely or retry earlier than requested.
            raise requests.exceptions.RetryError("Provider Retry-After exceeds 30-second retry budget")
        return super().sleep_for_retry(response)


def create_get_session():
    session = requests.Session()
    retry = BoundedRetry(total=2, connect=2, read=0, status=2, other=0,
                         backoff_factor=0.5, backoff_max=5,
                         status_forcelist=(429, 500, 502, 503, 504),
                         allowed_methods=frozenset({"GET"}), raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
