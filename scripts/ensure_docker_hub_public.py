#!/usr/bin/env python3
"""Ensure the Docker Hub release repository is public before publication starts."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import logging
import os
import sys
import time
from typing import Any
import urllib.error
import urllib.request


LOGGER = logging.getLogger("ensure_docker_hub_public")


class DockerHubPublicError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class DockerHubConfig:
    username: str
    token: str
    namespace: str
    repository: str
    registry: str
    login_url: str
    namespace_repositories_url: str
    legacy_repositories_url: str


@dataclass(frozen=True)
class DockerHubRequest:
    method: str
    url: str
    token: str | None = None
    payload: dict[str, object] | None = None


DockerHubRequester = Callable[
    [str, str],
    object,
]


def ensure_docker_hub_public(
    config: DockerHubConfig,
    request: Callable[
        [str, str],
        object,
    ] = None,
    *,
    verify_attempts: int = 1,
    verify_retry_delay_seconds: float = 0,
) -> str:
    requester = request or _request_json
    jwt = _login(config, requester)
    repository_url = _repository_url(config)

    try:
        repository = _expect_dict(
            requester("GET", repository_url, token=jwt),
            "Docker Hub repository",
        )
    except DockerHubPublicError as exc:
        if exc.status != 404:
            raise
        _create_public_repository(config, requester, jwt)
        _verify_anonymous_public(
            config,
            requester,
            attempts=verify_attempts,
            retry_delay_seconds=verify_retry_delay_seconds,
        )
        return "created-public"

    if repository.get("is_private") is False:
        _verify_anonymous_public(
            config,
            requester,
            attempts=verify_attempts,
            retry_delay_seconds=verify_retry_delay_seconds,
        )
        return "already-public"

    if repository.get("is_private") is not True:
        raise DockerHubPublicError("Docker Hub repository privacy field was missing")

    _patch_private_repository(config, requester, jwt)
    _verify_anonymous_public(
        config,
        requester,
        attempts=verify_attempts,
        retry_delay_seconds=verify_retry_delay_seconds,
    )
    return "updated-public"


def _login(
    config: DockerHubConfig,
    request: Callable[
        [str, str],
        object,
    ],
) -> str:
    payload = _expect_dict(
        request(
            "POST",
            config.login_url,
            payload={"username": config.username, "password": config.token},
        ),
        "Docker Hub login",
    )
    token = payload.get("token") or payload.get("jwt")
    if not isinstance(token, str) or not token:
        raise DockerHubPublicError("Docker Hub login did not return a bearer token")
    return token


def _create_public_repository(
    config: DockerHubConfig,
    request: Callable[
        [str, str],
        object,
    ],
    jwt: str,
) -> None:
    payload = _expect_dict(
        request(
            "POST",
            _namespace_repositories_url(config),
            token=jwt,
            payload={
                "name": config.repository,
                "namespace": config.namespace,
                "repository_type": "image",
                "registry": config.registry,
                "is_private": False,
            },
        ),
        "Docker Hub repository create",
    )
    if payload.get("is_private") is True:
        raise DockerHubPublicError("Docker Hub created the repository as private")


def _patch_private_repository(
    config: DockerHubConfig,
    request: Callable[
        [str, str],
        object,
    ],
    jwt: str,
) -> None:
    patch_urls = [
        _repository_url(config),
        _legacy_repository_url(config),
    ]
    failures: list[str] = []
    for url in patch_urls:
        try:
            payload = _expect_dict(
                request("PATCH", url, token=jwt, payload={"is_private": False}),
                "Docker Hub repository update",
            )
        except DockerHubPublicError as exc:
            failures.append(f"HTTP {exc.status}" if exc.status else "request failed")
            if exc.status in {400, 404, 405}:
                continue
            raise
        if payload.get("is_private") is False:
            return
        failures.append(f"{url} returned is_private={payload.get('is_private')!r}")
    raise DockerHubPublicError(
        "Docker Hub could not make repository public through the API; "
        "set the repository visibility to public in Docker Hub and rerun verification"
        + (f" ({', '.join(failures)})" if failures else "")
    )


def _verify_anonymous_public(
    config: DockerHubConfig,
    request: Callable[
        [str, str],
        object,
    ],
    *,
    attempts: int,
    retry_delay_seconds: float,
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            payload = _expect_dict(
                request("GET", _legacy_repository_url(config)),
                "Docker Hub anonymous repository",
            )
            if payload.get("is_private") is True:
                raise DockerHubPublicError("Docker Hub repository is still private")
            return
        except Exception as exc:
            last_error = exc
            if attempt >= max(1, attempts):
                break
            LOGGER.info(
                "Docker Hub repository is not anonymous-public yet; retrying attempt %s/%s",
                attempt + 1,
                max(1, attempts),
            )
            time.sleep(retry_delay_seconds)
    raise DockerHubPublicError("Docker Hub repository is not public anonymously") from last_error


def _namespace_repositories_url(config: DockerHubConfig) -> str:
    return f"{config.namespace_repositories_url.rstrip('/')}/{config.namespace}/repositories"


def _repository_url(config: DockerHubConfig) -> str:
    return f"{_namespace_repositories_url(config)}/{config.repository}"


def _legacy_repository_url(config: DockerHubConfig) -> str:
    return (
        f"{config.legacy_repositories_url.rstrip('/')}/"
        f"{config.namespace}/{config.repository}/"
    )


def _request_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    payload: dict[str, object] | None = None,
) -> object:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise DockerHubPublicError("Docker Hub API request failed", status=exc.code) from exc
    except OSError as exc:
        raise DockerHubPublicError("Docker Hub API request failed") from exc
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DockerHubPublicError("Docker Hub API response was not JSON") from exc


def _expect_dict(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DockerHubPublicError(f"{label} response was not a JSON object")
    return value


def dockerhub_token_from_env(environ: Mapping[str, str]) -> str:
    token = environ.get("DOCKERHUB_TOKEN", "").strip()
    if not token:
        raise DockerHubPublicError("DOCKERHUB_TOKEN is required")
    return token


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--login-url", required=True)
    parser.add_argument("--namespace-repositories-url", required=True)
    parser.add_argument("--legacy-repositories-url", required=True)
    parser.add_argument("--verify-attempts", type=int, default=6)
    parser.add_argument("--verify-retry-delay-seconds", type=float, default=10)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        token = dockerhub_token_from_env(os.environ)
        result = ensure_docker_hub_public(
            DockerHubConfig(
                username=args.username,
                token=token,
                namespace=args.namespace,
                repository=args.repository,
                registry=args.registry,
                login_url=args.login_url,
                namespace_repositories_url=args.namespace_repositories_url,
                legacy_repositories_url=args.legacy_repositories_url,
            ),
            verify_attempts=args.verify_attempts,
            verify_retry_delay_seconds=args.verify_retry_delay_seconds,
        )
    except DockerHubPublicError as exc:
        LOGGER.error("docker_hub_public_ensure_failed error=%s", exc)
        return 2
    LOGGER.info("docker_hub_public_ensure_passed result=%s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
