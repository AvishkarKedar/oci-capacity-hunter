import os
import sys
import time
import json
import ssl
import random
import socket
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone
import oci

# ---------------------------------------------------------
# Timing & Jitter Configuration
# ---------------------------------------------------------
# Competitive 40s - 60s interval with randomized jitter (average ~50s)
# to beat competing free-tier launch scripts while safely evading
# robotic pattern detection and HTTP 429 TooManyRequests edge rate limits.
JITTER_MIN = 40.0
JITTER_MAX = 60.0

# Exponential backoff on HTTP 429 starting at 120s cooldown
HTTP_429_BASE_COOLDOWN = 120.0
HTTP_429_MAX_COOLDOWN = 300.0

# 15s network timeout on all API calls
NETWORK_TIMEOUT_SECONDS = 15

# 5.5-hour execution limit per job with 5-minute safety buffer
MAX_RUNTIME_SECONDS = int(5.5 * 3600 - 300)  # 19,500s (~5h 25m)


def log(msg: str):
    """Print timestamped UTC log message with immediate flush."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def verify_endpoints() -> dict:
    """Test and verify ecosystem endpoints via Cloudflare Tunnel with 15s timeout."""
    endpoints = ["https://os.avishkark.in", "https://vpn.avishkark.in"]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    results = {ep: "Starting up (Cloudflare Tunnel connecting...)" for ep in endpoints}
    pending = set(endpoints)

    log("Verifying ecosystem endpoints via Cloudflare Tunnel...")
    for attempt in range(1, 13):
        for ep in list(pending):
            try:
                req = urllib.request.Request(
                    ep,
                    headers={"User-Agent": "Mozilla/5.0 CapacityHunter/2.0"}
                )
                with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT_SECONDS, context=ctx) as resp:
                    results[ep] = f"ONLINE (HTTP {resp.status})"
                    log(f"  [+] {ep} is {results[ep]} (attempt {attempt}/12)")
                    pending.remove(ep)
            except urllib.error.HTTPError as he:
                # Any HTTP response from origin server (even 401/403/404) indicates service is reachable
                if he.code < 500:
                    results[ep] = f"ONLINE (HTTP {he.code})"
                    log(f"  [+] {ep} is {results[ep]} (attempt {attempt}/12)")
                    pending.remove(ep)
                else:
                    log(f"  [-] {ep} attempt {attempt}/12 server returned HTTP {he.code}")
            except Exception as e:
                log(f"  [-] {ep} attempt {attempt}/12 status check: {e}")

        if not pending:
            break
        time.sleep(10)

    for ep in pending:
        log(f"  [-] {ep} final status: {results[ep]}")
    return results


def cancel_and_disable_workflows():
    """Disable hunt.yml and cancel any queued/in-progress workflow runs."""
    log("Disabling hunt.yml workflow and cancelling active runs...")
    try:
        res = subprocess.run(
            ["gh", "workflow", "disable", "hunt.yml"],
            capture_output=True, text=True, check=False
        )
        if res.returncode == 0:
            log("[+] Workflow 'hunt.yml' successfully disabled.")
        else:
            log(f"[-] Could not disable workflow: {res.stderr.strip()}")
    except Exception as e:
        log(f"[-] Error calling gh workflow disable: {e}")

    try:
        current_run_id = os.environ.get("GITHUB_RUN_ID")
        res = subprocess.run(
            ["gh", "run", "list", "--workflow=hunt.yml", "--json", "databaseId,status"],
            capture_output=True, text=True, check=False
        )
        if res.returncode == 0 and res.stdout.strip():
            runs = json.loads(res.stdout)
            for r in runs:
                r_id = str(r.get("databaseId"))
                status = r.get("status")
                if status in ["queued", "in_progress", "waiting", "requested"] and r_id != current_run_id:
                    log(f"  Cancelling active/queued workflow run #{r_id} (status: {status})...")
                    subprocess.run(["gh", "run", "cancel", r_id], check=False)
    except Exception as e:
        log(f"[-] Error cancelling active workflow runs: {e}")


def notify_user_success(instance_id: str, display_name: str, public_ip: str, private_ip: str, endpoints_status: dict, ocpus: float, memory_gb: float):
    """Create a GitHub Issue in the repo with full instance details (triggers instant phone push)."""
    # Prevent duplicate issue creation if one already exists for this instance
    try:
        check_issue = subprocess.run(
            ["gh", "issue", "list", "--search", instance_id, "--json", "number"],
            capture_output=True, text=True, check=False
        )
        if check_issue.returncode == 0 and check_issue.stdout.strip():
            existing_issues = json.loads(check_issue.stdout)
            if existing_issues:
                log(f"[+] Notification issue already exists (#{existing_issues[0]['number']}) for instance {instance_id}. Skipping creation.")
                return
    except Exception as e:
        log(f"[-] Notice checking existing issues: {e}")

    title = f"🎉 Oracle Cloud Instance Successfully Provisioned! (IP: {public_ip})"
    body = f"""## 🚀 Oracle Cloud Instance Provisioned!

- **Instance Name:** `{display_name}`
- **Instance ID:** `{instance_id}`
- **Public IP:** `{public_ip}`
- **Private IP:** `{private_ip}`
- **Shape:** `VM.Standard.A1.Flex` ({ocpus} OCPUs, {memory_gb} GB RAM)
- **Time:** `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}`

### Ecosystem Status:
- **Cloud OS (`https://os.avishkark.in`):** {endpoints_status.get('https://os.avishkark.in', 'Checking...')}
- **VPN Panel (`https://vpn.avishkark.in`):** {endpoints_status.get('https://vpn.avishkark.in', 'Checking...')}

---
*Generated automatically by 24/7 Capacity Hunter. All hunter workflows have been cleanly halted.*
"""
    try:
        res = subprocess.run(
            ["gh", "issue", "create", "--title", title, "--body", body],
            capture_output=True, text=True, check=False
        )
        if res.returncode == 0:
            log(f"[+] Created GitHub notification issue successfully: {res.stdout.strip()}")
        else:
            log(f"[-] GitHub issue create failed: {res.stderr.strip()}")
    except Exception as e:
        log(f"[-] Could not create GitHub issue notification: {e}")


def check_existing_active_instance(compute_client, tenancy_id: str, target_name: str):
    """Verify whether target instance is already PROVISIONING, STARTING, or RUNNING."""
    insts = compute_client.list_instances(compartment_id=tenancy_id, display_name=target_name).data
    for i in insts:
        if i.display_name == target_name and i.lifecycle_state in ["PROVISIONING", "STARTING", "RUNNING"]:
            return i
    return None


def handle_successful_provision(
    compute_client,
    vn_client,
    tenancy_id: str,
    instance_id: str,
    display_name: str,
    availability_domain: str,
    ocpu_count: float,
    memory_gb: float,
    boot_volume_id: str,
    subnet_id: str
) -> bool:
    """Post-provisioning handler: poll RUNNING state, fetch IPs, save JSON, verify endpoints, alert, and disable."""
    log("=" * 65)
    log(f"[SUCCESS] Target Instance active: {display_name} ({instance_id})")
    log("=" * 65)

    log("Polling instance until RUNNING state...")
    current_state = "UNKNOWN"
    for poll in range(1, 61):  # Max 10 mins (60 * 10s)
        try:
            inst_info = compute_client.get_instance(instance_id).data
            current_state = inst_info.lifecycle_state
            log(f"  Polling [{poll}/60]: Status = {current_state}")
            if current_state == "RUNNING":
                break
            elif current_state in ["TERMINATED", "TERMINATING"]:
                log(f"  [ERROR] Instance entered {current_state} state! Aborting post-provision flow.")
                return False
        except Exception as e:
            log(f"  Warning polling instance status: {e}")
        time.sleep(10)

    if current_state in ["TERMINATED", "TERMINATING"]:
        log(f"[-] Cannot proceed: instance is in {current_state} state.")
        return False

    # Fetch Public and Private IP addresses with retry for VNIC binding
    log("Retrieving VNIC IP addresses...")
    public_ip = "Not Assigned"
    private_ip = "Not Assigned"
    for vnic_poll in range(1, 13):
        try:
            vnics = compute_client.list_vnic_attachments(compartment_id=tenancy_id, instance_id=instance_id).data
            for v in vnics:
                if v.lifecycle_state == "ATTACHED" or not v.lifecycle_state:
                    vnic = vn_client.get_vnic(v.vnic_id).data
                    if vnic.public_ip:
                        public_ip = vnic.public_ip
                    if vnic.private_ip:
                        private_ip = vnic.private_ip
            if public_ip != "Not Assigned" and private_ip != "Not Assigned":
                break
        except Exception as e:
            log(f"  Warning fetching VNIC details (attempt {vnic_poll}/12): {e}")
        time.sleep(5)

    log(f"[+] Public IP:  {public_ip}")
    log(f"[+] Private IP: {private_ip}")

    # Allow network settling before endpoint health check
    log("Allowing initial boot settling (15s) before endpoint checks...")
    time.sleep(15)
    endpoints_status = verify_endpoints()

    # Write instance_info.json
    instance_info = {
        "instance_id": instance_id,
        "display_name": display_name,
        "lifecycle_state": current_state,
        "public_ip": public_ip,
        "private_ip": private_ip,
        "availability_domain": availability_domain,
        "shape": "VM.Standard.A1.Flex",
        "ocpus": ocpu_count,
        "memory_gb": memory_gb,
        "boot_volume_id": boot_volume_id,
        "subnet_id": subnet_id,
        "provisioned_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "endpoints": endpoints_status
    }
    try:
        with open("instance_info.json", "w", encoding="utf-8") as f:
            json.dump(instance_info, f, indent=2)
        log("[+] Wrote instance_info.json successfully.")
    except Exception as e:
        log(f"[-] Warning writing instance_info.json: {e}")

    # Send GitHub Issue alert
    notify_user_success(instance_id, display_name, public_ip, private_ip, endpoints_status, ocpu_count, memory_gb)

    # Cancel other runs and disable workflow
    cancel_and_disable_workflows()
    return True


def main():
    log("Starting 24/7 Oracle Cloud A1 Capacity Hunter (Optimized & Rate-Safe)...")

    # Load and validate OCI config
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

    # Initialize OCI clients with 15s network timeout on all API calls
    timeout_tuple = (NETWORK_TIMEOUT_SECONDS, NETWORK_TIMEOUT_SECONDS)
    compute_client = oci.core.ComputeClient(config, timeout=timeout_tuple)
    vn_client = oci.core.VirtualNetworkClient(config, timeout=timeout_tuple)

    tenancy_id = config["tenancy"]

    # Target instance parameters
    ad = os.environ.get("AVAILABILITY_DOMAIN") or "FbyS:PHX-AD-2"
    boot_volume_id = os.environ.get("BOOT_VOLUME_ID") or "ocid1.bootvolume.oc1.phx.abyhqljsm2i5ikkuzg25z74cq7walpxnb2fc37tnvs42jucrsi6hkzcd5g4q"
    subnet_id = os.environ.get("SUBNET_ID") or "ocid1.subnet.oc1.phx.aaaaaaaag2vh7buztgwuj242kx7v7ls62nuxdrh6zuupwaiiypkaz3i6o5mq"
    instance_name = os.environ.get("INSTANCE_NAME") or "Avishkar"
    ocpu_count = float(os.environ.get("OCPU_COUNT") or 2.0)
    memory_gb = float(os.environ.get("MEMORY_GB") or 12.0)
    ssh_key = os.environ.get("SSH_PUBLIC_KEY")

    if not ssh_key:
        for pub_path in [
            os.path.expanduser("~/.ssh/oracle_script_key.pub"),
            os.path.expanduser("~/.ssh/id_rsa.pub"),
            os.path.expanduser("~/.ssh/id_ed25519.pub"),
        ]:
            if os.path.exists(pub_path):
                try:
                    with open(pub_path, "r", encoding="utf-8") as f:
                        ssh_key = f.read().strip()
                        log(f"Loaded SSH public key from {pub_path}")
                        break
                except Exception:
                    pass

    if not boot_volume_id or not subnet_id or not ssh_key:
        log("ERROR: Missing required configuration (BOOT_VOLUME_ID, SUBNET_ID, or SSH_PUBLIC_KEY)")
        return 1

    log(f"Target Instance: {instance_name} | Shape: VM.Standard.A1.Flex ({ocpu_count} OCPUs, {memory_gb} GB RAM)")
    log(f"Availability Domain: {ad}")
    log(f"Boot Volume ID:      {boot_volume_id[:25]}...")
    log(f"Subnet ID:           {subnet_id[:25]}...")
    log(f"Timing Configuration: Competitive 40s-60s interval ({JITTER_MIN}s-{JITTER_MAX}s randomized jitter)")
    log(f"Rate-limit handling: Exponential backoff starting at {HTTP_429_BASE_COOLDOWN}s cooldown")
    log(f"API Network Timeout: {NETWORK_TIMEOUT_SECONDS}s with socket drop resiliency")
    log(f"Cycle Max Window:    {MAX_RUNTIME_SECONDS / 3600:.2f} hours")

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
    consecutive_429 = 0
    start_time = time.time()

    while True:
        elapsed = time.time() - start_time
        if elapsed > MAX_RUNTIME_SECONDS:
            log(f"Maximum cycle window elapsed ({elapsed / 60:.1f} mins). Exiting cleanly for next chained workflow cycle.")
            return 0

        attempt += 1

        # 1. Verify before EACH attempt whether instance is already running/provisioning
        try:
            existing = check_existing_active_instance(compute_client, tenancy_id, instance_name)
            if existing:
                log(f"[!] Active instance '{instance_name}' found in {existing.lifecycle_state} state (ID: {existing.id})! Double-launch prevented.")
                if handle_successful_provision(
                    compute_client, vn_client, tenancy_id, existing.id, existing.display_name,
                    ad, ocpu_count, memory_gb, boot_volume_id, subnet_id
                ):
                    return 0
                else:
                    log(f"[!] Active instance '{instance_name}' failed post-provisioning checks. Continuing hunt...")

            # 2. Attempt instance launch
            log(f"Attempt {attempt}: Requesting VM launch ({instance_name}, {ocpu_count} OCPUs, {memory_gb} GB RAM in {ad})...")
            resp = compute_client.launch_instance(launch_details)
            new_inst = resp.data
            consecutive_429 = 0

            if handle_successful_provision(
                compute_client, vn_client, tenancy_id, new_inst.id, new_inst.display_name,
                ad, ocpu_count, memory_gb, boot_volume_id, subnet_id
            ):
                return 0
            else:
                log(f"[!] Launched instance did not reach RUNNING state. Continuing hunt...")

        except oci.exceptions.ServiceError as se:
            if se.status == 429:
                consecutive_429 += 1
                base = HTTP_429_BASE_COOLDOWN * (1.5 ** (consecutive_429 - 1))
                cooldown = min(HTTP_429_MAX_COOLDOWN, base) + random.uniform(5.0, 15.0)
                log(f"Attempt {attempt}: Rate limit encountered (HTTP 429). Consecutive: {consecutive_429}. Backoff cooldown: {cooldown:.1f}s...")
                time.sleep(cooldown)
            elif se.status == 500 and (
                "capacity" in str(se.message).lower()
                or "capacity" in str(se.code).lower()
                or se.code in ["InternalError", "LimitExceeded"]
            ):
                consecutive_429 = 0
                delay = random.uniform(JITTER_MIN, JITTER_MAX)
                log(f"Attempt {attempt}: Out of host capacity in {ad}. Retrying in {delay:.1f}s (jittered 40s-60s)...")
                time.sleep(delay)
            else:
                consecutive_429 = 0
                delay = random.uniform(JITTER_MIN, JITTER_MAX)
                log(f"Attempt {attempt}: OCI Service Error {se.status} ({se.code}): {se.message}. Retrying in {delay:.1f}s...")
                time.sleep(delay)

        except (oci.exceptions.ConnectTimeout, oci.exceptions.RequestException, socket.error, TimeoutError, urllib.error.URLError) as ne:
            consecutive_429 = 0
            delay = random.uniform(25.0, 35.0)
            log(f"Attempt {attempt}: Network / DNS / Socket drop or timeout (15s limit): {ne}. Retrying in {delay:.1f}s...")
            time.sleep(delay)

        except Exception as e:
            consecutive_429 = 0
            delay = random.uniform(JITTER_MIN, JITTER_MAX)
            log(f"Attempt {attempt}: Unexpected error: {e}. Retrying in {delay:.1f}s...")
            time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
