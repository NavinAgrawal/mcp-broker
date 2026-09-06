"""Cross-field validation for upstream lifecycle modes."""

from __future__ import annotations

from collections.abc import Mapping


def validate_upstream_transport_mode(name: str, mode: str, transport: str) -> None:
    if mode == "per_call" and transport != "stdio":
        raise ValueError(f"upstreams.{name}.mode per_call requires transport: stdio")


def validate_upstream_session_mode(
    name: str,
    mode: str,
    session_env: Mapping[str, str],
) -> None:
    if session_env and mode not in {"per_session", "per_call"}:
        raise ValueError(
            f"upstreams.{name}.session_env requires mode: per_session or per_call"
        )
