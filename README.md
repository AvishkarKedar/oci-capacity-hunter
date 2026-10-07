# 24/7 Oracle Cloud A1 Capacity Hunter & Auto Provisioner

Automated cloud runner that operates **24/7/365** on GitHub Actions to continuously hunt for released **Ampere A1 (ARM64)** capacity in Oracle Cloud Infrastructure (Phoenix region) and immediately provision the instance attached to an existing boot volume.

---

## ⚡ How It Works
1. Runs continuously in GitHub's cloud without touching your local computer, battery, or network.
2. Checks capacity with an optimal 40-second interval (with automatic 75-second backoff on HTTP 429 rate limits).
3. The moment an A1 slot is freed up by another user in `PHX-AD-2`:
   - Instantly locks in the VM shape (**2 OCPUs, 12 GB RAM**).
   - Attaches your existing **200 GB Boot Volume**.
   - Captures the newly assigned Public IP and Private IP.
   - Tests and verifies `https://os.avishkark.in` and `https://vpn.avishkark.in`.
   - Automatically opens a **GitHub Issue** alert in this repository (which triggers an instant push notification and email to your phone!).

---

## 🔒 Security
- All sensitive credentials (`OCI_PRIVATE_KEY`, `OCI_CONFIG_FILE`, `SSH_PUBLIC_KEY`, OCIDs) are securely encrypted inside **GitHub Repository Secrets**.
- GitHub Actions automatically masks all secrets (`***`) in execution logs.

---

## 📊 Live Tracking
To track live progress in real-time:
1. Open this repository on GitHub (or the GitHub mobile app).
2. Click on the **Actions** tab at the top.
3. Click the active workflow run to watch live timestamps and attempt counts.
