from __future__ import annotations

import pytest

from scripts.ensure_docker_hub_public import (
    DockerHubConfig,
    DockerHubPublicError,
    DockerHubRequest,
    dockerhub_token_from_env,
    ensure_docker_hub_public,
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
