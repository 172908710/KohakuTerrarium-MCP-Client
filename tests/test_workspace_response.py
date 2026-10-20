"""Regressions based on KT 2.1.5's actual workspace-list response."""
import pytest
from kt_mcp_client.kt_runtime import KTRuntime


def test_native_envelope_ignores_management_and_registration_metadata():
    payload = {
        "source": "registry_snapshot", "management": {"state": "stopped"},
        "workspaces": [{"workspace_id": "localpc", "path": "D:/safe/workspace",
                        "registration_id": "example-registration", "state": "unloaded", "error": None}],
    }
    assert KTRuntime._workspace_entries(payload) == [("localpc", "D:/safe/workspace")]


def test_multiple_records_with_different_states_are_not_duplicate_workspaces():
    payload = {"workspaces": [
        {"workspace_id": "one", "path": "D:/one", "state": "ready"},
        {"workspace_id": "two", "path": "D:/two", "state": "unloaded"},
    ]}
    assert KTRuntime._workspace_entries(payload) == [("one", "D:/one"), ("two", "D:/two")]


def test_legacy_workspace_mapping_remains_supported():
    assert KTRuntime._workspace_entries({"workspaces": {"default": "D:/work"}}) == [("default", "D:/work")]


def test_empty_native_collection_does_not_parse_source_as_workspace():
    assert KTRuntime._workspace_entries({"source": "registry_snapshot", "management": {"state": "stopped"}, "workspaces": []}) == []
