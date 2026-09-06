from __future__ import annotations

import base64
import json

import pytest
import urllib.request

import scripts.ensure_docker_hub_public as docker_hub

from scripts.ensure_docker_hub_public import (
    _request_registry_token,
    _NoRedirectHandler,
    DockerHubConfig,
    DockerHubPublicError,
    DockerHubRequest,
    dockerhub_token_from_env,
    ensure_docker_hub_public,
    verify_docker_registry_push_access,
)


pytestmark = pytest.mark.unit


class FakeDockerHub:
    def __init__(self, responses: dict[tuple[str, str], object]) -> None:
        self.responses = responses
        self.requests: list[DockerHubRequest] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> object:
        request = DockerHubRequest(method=method, url=url, token=token, payload=payload)
        self.requests.append(request)
        response = self.responses.get((method, url))
        if isinstance(response, BaseException):
            raise response
        return response


def _config() -> DockerHubConfig:
    return DockerHubConfig(
        username="docker-user",
        token="docker-token",
        namespace="example",
        repository="broker",
        registry="registry.example",
        login_url="https://hub.example/v2/users/login",
        namespace_repositories_url="https://hub.example/v2/namespaces",
        legacy_repositories_url="https://hub.example/v2/repositories",
    )


def _registry_token(
    actions: list[str],
    *,
    name: str = "example/broker",
    audience: str = "registry.example",
    not_before: int = 900,
    expires: int = 1100,
) -> str:
    payload = {
        "aud": audience,
        "nbf": not_before,
        "exp": expires,
        "access": [{"type": "repository", "name": name, "actions": actions}],
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def _main_args() -> list[str]:
    return [
        "--username", "docker-user",
        "--namespace", "example",
        "--repository", "broker",
        "--registry", "registry.example",
        "--registry-auth-url", "https://auth.example/token",
        "--registry-service", "registry.example",
        "--login-url", "https://hub.example/login",
        "--namespace-repositories-url", "https://hub.example/namespaces",
        "--legacy-repositories-url", "https://hub.example/repositories",
        "--verify-attempts", "2",
        "--verify-retry-delay-seconds", "0.25",
    ]


def test_registry_push_access_accepts_exact_repository_push_scope() -> None:
    requests: list[tuple[str, str, str, str, str]] = []

    def request(url: str, username: str, token: str, service: str, scope: str) -> object:
        requests.append((url, username, token, service, scope))
        return {"token": _registry_token(["pull", "push"])}

    verify_docker_registry_push_access(
        _config(),
        registry_auth_url="https://auth.example/token",
        registry_service="registry.example",
        request=request,
        now=1000,
    )

    assert requests == [
        (
            "https://auth.example/token",
            "docker-user",
            "docker-token",
            "registry.example",
            "repository:example/broker:pull,push",
        )
    ]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"token": _registry_token(["pull"])}, "lacks push access"),
        ({"token": _registry_token(["pull", "push"], name="other/broker")}, "lacks push access"),
        ({"token": _registry_token(["pull", "push"], audience="other.example")}, "audience"),
        ({"token": _registry_token(["pull", "push"], expires=1000)}, "expired"),
        ({"token": _registry_token(["pull", "push"], not_before=1001)}, "not valid yet"),
        ({"token": "not-a-jwt"}, "malformed registry token"),
        ({"token": _registry_token(["pull", "push"]).replace(".", ".!!!!", 1)}, "malformed registry token"),
        ({}, "did not return a registry token"),
    ],
)
def test_registry_push_access_rejects_missing_or_invalid_scope(
    response: object,
    message: str,
) -> None:
    with pytest.raises(DockerHubPublicError, match=message):
        verify_docker_registry_push_access(
            _config(),
            registry_auth_url="https://auth.example/token",
            registry_service="registry.example",
            request=lambda *_args: response,
            now=1000,
        )


@pytest.mark.error_simulation
def test_registry_token_request_preserves_existing_query_and_drops_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"token":"registry-token"}'

    class FakeOpener:
        def open(self, request: object, *, timeout: int) -> FakeResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()

    def build_opener(*handlers: object) -> FakeOpener:
        captured["handlers"] = handlers
        return FakeOpener()

    monkeypatch.setattr("urllib.request.build_opener", build_opener)
    result = _request_registry_token(
        "https://auth.example/token?account=existing#ignored",
        "docker-user",
        "docker-token",
        "registry.example",
        "repository:example/broker:pull,push",
    )

    request = captured["request"]
    assert result == {"token": "registry-token"}
    assert request.full_url == (
        "https://auth.example/token?account=existing&service=registry.example&"
        "scope=repository%3Aexample%2Fbroker%3Apull%2Cpush"
    )
    assert "docker-token" not in request.full_url
    assert captured["timeout"] == 30
    assert any(type(handler).__name__ == "_NoRedirectHandler" for handler in captured["handlers"])


def test_registry_token_request_rejects_non_https_endpoint() -> None:
    with pytest.raises(DockerHubPublicError, match="must use HTTPS"):
        _request_registry_token(
            "http://auth.example/token",
            "docker-user",
            "docker-token",
            "registry.example",
            "repository:example/broker:pull,push",
        )


def test_registry_token_redirect_handler_never_forwards_authorization() -> None:
    request = urllib.request.Request(
        "https://auth.example/token",
        headers={"Authorization": "Basic encoded-credential"},
    )

    redirected = _NoRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://other.example/token",
    )

    assert redirected is None


@pytest.mark.error_simulation
def test_main_denied_push_scope_never_calls_docker_hub_repository_api(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository_api_calls: list[str] = []
    caplog.set_level(docker_hub.logging.ERROR, logger="ensure_docker_hub_public")

    result = docker_hub.main(
        _main_args(),
        environ={"DOCKERHUB_TOKEN": "docker-token"},
        registry_request=lambda *_args: {
            "token": _registry_token(
                ["pull"],
                audience="registry.example",
                not_before=0,
                expires=4_102_444_800,
            )
        },
        repository_request=lambda *_args, **_kwargs: repository_api_calls.append("called"),
    )

    assert result == 2
    assert repository_api_calls == []
    assert [record.getMessage() for record in caplog.records] == [
        "docker_hub_public_ensure_failed error="
        "Docker Hub credential lacks push access to example/broker"
    ]


@pytest.mark.error_simulation
def test_main_forwards_cli_config_and_dependencies(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logging_calls: list[dict[str, object]] = []
    sleep_calls: list[float] = []
    registry_calls: list[tuple[str, str, str, str, str]] = []
    anonymous_responses: list[object] = [
        DockerHubPublicError("repository visibility pending", status=404),
        {"is_private": False},
    ]
    fake = FakeDockerHub(
        {
            ("POST", "https://hub.example/login"): {"token": "jwt-token"},
            ("GET", "https://hub.example/namespaces/example/repositories/broker"): (
                DockerHubPublicError("repository missing", status=404)
            ),
            ("POST", "https://hub.example/namespaces/example/repositories"): {
                "is_private": False
            },
            ("GET", "https://hub.example/repositories/example/broker/"): {
                "is_private": False
            },
        }
    )
    monkeypatch.setattr(
        docker_hub.logging,
        "basicConfig",
        lambda **kwargs: logging_calls.append(kwargs),
    )
    monkeypatch.setattr(docker_hub.time, "sleep", sleep_calls.append)
    caplog.set_level(docker_hub.logging.INFO, logger="ensure_docker_hub_public")

    def registry_request(
        url: str,
        username: str,
        token: str,
        service: str,
        scope: str,
    ) -> object:
        registry_calls.append((url, username, token, service, scope))
        return {
            "token": _registry_token(
                ["pull", "push"],
                not_before=0,
                expires=4_102_444_800,
            )
        }

    def repository_request(
        method: str,
        url: str,
        *,
        token: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> object:
        if url == "https://hub.example/repositories/example/broker/":
            fake.requests.append(
                DockerHubRequest(method=method, url=url, token=token, payload=payload)
            )
            response = anonymous_responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return fake.request(method, url, token=token, payload=payload)

    result = docker_hub.main(
        _main_args(),
        environ={"DOCKERHUB_TOKEN": "docker-token"},
        registry_request=registry_request,
        repository_request=repository_request,
    )

    assert result == 0
    assert logging_calls == [
        {"level": docker_hub.logging.INFO, "format": "%(levelname)s %(message)s"}
    ]
    assert registry_calls == [
        (
            "https://auth.example/token",
            "docker-user",
            "docker-token",
            "registry.example",
            "repository:example/broker:pull,push",
        )
    ]
    assert fake.requests == [
        DockerHubRequest(
            method="POST",
            url="https://hub.example/login",
            payload={"username": "docker-user", "password": "docker-token"},
        ),
        DockerHubRequest(
            method="GET",
            url="https://hub.example/namespaces/example/repositories/broker",
            token="jwt-token",
        ),
        DockerHubRequest(
            method="POST",
            url="https://hub.example/namespaces/example/repositories",
            token="jwt-token",
            payload={
                "name": "broker",
                "namespace": "example",
                "repository_type": "image",
                "registry": "registry.example",
                "is_private": False,
            },
        ),
        DockerHubRequest(
            method="GET",
            url="https://hub.example/repositories/example/broker/",
        ),
        DockerHubRequest(
            method="GET",
            url="https://hub.example/repositories/example/broker/",
        ),
    ]
    assert anonymous_responses == []
    assert sleep_calls == [0.25]
    assert [record.getMessage() for record in caplog.records] == [
        "Docker Hub repository is not anonymous-public yet; retrying attempt 2/2",
        "docker_hub_public_ensure_passed result=created-public"
    ]


def test_dockerhub_token_comes_from_environment() -> None:
    assert dockerhub_token_from_env({"DOCKERHUB_TOKEN": "docker-token"}) == "docker-token"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_dockerhub_token_rejects_missing_or_blank_environment_value(
    value: str | None,
) -> None:
    environment = {} if value is None else {"DOCKERHUB_TOKEN": value}

    with pytest.raises(DockerHubPublicError) as excinfo:
        dockerhub_token_from_env(environment)

    assert str(excinfo.value) == "DOCKERHUB_TOKEN is required"


def test_ensure_docker_hub_public_creates_missing_public_repository() -> None:
    fake = FakeDockerHub(
        {
            ("POST", "https://hub.example/v2/users/login"): {"token": "jwt-token"},
            ("GET", "https://hub.example/v2/namespaces/example/repositories/broker"): DockerHubPublicError(
                "repository missing",
                status=404,
            ),
            ("POST", "https://hub.example/v2/namespaces/example/repositories"): {
                "name": "broker",
                "is_private": False,
            },
            ("GET", "https://hub.example/v2/repositories/example/broker/"): {
                "name": "broker",
                "is_private": False,
            },
        }
    )

    result = ensure_docker_hub_public(_config(), fake.request)

    assert result == "created-public"
    assert fake.requests[1].token == "jwt-token"
    assert fake.requests[2].payload == {
        "name": "broker",
        "namespace": "example",
        "repository_type": "image",
        "registry": "registry.example",
        "is_private": False,
    }
    assert fake.requests[-1].token is None


def test_ensure_docker_hub_public_leaves_existing_public_repository_unchanged() -> None:
    fake = FakeDockerHub(
        {
            ("POST", "https://hub.example/v2/users/login"): {"token": "jwt-token"},
            ("GET", "https://hub.example/v2/namespaces/example/repositories/broker"): {
                "name": "broker",
                "is_private": False,
            },
            ("GET", "https://hub.example/v2/repositories/example/broker/"): {
                "name": "broker",
                "is_private": False,
            },
        }
    )

    result = ensure_docker_hub_public(_config(), fake.request)

    assert result == "already-public"
    assert [request.method for request in fake.requests] == ["POST", "GET", "GET"]


def test_ensure_docker_hub_public_patches_private_repository_before_verifying() -> None:
    fake = FakeDockerHub(
        {
            ("POST", "https://hub.example/v2/users/login"): {"token": "jwt-token"},
            ("GET", "https://hub.example/v2/namespaces/example/repositories/broker"): {
                "name": "broker",
                "is_private": True,
            },
            ("PATCH", "https://hub.example/v2/namespaces/example/repositories/broker"): DockerHubPublicError(
                "method not allowed",
                status=405,
            ),
            ("PATCH", "https://hub.example/v2/repositories/example/broker/"): {
                "name": "broker",
                "is_private": False,
            },
            ("GET", "https://hub.example/v2/repositories/example/broker/"): {
                "name": "broker",
                "is_private": False,
            },
        }
    )

    result = ensure_docker_hub_public(_config(), fake.request)

    assert result == "updated-public"
    patch_requests = [request for request in fake.requests if request.method == "PATCH"]
    assert [request.url for request in patch_requests] == [
        "https://hub.example/v2/namespaces/example/repositories/broker",
        "https://hub.example/v2/repositories/example/broker/",
    ]
    assert [request.payload for request in patch_requests] == [{"is_private": False}] * 2


def test_ensure_docker_hub_public_fails_when_repository_remains_private() -> None:
    fake = FakeDockerHub(
        {
            ("POST", "https://hub.example/v2/users/login"): {"token": "jwt-token"},
            ("GET", "https://hub.example/v2/namespaces/example/repositories/broker"): {
                "name": "broker",
                "is_private": True,
            },
            ("PATCH", "https://hub.example/v2/namespaces/example/repositories/broker"): DockerHubPublicError(
                "method not allowed",
                status=405,
            ),
            ("PATCH", "https://hub.example/v2/repositories/example/broker/"): DockerHubPublicError(
                "method not allowed",
                status=405,
            ),
        }
    )

    with pytest.raises(DockerHubPublicError, match="could not make repository public"):
        ensure_docker_hub_public(_config(), fake.request)


def test_ensure_docker_hub_public_reports_successful_patch_that_stays_private() -> None:
    fake = FakeDockerHub(
        {
            ("POST", "https://hub.example/v2/users/login"): {"token": "jwt-token"},
            ("GET", "https://hub.example/v2/namespaces/example/repositories/broker"): {
                "name": "broker",
                "is_private": True,
            },
            ("PATCH", "https://hub.example/v2/namespaces/example/repositories/broker"): {
                "name": "broker",
                "is_private": True,
            },
            ("PATCH", "https://hub.example/v2/repositories/example/broker/"): {
                "name": "broker",
                "is_private": True,
            },
        }
    )

    with pytest.raises(DockerHubPublicError) as excinfo:
        ensure_docker_hub_public(_config(), fake.request)

    assert "returned is_private=True" in str(excinfo.value)
    assert "v2/namespaces/example/repositories/broker" in str(excinfo.value)
    assert "v2/repositories/example/broker/" in str(excinfo.value)
