from __future__ import annotations

import secrets

from kt_mcp_client.config import DeviceProfile, set_actual_endpoint


def set_test_endpoint(profile: DeviceProfile, endpoint: str) -> str:
    """Test-only stand-in for the private KTRuntime endpoint proof setup."""

    proof = secrets.token_urlsafe(32)
    profile._active_runtime_proof = proof
    return set_actual_endpoint(profile, endpoint, proof=proof)
