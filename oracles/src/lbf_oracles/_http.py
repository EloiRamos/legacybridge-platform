"""Minimal stdlib HTTP helpers (loopback test endpoints only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


def request(method: str, url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None,
            timeout: float = 10.0) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})  # noqa: S310 - test URLs
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read(), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())


def get_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 10.0) -> Any:
    status, body, _ = request("GET", url, headers=headers, timeout=timeout)
    if status != 200:
        raise RuntimeError(f"GET {url} -> HTTP {status}")
    return json.loads(body)


def post_json(url: str, obj: Any, *, timeout: float = 30.0) -> Any:
    status, body, _ = request("POST", url, data=json.dumps(obj).encode(), timeout=timeout,
                              headers={"Content-Type": "application/json"})
    if status != 200:
        raise RuntimeError(f"POST {url} -> HTTP {status}: {body[:200]!r}")
    return json.loads(body) if body else None
