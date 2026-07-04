"""Daemon JSON-RPC tools/call handling."""

from __future__ import annotations

from typing import Any

from mcp_broker.broker import BrokerToolError
from mcp_broker.catalog import BrokerCatalogFacade
from mcp_broker.hybrid_router import HybridRoutingContext, HybridToolRouter
from mcp_broker.jsonrpc import JsonRpcRequest, JsonRpcResponse


class BrokerDaemonToolCallMixin:
    def _handle_tools_call(self, request: JsonRpcRequest) -> JsonRpcResponse:
        if self.broker_config is None:
            return JsonRpcResponse.error(request.id, -32000, "broker config is not loaded")
        params = request.params
        if not isinstance(params, dict):
            return JsonRpcResponse.error(request.id, -32602, "tools/call params must be an object")
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return JsonRpcResponse.error(request.id, -32602, "tools/call name and arguments required")
        try:
            session_id = self._session_id_from_params(params)
            session_context = self._session_context_from_params(params)
            profile = self._effective_profile(params, session_context)
        except ValueError as exc:
            return JsonRpcResponse.error(request.id, -32602, str(exc))
        call_upstream = self._call_upstream_for_session(session_id, session_context)
        list_upstream = self._list_upstream_for_session(session_id, session_context)
        canonical_name = profile.canonical_broker_tool_name(name) if profile is not None else name
        if canonical_name.startswith("broker."):
            return self._handle_broker_catalog_tool_call(
                request_id=request.id,
                name=name,
                arguments=arguments,
                profile=profile,
                list_upstream=list_upstream,
                call_upstream=call_upstream,
                session_context=session_context,
            )
        return self._handle_upstream_tool_call(
            request_id=request.id,
            name=name,
            arguments=arguments,
            params=params,
            profile=profile,
            call_upstream=call_upstream,
            session_context=session_context,
        )

    def _handle_broker_catalog_tool_call(
        self,
        *,
        request_id: str | int | None,
        name: str,
        arguments: dict[str, Any],
        profile: Any,
        list_upstream: Any,
        call_upstream: Any,
        session_context: dict[str, str],
    ) -> JsonRpcResponse:
        try:
            result = BrokerCatalogFacade(
                broker_config=self.broker_config,
                profile=profile,
                list_upstream=list_upstream,
                call_upstream=call_upstream,
                call_locks=self._upstream_call_locks,
                status_provider=self._upstream_health_for_status,
                client_cwd=session_context.get("client_cwd"),
            ).call_tool(name, arguments)
        except (BrokerToolError, ValueError) as exc:
            return JsonRpcResponse.error(request_id, -32000, str(exc))
        return JsonRpcResponse.result(request_id, result)

    def _handle_upstream_tool_call(
        self,
        *,
        request_id: str | int | None,
        name: str,
        arguments: dict[str, Any],
        params: dict[str, Any],
        profile: Any,
        call_upstream: Any,
        session_context: dict[str, str],
    ) -> JsonRpcResponse:
        arguments = self._inject_cwd_project_arg(name, arguments, session_context)
        shared_context = HybridRoutingContext.from_params(params)
        try:
            result = HybridToolRouter(
                upstreams=self.broker_config.upstreams,
                settings=self.broker_config.broker,
                profile=profile,
                call_locks=self._upstream_call_locks,
                shared_worker=self._shared_worker_runtime,
            ).call_tool(
                advertised_name=name,
                arguments=arguments,
                edge_caller=call_upstream,
                tenant_context=shared_context.tenant_context,
                team_id=shared_context.team_id,
                quota_snapshot=shared_context.quota_snapshot,
            )
        except (BrokerToolError, ValueError) as exc:
            message = exc.message if isinstance(exc, BrokerToolError) else str(exc)
            return JsonRpcResponse.error(request_id, -32000, message)
        return JsonRpcResponse.result(request_id, result)
