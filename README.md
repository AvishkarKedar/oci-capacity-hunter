# 24/7 Oracle Cloud A1 Capacity Hunter & Auto Provisioner

Automated cloud runner that operates **24/7/365** on GitHub Actions to continuously hunt for released **Ampere A1 (ARM64)** capacity in Oracle Cloud Infrastructure (Phoenix region) and immediately provision the instance attached to an existing boot volume.

---

## ⚡ How It Works
1. Operates continuously in GitHub's cloud without touching your local machine, battery, or network bandwidth.
2. **Intelligent & Rate-Safe Timing**: Checks capacity with a competitive interval with randomized jitter (40s – 60s) to beat competing scripts while avoiding robotic pattern detection by Oracle Cloud WAF/edge gateways and preventing HTTP 429 TooManyRequests.
3. **Adaptive Backoff**: Implements exponential backoff with jitter on HTTP 429 (120s cooldown baseline).
4. **Resilient Network I/O**: Configured with 15s network timeout on all API operations, gracefully recovering from DNS or socket drops without crashing.
5. **Double-Launch Prevention**: Verifies before every single attempt whether the target instance is already in `PROVISIONING`, `STARTING`, or `RUNNING` status.
6. The moment an A1 slot is secured in `PHX-AD-2`:
   - Instantly locks in the VM shape (**2.0 OCPUs, 12.0 GB RAM**).
   - Attaches existing **200 GB Boot Volume**.
   - Polls until the VM enters `RUNNING` status.
   - Fetches assigned Public IP and Private IP, saving full details to `instance_info.json`.
   - Tests and verifies `https://os.avishkark.in` and `https://vpn.avishkark.in`.
   - Opens a **GitHub Issue** alert in this repository (triggering an instant push notification to your phone).
   - Automatically cancels active runs and disables future workflows to preserve resources.

---

## 🔒 Security
- All sensitive credentials (`OCI_PRIVATE_KEY`, `OCI_CONFIG_FILE`, `SSH_PUBLIC_KEY`, OCIDs) are securely encrypted inside **GitHub Repository Secrets**.
- GitHub Actions automatically masks all secrets (`***`) in execution logs.

---

## 📊 Live Tracking
To track live progress in real-time:
1. Open this repository on GitHub (or the GitHub mobile app).
2. Click on the **Actions** tab at the top.
3. Click the active workflow run to watch live timestamps, attempt logs, and status updates.
