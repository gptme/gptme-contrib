"""Basic tests for gptme-whatsapp setup module."""

from gptme_whatsapp.setup import generate_systemd_service


def test_generate_systemd_service():
    """Test that systemd service generation works."""
    service = generate_systemd_service(
        agent_name="sven",
        workspace="/home/sven/sven",
        allowed_contacts=["447700900000"],
    )
    assert "sven" in service
    assert "/home/sven/sven" in service
    assert "447700900000" in service
    assert "ExecStart=" in service
    assert "[Unit]" in service
    assert "[Service]" in service
    assert "[Install]" in service


def test_generate_systemd_service_no_contacts():
    """Test service generation with no contact filter."""
    service = generate_systemd_service(
        agent_name="test-agent",
        workspace="/tmp/test",
        allowed_contacts=[],
    )
    assert "ALLOWED_CONTACTS=" in service
    assert "test-agent" in service
