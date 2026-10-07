import unittest
from unittest.mock import MagicMock, patch, mock_open
import json
import os
import socket
import urllib.error
import hunter
import oci

class TestHunter(unittest.TestCase):

    def test_jitter_interval_bounds(self):
        """Verify jitter intervals are within [55.0, 75.0] seconds."""
        for _ in range(100):
            val = hunter.random.uniform(hunter.JITTER_MIN, hunter.JITTER_MAX)
            self.assertGreaterEqual(val, 55.0)
            self.assertLessEqual(val, 75.0)

    def test_exponential_backoff_429(self):
        """Verify exponential backoff calculation increases on repeated 429s."""
        prev = 0
        for consecutive in range(1, 6):
            base = hunter.HTTP_429_BASE_COOLDOWN * (1.5 ** (consecutive - 1))
            cooldown = min(hunter.HTTP_429_MAX_COOLDOWN, base)
            self.assertGreaterEqual(cooldown, prev)
            self.assertLessEqual(cooldown, hunter.HTTP_429_MAX_COOLDOWN)
            prev = cooldown

    def test_check_existing_active_instance(self):
        """Test active instance detection across lifecycle states."""
        mock_client = MagicMock()
        
        # Test finding RUNNING instance
        running_inst = MagicMock()
        running_inst.display_name = "Avishkar"
        running_inst.lifecycle_state = "RUNNING"
        running_inst.id = "ocid1.instance.running"

        stopped_inst = MagicMock()
        stopped_inst.display_name = "Avishkar"
        stopped_inst.lifecycle_state = "STOPPED"
        stopped_inst.id = "ocid1.instance.stopped"

        other_inst = MagicMock()
        other_inst.display_name = "Other"
        other_inst.lifecycle_state = "RUNNING"
        other_inst.id = "ocid1.instance.other"

        mock_client.list_instances.return_value.data = [stopped_inst, running_inst, other_inst]
        found = hunter.check_existing_active_instance(mock_client, "tenancy_123", "Avishkar")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, "ocid1.instance.running")

        # Test finding PROVISIONING instance
        running_inst.lifecycle_state = "PROVISIONING"
        found = hunter.check_existing_active_instance(mock_client, "tenancy_123", "Avishkar")
        self.assertIsNotNone(found)

        # Test finding STARTING instance
        running_inst.lifecycle_state = "STARTING"
        found = hunter.check_existing_active_instance(mock_client, "tenancy_123", "Avishkar")
        self.assertIsNotNone(found)

        # Test when no active instance exists
        mock_client.list_instances.return_value.data = [stopped_inst, other_inst]
        found = hunter.check_existing_active_instance(mock_client, "tenancy_123", "Avishkar")
        self.assertIsNone(found)

        # Test when API throws an error
        mock_client.list_instances.side_effect = Exception("API connection dropped")
        found = hunter.check_existing_active_instance(mock_client, "tenancy_123", "Avishkar")
        self.assertIsNone(found)

    @patch("hunter.cancel_and_disable_workflows")
    @patch("hunter.notify_user_success")
    @patch("hunter.verify_endpoints")
    @patch("hunter.time.sleep")
    def test_handle_successful_provision(self, mock_sleep, mock_verify, mock_notify, mock_cancel):
        """Test the post-provisioning flow and instance_info.json creation."""
        mock_compute = MagicMock()
        mock_vn = MagicMock()

        inst_data = MagicMock()
        inst_data.lifecycle_state = "RUNNING"
        mock_compute.get_instance.return_value.data = inst_data

        vnic_att = MagicMock()
        vnic_att.vnic_id = "vnic_123"
        mock_compute.list_vnic_attachments.return_value.data = [vnic_att]

        vnic_obj = MagicMock()
        vnic_obj.public_ip = "129.146.10.20"
        vnic_obj.private_ip = "10.0.0.15"
        mock_vn.get_vnic.return_value.data = vnic_obj

        mock_verify.return_value = {
            "https://os.avishkark.in": "ONLINE (HTTP 200)",
            "https://vpn.avishkark.in": "ONLINE (HTTP 200)"
        }

        with patch("builtins.open", mock_open()) as mock_file:
            hunter.handle_successful_provision(
                mock_compute, mock_vn, "tenancy_test", "ocid1.inst.test", "Avishkar",
                "FbyS:PHX-AD-2", 2.0, 12.0, "ocid.bv", "ocid.subnet"
            )

        mock_verify.assert_called_once()
        mock_notify.assert_called_once()
        mock_cancel.assert_called_once()

    @patch("subprocess.run")
    def test_cancel_and_disable_workflows(self, mock_subproc):
        """Test disabling workflow and cancelling active runs."""
        mock_disable_res = MagicMock()
        mock_disable_res.returncode = 0
        mock_disable_res.stderr = ""

        mock_list_res = MagicMock()
        mock_list_res.returncode = 0
        mock_list_res.stdout = json.dumps([
            {"databaseId": 111, "status": "in_progress"},
            {"databaseId": 222, "status": "completed"},
            {"databaseId": 333, "status": "queued"}
        ])

        mock_subproc.side_effect = [mock_disable_res, mock_list_res, MagicMock(), MagicMock()]

        with patch.dict(os.environ, {"GITHUB_RUN_ID": "111"}):
            hunter.cancel_and_disable_workflows()

        calls = [c[0][0] for c in mock_subproc.call_args_list]
        self.assertIn(["gh", "workflow", "disable", "hunt.yml"], calls)
        self.assertIn(["gh", "run", "cancel", "333"], calls)
        self.assertNotIn(["gh", "run", "cancel", "111"], calls)
        self.assertNotIn(["gh", "run", "cancel", "222"], calls)

    @patch("urllib.request.urlopen")
    def test_verify_endpoints_success(self, mock_urlopen):
        """Test endpoint verification with successful responses."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        results = hunter.verify_endpoints()
        self.assertEqual(results["https://os.avishkark.in"], "ONLINE (HTTP 200)")
        self.assertEqual(results["https://vpn.avishkark.in"], "ONLINE (HTTP 200)")

    @patch("urllib.request.urlopen")
    @patch("hunter.time.sleep")
    def test_verify_endpoints_failure_handling(self, mock_sleep, mock_urlopen):
        """Test endpoint verification fallback when offline."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        results = hunter.verify_endpoints()
        self.assertIn("Starting up", results["https://os.avishkark.in"])
        self.assertIn("Starting up", results["https://vpn.avishkark.in"])


if __name__ == "__main__":
    unittest.main()
