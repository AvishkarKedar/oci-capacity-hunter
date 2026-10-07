import os
import sys
import time
import json
import ssl
import subprocess
import urllib.request
from datetime import datetime, timezone
import oci

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] {msg}", flush=True)

def verify_endpoints():
    endpoints = ["https://os.avishkark.in", "https://vpn.avishkark.in"]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    results = {}
    log("Verifying ecosystem endpoints via Cloudflare Tunnel...")
    for ep in endpoints:
        for attempt in range(12):
            try:
                req = urllib.request.Request(ep, headers={"User-Agent": "Mozilla/5.0 CapacityHunter/1.0"})
                with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
                    results[ep] = f"ONLINE (HTTP {resp.status})"
                    log(f"  [+] {ep} is {results[ep]}")
                    break
            except Exception as e:
                time.sleep(10)
        else:
            results[ep] = "Starting up (Cloudflare Tunnel connecting...)"
            log(f"  [-] {ep} pending: {results[ep]}")
    return results

def notify_user_success(instance_id, display_name, public_ip, private_ip, endpoints_status):
    title = f"🎉 Oracle Cloud Instance Successfully Provisioned! (IP: {public_ip})"
    body = f"""## 🚀 Oracle Cloud Instance Provisioned!

- **Instance Name:** `{display_name}`
- **Instance ID:** `{instance_id}`
- **Public IP:** `{public_ip}`
- **Private IP:** `{private_ip}`
- **Shape:** `VM.Standard.A1.Flex` (2 OCPUs, 12 GB RAM)
- **Time:** `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}`

### Ecosystem Status:
- **Cloud OS (`https://os.avishkark.in`):** {endpoints_status.get('https://os.avishkark.in', 'Checking...')}
- **VPN Panel (`https://vpn.avishkark.in`):** {endpoints_status.get('https://vpn.avishkark.in', 'Checking...')}

---
*Generated automatically by 24/7 Capacity Hunter.*
"""
    try:
        subprocess.run(["gh", "issue", "create", "--title", title, "--body", body], check=False)
        log("Created GitHub notification issue successfully!")
    except Exception as e:
        log(f"Could not create GitHub issue notification: {e}")

def main():
    log("Starting 24/7 Oracle Cloud A1 Capacity Hunter...")

    # Load configuration from environment / file
    config_path = os.path.expanduser("~/.oci/config")
    if not os.path.exists(config_path):
        log(f"ERROR: OCI config not found at {config_path}")
        return 1

    try:
        config = oci.config.from_file(config_path)
        oci.config.validate_config(config)
        log("OCI Config validated successfully.")
    except Exception as e:
        log(f"Config error: {e}")
        return 1

    compute_client = oci.core.ComputeClient(config)
    vn_client = oci.core.VirtualNetworkClient(config)

    tenancy_id = config["tenancy"]
    ad = os.environ.get("AVAILABILITY_DOMAIN", "FbyS:PHX-AD-2")
    boot_volume_id = os.environ.get("BOOT_VOLUME_ID")
    subnet_id = os.environ.get("SUBNET_ID")
    instance_name = os.environ.get("INSTANCE_NAME", "Avishkar")
    ocpu_count = float(os.environ.get("OCPU_COUNT", "2.0"))
    memory_gb = float(os.environ.get("MEMORY_GB", "12.0"))
    ssh_key = os.environ.get("SSH_PUBLIC_KEY")

    if not boot_volume_id or not subnet_id or not ssh_key:
        log("ERROR: Missing required environment variables (BOOT_VOLUME_ID, SUBNET_ID, or SSH_PUBLIC_KEY)")
        return 1

    log(f"Target: {instance_name} | {ocpu_count} OCPUs, {memory_gb} GB RAM | AD: {ad}")
    log(f"Boot Volume ID: {boot_volume_id[:25]}...")
    log(f"Subnet ID:      {subnet_id[:25]}...")

    # Check if instance is ALREADY running
    try:
        insts = compute_client.list_instances(compartment_id=tenancy_id).data
        for i in insts:
            if i.display_name == instance_name and i.lifecycle_state in ["RUNNING", "PROVISIONING", "STARTING"]:
                log(f"[!] Instance '{instance_name}' is ALREADY {i.lifecycle_state} (ID: {i.id})! Nothing to do.")
                return 0
    except Exception as e:
        log(f"Warning checking active instances: {e}")

    launch_details = oci.core.models.LaunchInstanceDetails(
        availability_domain=ad,
        compartment_id=tenancy_id,
        display_name=instance_name,
        shape="VM.Standard.A1.Flex",
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=ocpu_count,
            memory_in_gbs=memory_gb
        ),
        source_details=oci.core.models.InstanceSourceViaBootVolumeDetails(
            source_type="bootVolume",
            boot_volume_id=boot_volume_id
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            assign_public_ip=True,
            display_name="primary_vnic",
            subnet_id=subnet_id
        ),
        metadata={
            "ssh_authorized_keys": ssh_key
        }
    )

    attempt = 0
    max_runtime_seconds = 5 * 3600 + 20 * 60  # 5 hours 20 mins per workflow run
    start_time = time.time()

    while True:
        elapsed = time.time() - start_time
        if elapsed > max_runtime_seconds:
            log(f"Reached max workflow execution window ({int(elapsed/60)} mins). Exiting so next scheduled run takes over.")
            return 0

        attempt += 1
        try:
            resp = compute_client.launch_instance(launch_details)
            new_inst = resp.data
            log("=" * 60)
            log(f"[SUCCESS!] INSTANCE PROVISIONED: {new_inst.display_name} ({new_inst.id})")
            log(f"Lifecycle State: {new_inst.lifecycle_state}")
            log("=" * 60)

            log("Waiting for instance to reach RUNNING state...")
            public_ip = "Unknown"
            private_ip = "Unknown"

            for _ in range(30):
                time.sleep(10)
                try:
                    inst_info = compute_client.get_instance(new_inst.id).data
                    log(f"  Current status: {inst_info.lifecycle_state}")
                    if inst_info.lifecycle_state == "RUNNING":
                        break
                except Exception:
                    pass

            # Fetch IP Addresses
            try:
                vnics = compute_client.list_vnic_attachments(compartment_id=tenancy_id, instance_id=new_inst.id).data
                for v in vnics:
                    vnic = vn_client.get_vnic(v.vnic_id).data
                    if vnic.public_ip:
                        public_ip = vnic.public_ip
                    if vnic.private_ip:
                        private_ip = vnic.private_ip
                log(f"[+] Public IP:  {public_ip}")
                log(f"[+] Private IP: {private_ip}")
            except Exception as e:
                log(f"Error fetching VNIC details: {e}")

            # Verify public endpoints
            time.sleep(15)
            endpoints_status = verify_endpoints()

            # Send Notification Issue
            notify_user_success(new_inst.id, new_inst.display_name, public_ip, private_ip, endpoints_status)
            return 0

        except oci.exceptions.ServiceError as se:
            if se.status == 429:
                log(f"Attempt {attempt}: Rate limit encountered (HTTP 429). Cooling down 75 seconds...")
                time.sleep(75)
            elif se.status == 500 and "capacity" in str(se.message).lower():
                log(f"Attempt {attempt}: Out of host capacity in {ad}. Retrying in 40s...")
                time.sleep(40)
            else:
                log(f"Attempt {attempt}: OCI Error {se.status} ({se.code}): {se.message}. Retrying in 45s...")
                time.sleep(45)

        except Exception as e:
            log(f"Attempt {attempt}: Unexpected error: {e}. Retrying in 45s...")
            time.sleep(45)

if __name__ == "__main__":
    sys.exit(main())
