"""Polite HTTP for the housing scrapers.

One `Fetcher` per source and per run: a browser-like User-Agent (several
sites answer bare clients with an error page), a pause between two requests
to the same host, a couple of retries on network hiccups and 5xx, and a
request counter — the UI shows how many requests a check cost, which is the
only "cost" of these free sources.
"""
from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
HOST_DELAY_S = 1.0   # at most one request per second to any one site
TIMEOUT_S = 25


class SourceError(Exception):
    """A site is unreachable, blocks us, or answered something we can't read."""


class Fetcher:
    def __init__(self, delay: float = HOST_DELAY_S):
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "fr-CH,fr;q=0.9,de;q=0.6,en;q=0.5"},
            timeout=TIMEOUT_S,
            follow_redirects=True,
        )
        self.delay = delay
        self.requests = 0
        self._last: dict[str, float] = {}

    def _wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            pause = self.delay - (time.monotonic() - last)
            if pause > 0:
                time.sleep(pause)
        self._last[host] = time.monotonic()

    def request(self, method: str, url: str, retries: int = 2, **kwargs) -> httpx.Response:
        host = urlparse(url).netloc
        for attempt in range(retries + 1):
            self._wait(host)
            self.requests += 1
            try:
                resp = self.client.request(method, url, **kwargs)
            except httpx.TransportError as e:
                if attempt == retries:
                    raise SourceError(f"{host} injoignable ({e.__class__.__name__})")
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code >= 500 and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code in (403, 429):
                raise SourceError(f"{host} refuse les robots pour l'instant (HTTP {resp.status_code})")
            return resp
        raise SourceError(f"{host} ne répond pas")  # unreachable, keeps type checkers happy

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def get_json(self, url: str, **kwargs):
        resp = self.get(url, **kwargs)
        if resp.status_code != 200:
            raise SourceError(f"{urlparse(url).netloc} a répondu {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            raise SourceError(f"{urlparse(url).netloc} : réponse illisible")

    def close(self) -> None:
        self.client.close()
