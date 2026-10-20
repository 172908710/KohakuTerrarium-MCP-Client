"""SSH lifecycle tests use fake ManagedProcess objects only."""

from dataclasses import dataclass

from kt_mcp_client.config import DeviceProfile
from kt_mcp_client.process import ProcessStatus
from kt_mcp_client.ssh_tunnel import SSHTunnel


@dataclass
class FakeProcess:
    status_value: ProcessStatus
    starts: int = 0

    def start(self, command, *, identity_tokens):
        self.starts += 1
        return self.status_value

    def stop(self):
        return True

    def status(self):
        return self.status_value


def profile(tmp_path):
    identity = tmp_path / "id"
    known = tmp_path / "known"
    identity.write_text("id", encoding="utf-8")
    known.write_text("host ssh-ed25519 AAAA", encoding="utf-8")
    return DeviceProfile(
        workspace_path=str(tmp_path), kt_python=__file__, kt_home=str(tmp_path / "home"),
        public_origin="https://example.invalid", ssh_host="host", ssh_user="user",
        ssh_identity_file=str(identity), ssh_known_hosts=str(known),
    )


def test_immediate_exit_is_degraded(tmp_path):
    value = profile(tmp_path)
    fake = FakeProcess(ProcessStatus("ssh-tunnel", False, 123, False, 1))
    tunnel = SSHTunnel(value, tmp_path / "runtime", process=fake)
    result = tunnel.start()
    assert not result.process.running
    assert "立即退出" in result.detail


def test_healthy_ssh_status_explicitly_does_not_claim_public_routing(tmp_path):
    value = profile(tmp_path)
    fake = FakeProcess(ProcessStatus("ssh-tunnel", True, 123, True, None))
    tunnel = SSHTunnel(value, tmp_path / "runtime", process=fake)
    result = tunnel.start()
    assert result.process.running and result.process.identity_ok
    assert "VPS 路由仍需单独配置" in result.detail
