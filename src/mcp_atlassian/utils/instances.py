"""Named Atlassian instances, selected per request.

One server process normally talks to one Jira and one Confluence, taken from
the environment. A deployment that fronts several of them -- a second
Confluence on another host, each behind its own SSO gate -- declares them in a
JSON file named by ``ATLASSIAN_INSTANCES_FILE``::

    {
      "jira": {
        "main": {"url": "https://jira.example.com",
                 "cookie_file": "/session/jira-cookie",
                 "projects_filter": "PROJ"}
      },
      "confluence": {
        "main": {"url": "https://confluence.example.com",
                 "cookie_file": "/session/confluence-cookie"},
        "labs": {"url": "https://labs.example.com/wiki",
                 "cookie_file": "/session/labs-cookie"}
      }
    }

A request picks one with the ``X-Atlassian-Jira-Instance`` or
``X-Atlassian-Confluence-Instance`` header. The credential stays on the
server: it is the session cookie in ``cookie_file``, read again for every
request, so a session renewed by whatever writes that file is picked up
without a restart.

The URLs come from the operator, not from the caller, which is why an instance
may carry a credential where a per-request ``X-Atlassian-*-Url`` may not.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("mcp-atlassian.instances")

INSTANCES_FILE_ENV = "ATLASSIAN_INSTANCES_FILE"
SERVICES = ("jira", "confluence")
JIRA_INSTANCE_HEADER = "X-Atlassian-Jira-Instance"
CONFLUENCE_INSTANCE_HEADER = "X-Atlassian-Confluence-Instance"
INSTANCE_HEADERS = {
    "jira": JIRA_INSTANCE_HEADER,
    "confluence": CONFLUENCE_INSTANCE_HEADER,
}
# What an instance may set besides its URL and credential.
_FILTER_KEYS = {"jira": "projects_filter", "confluence": "spaces_filter"}


class InstanceError(ValueError):
    """An instance is unknown, or its definition or credential is unusable."""


@dataclasses.dataclass(frozen=True)
class Instance:
    """One declared Jira or Confluence instance."""

    service: str
    name: str
    url: str
    cookie_file: Path
    filter_value: str | None = None
    ssl_verify: bool = True

    def read_cookie(self) -> str:
        """Return the session cookie, as it is on disk right now.

        Raises:
            InstanceError: If the file is missing or empty. The message names
                the instance and the file, never the cookie.
        """
        try:
            cookie = self.cookie_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise InstanceError(
                f"No session for {self.service} instance '{self.name}': "
                f"cannot read {self.cookie_file} ({error.strerror})."
            ) from error
        cookie = cookie.replace("\r", "").replace("\n", "")
        if not cookie:
            raise InstanceError(
                f"No session for {self.service} instance '{self.name}': "
                f"{self.cookie_file} is empty."
            )
        return cookie

    def config_kwargs(self) -> dict[str, Any]:
        """Arguments for ``JiraConfig`` / ``ConfluenceConfig`` for this instance."""
        kwargs: dict[str, Any] = {
            "url": self.url,
            "auth_type": "external",
            "custom_headers": {"Cookie": self.read_cookie()},
            "ssl_verify": self.ssl_verify,
        }
        kwargs[_FILTER_KEYS[self.service]] = self.filter_value
        return kwargs


_cache_lock = threading.Lock()
_cache: tuple[str, float, dict[str, dict[str, Instance]]] | None = None


def _parse(path: Path) -> dict[str, dict[str, Instance]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise InstanceError(
            f"Cannot read {INSTANCES_FILE_ENV}={path} ({error.strerror})."
        ) from error
    except json.JSONDecodeError as error:
        raise InstanceError(f"{path} is not valid JSON: {error}.") from error
    if not isinstance(raw, dict):
        raise InstanceError(f"{path} must hold a JSON object.")
    unknown = set(raw) - set(SERVICES)
    if unknown:
        raise InstanceError(
            f"{path}: unknown service(s) {sorted(unknown)}; expected {list(SERVICES)}."
        )

    instances: dict[str, dict[str, Instance]] = {service: {} for service in SERVICES}
    for service, declared in raw.items():
        if not isinstance(declared, dict):
            raise InstanceError(f"{path}: '{service}' must map names to instances.")
        for name, spec in declared.items():
            where = f"{path}: {service} instance '{name}'"
            if not isinstance(spec, dict):
                raise InstanceError(f"{where} must be an object.")
            url, cookie_file = spec.get("url"), spec.get("cookie_file")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                raise InstanceError(f"{where} needs an http(s) 'url'.")
            if not isinstance(cookie_file, str) or not cookie_file:
                raise InstanceError(f"{where} needs a 'cookie_file'.")
            filter_value = spec.get(_FILTER_KEYS[service])
            if filter_value is not None and not isinstance(filter_value, str):
                raise InstanceError(
                    f"{where}: '{_FILTER_KEYS[service]}' must be a string."
                )
            instances[service][name] = Instance(
                service=service,
                name=name,
                url=url.rstrip("/"),
                cookie_file=Path(cookie_file).expanduser(),
                filter_value=filter_value,
                ssl_verify=bool(spec.get("ssl_verify", True)),
            )
    return instances


def load_instances() -> dict[str, dict[str, Instance]]:
    """Return the declared instances, keyed by service then name.

    The file is read again when it changes on disk. With no file configured
    every service maps to an empty dict.

    Raises:
        InstanceError: If the configured file cannot be read or is malformed.
    """
    global _cache
    configured = os.getenv(INSTANCES_FILE_ENV, "").strip()
    if not configured:
        return {service: {} for service in SERVICES}
    path = Path(configured).expanduser()
    try:
        mtime = path.stat().st_mtime
    except OSError as error:
        raise InstanceError(
            f"Cannot read {INSTANCES_FILE_ENV}={path} ({error.strerror})."
        ) from error
    with _cache_lock:
        if _cache is not None and _cache[0] == str(path) and _cache[1] == mtime:
            return _cache[2]
        instances = _parse(path)
        _cache = (str(path), mtime, instances)
        logger.info(
            "Loaded Atlassian instances from %s: %s",
            path,
            {service: sorted(names) for service, names in instances.items()},
        )
        return instances


def get_instance(service: str, name: str) -> Instance:
    """Return one declared instance.

    Raises:
        InstanceError: If no instance of that service has that name.
    """
    declared = load_instances().get(service, {})
    if name not in declared:
        known = ", ".join(sorted(declared)) or "none"
        raise InstanceError(f"Unknown {service} instance '{name}'. Declared: {known}.")
    return declared[name]


def instances_configured() -> bool:
    """Whether an instances file is configured at all."""
    return bool(os.getenv(INSTANCES_FILE_ENV, "").strip())
