# Polybot VPS Setup Guide

Complete guide for deploying Polybot on a Hetzner Cloud VPS for 24/7 operation.

## Why VPS?

| Issue | Local Machine | VPS |
|-------|---------------|-----|
| Network drops | WiFi/ISP hiccups | Datacenter-grade uptime |
| Sleep/hibernation | Interrupts bot | Never sleeps |
| Uptime | Only when laptop open | 24/7/365 |
| Latency | Variable | Consistent, low |
| Power outages | Kills bot | Redundant power |

---

## Server Recommendation

**Hetzner CX22** - €4.35/month
- 2 vCPU (Intel)
- 4 GB RAM
- 40 GB SSD
- 20 TB Traffic

Other options:
| Provider | Price | RAM |
|----------|-------|-----|
| Hetzner CX22 | €4.35/mo | 4GB |
| DigitalOcean | $6/mo | 1GB |
| Vultr | $6/mo | 1GB |
| AWS Lightsail | $5/mo | 1GB |

---

## Initial Setup

### 1. Create Server at Hetzner

1. Go to [console.hetzner.cloud](https://console.hetzner.cloud)
2. Click **Add Server**
3. Configure:
   - **Location**: Falkenstein (EU) or Ashburn (US)
   - **Image**: Ubuntu 24.04
   - **Type**: CX22
   - **SSH Key**: Add your public key
   - **Name**: `polybot`
4. Create & Buy

### 2. Generate SSH Key (on your Mac)

```bash
# Generate key if you don't have one
ssh-keygen -t ed25519 -C "polybot"

# View public key (add this to Hetzner)
cat ~/.ssh/id_ed25519.pub
```

### 3. Connect to Server

```bash
ssh root@YOUR_SERVER_IP
```

---

## Server Configuration

### 4. Update System

```bash
apt update && apt upgrade -y
```

### 5. Install Dependencies

```bash
apt install -y python3 python3-pip python3-venv git screen htop curl
```

### 6. Create Bot User (Optional but Recommended)

```bash
adduser polybot
usermod -aG sudo polybot
su - polybot
```

---

## Install Polybot

### 7. Clone Repository

```bash
git clone https://github.com/karlvfx/polybot.git
cd polybot
```

### 8. Setup Python Environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 9. Configure Environment

```bash
nano .env
```

```env
# Operating Mode
MODE=alert

# Assets to trade
ASSETS=BTC,ETH,SOL

# Discord Webhook
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK

# Polygon RPC (for Chainlink)
POLYGON_RPC_URL=https://polygon-rpc.com
POLYGON_WS_URL=wss://polygon-bor-rpc.publicnode.com

# Polymarket Credentials (for live trading)
POLYMARKET_API_KEY=
POLYMARKET_SECRET=
POLYMARKET_PASSPHRASE=

# Logging
LOG_LEVEL=INFO
```

Save: `Ctrl+X`, then `Y`, then `Enter`

### 10. Test Run

```bash
source venv/bin/activate
python -m src.main
```

Press `Ctrl+C` to stop if working.

---

## Run as System Service (Auto-restart)

### 11. Create Systemd Service

```bash
sudo nano /etc/systemd/system/polybot.service
```

```ini
[Unit]
Description=Polybot Trading Bot
After=network.target

[Service]
Type=simple
User=polybot
WorkingDirectory=/home/polybot/polybot
Environment="PATH=/home/polybot/polybot/venv/bin"
ExecStart=/home/polybot/polybot/venv/bin/python -m src.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Note**: If running as root, change `User=polybot` to `User=root` and update `WorkingDirectory` to `/root/polybot`.

### 12. Enable & Start Service

```bash
sudo systemctl daemon-reload
sudo systemctl enable polybot
sudo systemctl start polybot
```

---

## Daily Commands

### Service Management

```bash
# Start bot
sudo systemctl start polybot

# Stop bot
sudo systemctl stop polybot

# Restart bot
sudo systemctl restart polybot

# Check status
sudo systemctl status polybot

# View live logs
sudo journalctl -u polybot -f

# View last 100 log lines
sudo journalctl -u polybot -n 100
```

### Update Code

```bash
cd ~/polybot
git pull
sudo systemctl restart polybot
```

### One-liner Update

```bash
cd ~/polybot && git pull && sudo systemctl restart polybot && sudo journalctl -u polybot -f
```

---

## Workflow: Mac → VPS

```
┌─────────────────┐     git push     ┌─────────────┐     git pull     ┌─────────────┐
│   Your Mac      │ ───────────────► │   GitHub    │ ◄─────────────── │    VPS      │
│ (development)   │                  │  (remote)   │                  │ (production)│
└─────────────────┘                  └─────────────┘                  └─────────────┘
```

**On Mac (after making changes):**
```bash
git add -A
git commit -m "description of changes"
git push
```

**On VPS (to deploy):**
```bash
cd ~/polybot && git pull && sudo systemctl restart polybot
```

---

## Optional: Convenience Aliases

Add to `~/.bashrc` on VPS:

```bash
# Quick aliases
alias bot-start='sudo systemctl start polybot'
alias bot-stop='sudo systemctl stop polybot'
alias bot-restart='sudo systemctl restart polybot'
alias bot-status='sudo systemctl status polybot'
alias bot-logs='sudo journalctl -u polybot -f'
alias bot-update='cd ~/polybot && git pull && sudo systemctl restart polybot && sudo journalctl -u polybot -f'
```

Then run `source ~/.bashrc` to activate.

Usage:
```bash
bot-logs      # Watch live logs
bot-update    # Pull & restart
bot-restart   # Just restart
```

---

## Optional: Firewall Setup

```bash
# Allow SSH only
sudo ufw allow ssh
sudo ufw enable
sudo ufw status
```

---

## Optional: Auto-Update Script

Create `~/update-polybot.sh`:

```bash
#!/bin/bash
cd /home/polybot/polybot
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart polybot
echo "Polybot updated at $(date)"
```

```bash
chmod +x ~/update-polybot.sh
```

---

## Troubleshooting

### Bot won't start
```bash
# Check logs for errors
sudo journalctl -u polybot -n 50

# Test manually
cd ~/polybot
source venv/bin/activate
python -m src.main
```

### Permission denied
```bash
# Fix ownership
sudo chown -R polybot:polybot /home/polybot/polybot
```

### Can't connect to server
```bash
# Check if server is running in Hetzner console
# Try resetting root password if SSH key not working
```

### Out of memory
```bash
# Check memory usage
htop

# Upgrade to larger server in Hetzner console
```

---

## Quick Reference

| Task | Command |
|------|---------|
| SSH into server | `ssh root@YOUR_IP` or `ssh polybot@YOUR_IP` |
| Start bot | `sudo systemctl start polybot` |
| Stop bot | `sudo systemctl stop polybot` |
| Restart bot | `sudo systemctl restart polybot` |
| View logs | `sudo journalctl -u polybot -f` |
| Update & restart | `cd ~/polybot && git pull && sudo systemctl restart polybot` |
| Check disk space | `df -h` |
| Check memory | `free -h` |
| Check CPU | `htop` |

---

## Cost Summary

- **Hetzner CX22**: €4.35/month
- **Uptime**: 99.9%+
- **Automatic restarts**: Yes (systemd)
- **Survives reboot**: Yes

Total: **~€52/year** for 24/7 professional hosting 🚀

sudo systemctl stop polybot && sleep 2 && sudo journalctl -u polybot -n 300 --no-pager