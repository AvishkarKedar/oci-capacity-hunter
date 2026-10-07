import unittest
from unittest.mock import MagicMock, patch, mock_open
import json
import os
import urllib.error
import hunter
import oci


class TestHunter(unittest.TestCase):

    def test_jitter_interval_bounds(self):
        """Verify jitter intervals are within [40.0, 60.0] seconds."""
        self.assertEqual(hunter.JITTER_MIN, 40.0)
        self.assertEqual(hunter.JITTER_MAX, 60.0)
        for _ in range(100):
            val = hunter.random.uniform(hunter.JITTER_MIN, hunter.JITTER_MAX)
            self.assertGreaterEqual(val, 40.0)
            self.assertLessEqual(val, 60.0)

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

        # Test when API throws an error: should bubble up to prevent double launch
        mock_client.list_instances.side_effect = Exception("API connection dropped")
        with self.assertRaises(Exception):
            hunter.check_existing_active_instance(mock_client, "tenancy_123", "Avishkar")

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
        vnic_att.lifecycle_state = "ATTACHED"
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
            res = hunter.handle_successful_provision(
                mock_compute, mock_vn, "tenancy_test", "ocid1.inst.test", "Avishkar",
                "FbyS:PHX-AD-2", 2.0, 12.0, "ocid.bv", "ocid.subnet"
            )

        self.assertTrue(res)
        mock_verify.assert_called_once()
        mock_notify.assert_called_once()
        mock_cancel.assert_called_once()

    @patch("hunter.cancel_and_disable_workflows")
    @patch("hunter.notify_user_success")
    @patch("hunter.time.sleep")
    def test_handle_successful_provision_terminated(self, mock_sleep, mock_notify, mock_cancel):
        """Test that terminated instances abort post-provision flow and do not disable hunting."""
        mock_compute = MagicMock()
        mock_vn = MagicMock()

        inst_data = MagicMock()
        inst_data.lifecycle_state = "TERMINATED"
        mock_compute.get_instance.return_value.data = inst_data

        with patch("builtins.open", mock_open()) as mock_file:
            res = hunter.handle_successful_provision(
                mock_compute, mock_vn, "tenancy_test", "ocid1.inst.test", "Avishkar",
                "FbyS:PHX-AD-2", 2.0, 12.0, "ocid.bv", "ocid.subnet"
            )

        self.assertFalse(res)
        mock_file.assert_not_called()
        mock_notify.assert_not_called()
        mock_cancel.assert_not_called()

    @patch("hunter.cancel_and_disable_workflows")
    @patch("hunter.notify_user_success")
    @patch("hunter.verify_endpoints")
    @patch("hunter.time.sleep")
    def test_handle_successful_provision_vnic_retry(self, mock_sleep, mock_verify, mock_notify, mock_cancel):
        """Test VNIC polling retries until public IP is bound."""
        mock_compute = MagicMock()
        mock_vn = MagicMock()

        inst_data = MagicMock()
        inst_data.lifecycle_state = "RUNNING"
        mock_compute.get_instance.return_value.data = inst_data

        vnic_att = MagicMock()
        vnic_att.vnic_id = "vnic_123"
        vnic_att.lifecycle_state = "ATTACHED"
        mock_compute.list_vnic_attachments.return_value.data = [vnic_att]

        # First poll: no public IP; Second poll: public IP assigned
        resp_pending = MagicMock()
        resp_pending.data = MagicMock(public_ip=None, private_ip="10.0.0.15")
        resp_ready = MagicMock()
        resp_ready.data = MagicMock(public_ip="129.146.10.20", private_ip="10.0.0.15")
        mock_vn.get_vnic.side_effect = [resp_pending, resp_ready]

        mock_verify.return_value = {
            "https://os.avishkark.in": "ONLINE (HTTP 200)",
            "https://vpn.avishkark.in": "ONLINE (HTTP 200)"
        }

        with patch("builtins.open", mock_open()) as mock_file:
            res = hunter.handle_successful_provision(
                mock_compute, mock_vn, "tenancy_test", "ocid1.inst.test", "Avishkar",
                "FbyS:PHX-AD-2", 2.0, 12.0, "ocid.bv", "ocid.subnet"
            )

        self.assertTrue(res)
        self.assertEqual(mock_vn.get_vnic.call_count, 2)

    @patch("subprocess.run")
    def test_cancel_and_disable_workflows(self, mock_subproc):
        """Test disabling workflow and cancelling active runs."""
        mock_disable_res = MagicMock(returncode=0, stderr="")
        mock_list_res = MagicMock(
            returncode=0,
            stdout=json.dumps([
                {"databaseId": 111, "status": "in_progress"},
                {"databaseId": 222, "status": "completed"},
                {"databaseId": 333, "status": "queued"}
            ])
        )

        mock_subproc.side_effect = [mock_disable_res, mock_list_res, MagicMock(), MagicMock()]

        with patch.dict(os.environ, {"GITHUB_RUN_ID": "111"}):
            hunter.cancel_and_disable_workflows()

        calls = [c[0][0] for c in mock_subproc.call_args_list]
        self.assertIn(["gh", "workflow", "disable", "hunt.yml"], calls)
        self.assertIn(["gh", "run", "cancel", "333"], calls)
        self.assertNotIn(["gh", "run", "cancel", "111"], calls)
        self.assertNotIn(["gh", "run", "cancel", "222"], calls)

    @patch("subprocess.run")
    def test_notify_user_success_duplicate_skip(self, mock_subproc):
        """Test that notify_user_success skips creation if issue already exists."""
        mock_list_res = MagicMock(returncode=0, stdout=json.dumps([{"number": 42}]))
        mock_subproc.return_value = mock_list_res

        hunter.notify_user_success("ocid1.test.id", "Avishkar", "1.2.3.4", "10.0.0.2", {}, 2.0, 12.0)

        # Only search called, create was NOT called
        calls = [c[0][0] for c in mock_subproc.call_args_list]
        self.assertEqual(len(calls), 1)
        self.assertIn("--search", calls[0])

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
    def test_verify_endpoints_http_auth(self, mock_urlopen):
        """Test endpoint verification recognizes HTTP 401/403 as ONLINE."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://vpn.avishkark.in", code=401, msg="Unauthorized", hdrs={}, fp=None
        )
        results = hunter.verify_endpoints()
        self.assertEqual(results["https://os.avishkark.in"], "ONLINE (HTTP 401)")
        self.assertEqual(results["https://vpn.avishkark.in"], "ONLINE (HTTP 401)")

    @patch("urllib.request.urlopen")
    @patch("hunter.time.sleep")
    def test_verify_endpoints_failure_handling(self, mock_sleep, mock_urlopen):
        """Test endpoint verification fallback when offline."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        results = hunter.verify_endpoints()
        self.assertIn("Starting up", results["https://os.avishkark.in"])
        self.assertIn("Starting up", results["https://vpn.avishkark.in"])

    @patch("hunter.handle_successful_provision")
    @patch("hunter.check_existing_active_instance")
    @patch("hunter.time.time")
    @patch("oci.core.VirtualNetworkClient")
    @patch("oci.core.ComputeClient")
    @patch("oci.config.validate_config")
    @patch("oci.config.from_file")
    @patch("os.path.exists")
    def test_main_preflight_double_launch_protection(
        self, mock_exists, mock_from_file, mock_validate, mock_compute, mock_vn, mock_time, mock_check, mock_handle
    ):
        """Verify that main() immediately exits 0 via handle_successful_provision without calling launch_instance."""
        mock_exists.return_value = True
        mock_from_file.return_value = {"tenancy": "ocid1.tenancy.test"}
        mock_time.side_effect = [100.0, 101.0, 102.0]

        mock_active = MagicMock()
        mock_active.id = "ocid1.inst.already_running"
        mock_active.display_name = "Avishkar"
        mock_active.lifecycle_state = "RUNNING"
        mock_check.return_value = mock_active
        mock_handle.return_value = True

        env_vars = {
            "SSH_PUBLIC_KEY": "ssh-ed25519 AAAAC3 test-key",
            "BOOT_VOLUME_ID": "ocid1.bootvolume.test",
            "SUBNET_ID": "ocid1.subnet.test"
        }
        with patch.dict(os.environ, env_vars):
            ret = hunter.main()

        self.assertEqual(ret, 0)
        mock_handle.assert_called_once()
        mock_compute.return_value.launch_instance.assert_not_called()

    @patch("hunter.time.time")
    @patch("oci.core.VirtualNetworkClient")
    @patch("oci.core.ComputeClient")
    @patch("oci.config.validate_config")
    @patch("oci.config.from_file")
    @patch("os.path.exists")
    def test_main_cycle_timeout(
        self, mock_exists, mock_from_file, mock_validate, mock_compute, mock_vn, mock_time
    ):
        """Verify that main() cleanly exits 0 when max cycle window is reached."""
        mock_exists.return_value = True
        mock_from_file.return_value = {"tenancy": "ocid1.tenancy.test"}
        mock_time.side_effect = [0.0, 20000.0]

        env_vars = {
            "SSH_PUBLIC_KEY": "ssh-ed25519 AAAAC3 test-key",
            "BOOT_VOLUME_ID": "ocid1.bootvolume.test",
            "SUBNET_ID": "ocid1.subnet.test"
        }
        with patch.dict(os.environ, env_vars):
            ret = hunter.main()

        self.assertEqual(ret, 0)
        mock_compute.return_value.launch_instance.assert_not_called()

    @patch("hunter.time.sleep")
    @patch("hunter.time.time")
    @patch("oci.core.VirtualNetworkClient")
    @patch("oci.core.ComputeClient")
    @patch("oci.config.validate_config")
    @patch("oci.config.from_file")
    @patch("os.path.exists")
    def test_main_capacity_retry_jitter_interval(
        self, mock_exists, mock_from_file, mock_validate, mock_compute, mock_vn, mock_time, mock_sleep
    ):
        """Verify that an OutOfCapacity error causes hunter to sleep between 40.0 and 60.0 seconds."""
        mock_exists.return_value = True
        mock_from_file.return_value = {"tenancy": "ocid1.tenancy.test"}
        # Cycle: start (0.0), elapsed check 1 (1.0), elapsed check 2 (20000.0 -> exit)
        mock_time.side_effect = [0.0, 1.0, 20000.0]

        mock_compute.return_value.list_instances.return_value.data = []
        se = oci.exceptions.ServiceError(
            status=500, code="OutOfCapacity", headers={}, message="Out of host capacity"
        )
        mock_compute.return_value.launch_instance.side_effect = se

        env_vars = {
            "SSH_PUBLIC_KEY": "ssh-ed25519 AAAAC3 test-key",
            "BOOT_VOLUME_ID": "ocid1.bootvolume.test",
            "SUBNET_ID": "ocid1.subnet.test"
        }
        with patch.dict(os.environ, env_vars):
            ret = hunter.main()

        self.assertEqual(ret, 0)
        mock_sleep.assert_called_once()
        sleep_arg = mock_sleep.call_args[0][0]
        self.assertGreaterEqual(sleep_arg, 40.0)
        self.assertLessEqual(sleep_arg, 60.0)

    @patch("hunter.time.sleep")
    @patch("hunter.time.time")
    @patch("oci.core.VirtualNetworkClient")
    @patch("oci.core.ComputeClient")
    @patch("oci.config.validate_config")
    @patch("oci.config.from_file")
    @patch("os.path.exists")
    def test_main_service_unavailable_fast_retry(
        self, mock_exists, mock_from_file, mock_validate, mock_compute, mock_vn, mock_time, mock_sleep
    ):
        """Verify that an HTTP 503 Service Unavailable triggers a fast retry (12s-20s)."""
        mock_exists.return_value = True
        mock_from_file.return_value = {"tenancy": "ocid1.tenancy.test"}
        mock_time.side_effect = [0.0, 1.0, 20000.0]

        mock_compute.return_value.list_instances.return_value.data = []
        se = oci.exceptions.ServiceError(
            status=503, code="ServiceUnavailable", headers={}, message="Gateway busy"
        )
        mock_compute.return_value.launch_instance.side_effect = se

        env_vars = {
            "SSH_PUBLIC_KEY": "ssh-ed25519 AAAAC3 test-key",
            "BOOT_VOLUME_ID": "ocid1.bootvolume.test",
            "SUBNET_ID": "ocid1.subnet.test"
        }
        with patch.dict(os.environ, env_vars):
            ret = hunter.main()

        self.assertEqual(ret, 0)
        mock_sleep.assert_called_once()
        sleep_arg = mock_sleep.call_args[0][0]
        self.assertGreaterEqual(sleep_arg, 12.0)
        self.assertLessEqual(sleep_arg, 20.0)

    @patch("hunter.handle_successful_provision")
    @patch("hunter.check_existing_active_instance")
    @patch("hunter.time.sleep")
    @patch("hunter.time.time")
    @patch("oci.core.VirtualNetworkClient")
    @patch("oci.core.ComputeClient")
    @patch("oci.config.validate_config")
    @patch("oci.config.from_file")
    @patch("os.path.exists")
    def test_main_conflict_recovery(
        self, mock_exists, mock_from_file, mock_validate, mock_compute, mock_vn, mock_time, mock_sleep, mock_check, mock_handle
    ):
        """Verify that an HTTP 409 Conflict checks for active instance and succeeds if found."""
        mock_exists.return_value = True
        mock_from_file.return_value = {"tenancy": "ocid1.tenancy.test"}
        mock_time.side_effect = [0.0, 1.0, 2.0]

        active_inst = MagicMock(id="ocid1.inst.conflict", display_name="Avishkar", lifecycle_state="PROVISIONING")
        # Pre-flight check: None. After conflict: active_inst found!
        mock_check.side_effect = [None, active_inst]
        mock_handle.return_value = True

        se = oci.exceptions.ServiceError(
            status=409, code="Conflict", headers={}, message="The boot volume is already in use"
        )
        mock_compute.return_value.launch_instance.side_effect = se

        env_vars = {
            "SSH_PUBLIC_KEY": "ssh-ed25519 AAAAC3 test-key",
            "BOOT_VOLUME_ID": "ocid1.bootvolume.test",
            "SUBNET_ID": "ocid1.subnet.test"
        }
        with patch.dict(os.environ, env_vars):
            ret = hunter.main()

        self.assertEqual(ret, 0)
        mock_handle.assert_called_once()
        self.assertEqual(mock_check.call_count, 2)

    @patch("hunter.check_existing_active_instance")
    @patch("hunter.time.sleep")
    @patch("hunter.time.time")
    @patch("oci.core.VirtualNetworkClient")
    @patch("oci.core.ComputeClient")
    @patch("oci.config.validate_config")
    @patch("oci.config.from_file")
    @patch("os.path.exists")
    def test_main_network_drop_fast_recovery(
        self, mock_exists, mock_from_file, mock_validate, mock_compute, mock_vn, mock_time, mock_sleep, mock_check
    ):
        """Verify that a socket drop or timeout checks instance status and sleeps with short backoff (5s-10s)."""
        mock_exists.return_value = True
        mock_from_file.return_value = {"tenancy": "ocid1.tenancy.test"}
        mock_time.side_effect = [0.0, 1.0, 20000.0]

        mock_check.return_value = None
        mock_compute.return_value.launch_instance.side_effect = TimeoutError("Connection reset by peer")

        env_vars = {
            "SSH_PUBLIC_KEY": "ssh-ed25519 AAAAC3 test-key",
            "BOOT_VOLUME_ID": "ocid1.bootvolume.test",
            "SUBNET_ID": "ocid1.subnet.test"
        }
        with patch.dict(os.environ, env_vars):
            ret = hunter.main()

        self.assertEqual(ret, 0)
        mock_sleep.assert_called_once()
        sleep_arg = mock_sleep.call_args[0][0]
        self.assertGreaterEqual(sleep_arg, 5.0)
        self.assertLessEqual(sleep_arg, 10.0)

    @patch("hunter.time.sleep")
    @patch("hunter.time.time")
    @patch("oci.core.VirtualNetworkClient")
    @patch("oci.core.ComputeClient")
    @patch("oci.config.validate_config")
    @patch("oci.config.from_file")
    @patch("os.path.exists")
    def test_main_zero_latency_immediate_launch(
        self, mock_exists, mock_from_file, mock_validate, mock_compute, mock_vn, mock_time, mock_sleep
    ):
        """Verify that within hunting loop, launch_instance is called immediately without calling list_instances on each retry."""
        mock_exists.return_value = True
        mock_from_file.return_value = {"tenancy": "ocid1.tenancy.test"}
        # Cycle: start (0.0), elapsed check 1 (1.0), elapsed check 2 (2.0), elapsed check 3 (20000.0 -> exit)
        mock_time.side_effect = [0.0, 1.0, 2.0, 20000.0]

        mock_compute.return_value.list_instances.return_value.data = []
        se = oci.exceptions.ServiceError(
            status=500, code="OutOfCapacity", headers={}, message="Out of host capacity"
        )
        mock_compute.return_value.launch_instance.side_effect = se

        env_vars = {
            "SSH_PUBLIC_KEY": "ssh-ed25519 AAAAC3 test-key",
            "BOOT_VOLUME_ID": "ocid1.bootvolume.test",
            "SUBNET_ID": "ocid1.subnet.test"
        }
        with patch.dict(os.environ, env_vars):
            ret = hunter.main()

        self.assertEqual(ret, 0)
        # Pre-flight called list_instances once; inside the loop 2 launch attempts occurred with ZERO list_instances calls
        self.assertEqual(mock_compute.return_value.launch_instance.call_count, 2)
        self.assertEqual(mock_compute.return_value.list_instances.call_count, 1)


if __name__ == "__main__":
    unittest.main()
