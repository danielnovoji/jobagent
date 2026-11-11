import time
from collections import defaultdict
from urllib.parse import urlparse

import requests

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_DELAY = 1.5   # seconds between requests to same host
HOST_DELAYS = {"www.linkedin.com": 8.0}  # CLAUDE.md: LinkedIn at a low rate only
MAX_RETRIES = 3


class HttpClient:
    def __init__(self, delay: float = DEFAULT_DELAY):
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._delay = delay
        self._last: dict[str, float] = defaultdict(float)

    def get(self, url: str, **kwargs) -> requests.Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self._request("POST", url, **kwargs)

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        host = urlparse(url).netloc
        delay = HOST_DELAYS.get(host, self._delay)
        elapsed = time.monotonic() - self._last[host]
        if elapsed < delay:
            time.sleep(delay - elapsed)

        resp = None
        for attempt in range(MAX_RETRIES):
            resp = self._session.request(method, url, timeout=30, **kwargs)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "60"))
                time.sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            break

        self._last[host] = time.monotonic()
        resp.raise_for_status()
        return resp

    def close(self) -> None:
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
