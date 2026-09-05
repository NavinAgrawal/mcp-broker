import json
from pathlib import Path
import uuid

import pytest

from mcp_broker.client import ClientShim
from mcp_broker.daemon import BrokerDaemon


pytestmark = pytest.mark.journey


def test_client_shim_round_trips_attachment_sized_request(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    socket_path = Path("/tmp") / f"mcp-broker-journey-{uuid.uuid4().hex}.sock"
    daemon = BrokerDaemon(runtime_root=runtime_root, socket_path=socket_path)
    payload = json.dumps(
        {
            "method": "broker/health",
            "id": "attachment-sized-health",
            "attachment_probe": "x" * 818_504,
        }
    ).encode("utf-8") + b"\n"

    daemon.start()
    try:
        response = json.loads(ClientShim(socket_path).forward_payload(payload).decode("utf-8"))
    finally:
        daemon.stop()

    assert response["id"] == "attachment-sized-health"
    assert response["result"]["status"] == "ok"
