#!/usr/bin/env python3
"""Ensure the Docker Hub release repository is public before publication starts."""

from __future__ import annotations

import argparse
import base64
import binascii
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import logging
import os
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
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

RegistryTokenRequester = Callable[[str, str, str, str, str], object]


def verify_docker_registry_push_access(
    config: DockerHubConfig,
    *,
    registry_auth_url: str,
    registry_service: str,
    request: RegistryTokenRequester | None = None,
    now: float | None = None,
) -> None:
    scope = f"repository:{config.namespace}/{config.repository}:pull,push"
    requester = request or _request_registry_token
    response = _expect_dict(
        requester(
            registry_auth_url,
            config.username,
            config.token,
            registry_service,
            scope,
        ),
        "Docker registry authorization",
    )
    token = response.get("token") or response.get("access_token")
    if not isinstance(token, str) or not token:
        raise DockerHubPublicError(
            "Docker registry authorization did not return a registry token"
        )
    claims = _registry_token_claims(token)
    _verify_registry_token_claims(
        claims,
        registry_service=registry_service,
        now=time.time() if now is None else now,
    )
    repository = f"{config.namespace}/{config.repository}"
    for grant in claims["access"]:
        if grant.get("type") != "repository" or grant.get("name") != repository:
            continue
        actions = grant.get("actions")
        if isinstance(actions, list) and "push" in actions:
            return
    raise DockerHubPublicError(
        f"Docker Hub credential lacks push access to {repository}"
    )


def _registry_token_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise DockerHubPublicError("Docker Hub returned a malformed registry token")
    payload_segment = parts[1]
    padding = "=" * (-len(payload_segment) % 4)
    try:
        payload = json.loads(
            base64.b64decode(
                payload_segment + padding,
                altchars=b"-_",
                validate=True,
            ).decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise DockerHubPublicError(
            "Docker Hub returned a malformed registry token"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("access"), list):
        raise DockerHubPublicError("Docker Hub returned a malformed registry token")
    payload["access"] = [grant for grant in payload["access"] if isinstance(grant, dict)]
    return payload


def _verify_registry_token_claims(
    claims: Mapping[str, Any],
    *,
    registry_service: str,
    now: float,
) -> None:
    audience = claims.get("aud")
    audiences = {audience} if isinstance(audience, str) else set()
    if isinstance(audience, list) and all(isinstance(value, str) for value in audience):
        audiences.update(audience)
    if registry_service not in audiences:
        raise DockerHubPublicError("Docker Hub registry token audience mismatch")
    not_before = claims.get("nbf")
    expires = claims.get("exp")
    if (
        isinstance(not_before, bool)
        or not isinstance(not_before, (int, float))
        or isinstance(expires, bool)
        or not isinstance(expires, (int, float))
    ):
        raise DockerHubPublicError("Docker Hub registry token validity claims are malformed")
    if not_before > now:
        raise DockerHubPublicError("Docker Hub registry token is not valid yet")
    if expires <= now:
        raise DockerHubPublicError("Docker Hub registry token is expired")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _request_registry_token(
    url: str,
    username: str,
    token: str,
    service: str,
    scope: str,
) -> object:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise DockerHubPublicError("Docker registry authorization URL must use HTTPS")
    query_items = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query_items.extend((("service", service), ("scope", scope)))
    request_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query_items), "")
    )
    authorization = base64.b64encode(f"{username}:{token}".encode()).decode()
    request = urllib.request.Request(
        request_url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Basic {authorization}",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise DockerHubPublicError(
            "Docker registry authorization failed",
            status=exc.code,
        ) from exc
    except OSError as exc:
        raise DockerHubPublicError("Docker registry authorization failed") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DockerHubPublicError(
            "Docker registry authorization response was not JSON"
        ) from exc


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
    parser.add_argument("--registry-auth-url", required=True)
    parser.add_argument("--registry-service", required=True)
    parser.add_argument("--login-url", required=True)
    parser.add_argument("--namespace-repositories-url", required=True)
    parser.add_argument("--legacy-repositories-url", required=True)
    parser.add_argument("--verify-attempts", type=int, default=6)
    parser.add_argument("--verify-retry-delay-seconds", type=float, default=10)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    registry_request: RegistryTokenRequester | None = None,
    repository_request: DockerHubRequester | None = None,
) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        token = dockerhub_token_from_env(os.environ if environ is None else environ)
        config = DockerHubConfig(
            username=args.username,
            token=token,
            namespace=args.namespace,
            repository=args.repository,
            registry=args.registry,
            login_url=args.login_url,
            namespace_repositories_url=args.namespace_repositories_url,
            legacy_repositories_url=args.legacy_repositories_url,
        )
        verify_docker_registry_push_access(
            config,
            registry_auth_url=args.registry_auth_url,
            registry_service=args.registry_service,
            request=registry_request,
        )
        result = ensure_docker_hub_public(
            config,
            request=repository_request,
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
