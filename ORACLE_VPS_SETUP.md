# Oracle Cloud Free Tier VPS Setup Guide

## Why Oracle Free Tier?

Oracle Cloud offers an **Always Free** tier that includes:
- **4 ARM OCPUs** (vs 2 vCPUs on Hetzner)
- **24 GB RAM** (vs 4 GB on Hetzner)
- **200 GB storage**
- **US East (Ashburn)** region = <10ms latency to exchanges
- **FREE FOREVER** (not a trial)

This is a massive upgrade from your Hetzner VPS and eliminates the 100-200ms latency to US-based services.

---

## Step 1: Create Oracle Cloud Account

1. Go to [cloud.oracle.com](https://cloud.oracle.com)
2. Click "Start for Free"
3. Fill in details (credit card required for verification, but won't be charged)
4. Select **US East (Ashburn)** as your home region (critical for latency!)
5. Wait for account activation (usually instant)

---

## Step 2: Create Always Free VM

1. Go to **Compute → Instances → Create Instance**

2. **Name**: `polybot-us-east`

3. **Image and Shape**:
   - Click "Change Image"
   - Select **Ubuntu 22.04** (or latest)
   - Click "Change Shape"
   - Select **Ampere** (ARM processors)
   - Choose **VM.Standard.A1.Flex**
   - Set: **4 OCPUs, 24 GB RAM**
   
4. **Networking**:
   - Create new VCN or use existing
   - Assign public IP (auto-assigned)

5. **Add SSH Keys**:
   - Paste your public key from `~/.ssh/id_rsa.pub`
   - Or generate new key pair

6. Click **Create**

Wait 2-5 minutes for the instance to provision.

---

## Step 3: Configure Security List (Firewall)

By default, only SSH (port 22) is allowed. You need to open outbound traffic.

1. Go to **Networking → Virtual Cloud Networks**
2. Click your VCN
3. Click **Security Lists → Default Security List**
4. Add **Egress Rule**:
   - Stateless: No
   - Destination: 0.0.0.0/0
   - Protocol: All Protocols
   - Description: Allow all outbound

This allows the bot to connect to exchanges, Polymarket, Discord, etc.

---

## Step 4: Connect and Setup

```bash
# SSH into your new VPS
ssh ubuntu@<YOUR_PUBLIC_IP>

# Update system
sudo apt update && sudo apt upgrade -y

# Install Python
sudo apt install -y python3 python3-pip python3-venv git

# Clone your repo
git clone https://github.com/karlvfx/polybot.git
cd polybot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## Step 5: Configure Environment

```bash
# Copy your .env from Hetzner
scp root@your-hetzner-ip:/root/polybot/.env ~/polybot/.env

# Or create new .env
nano .env
```

Add your configuration:
```bash
MODE=alert
ASSETS=BTC,ETH,SOL
CHAINLINK__POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY
ALERTS__DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

---

## Step 6: Test Latency

```bash
# Test latency to key services
ping -c 5 stream.binance.com
ping -c 5 ws-feed.exchange.coinbase.com
ping -c 5 clob.polymarket.com

# Expected: <10ms (vs 100-200ms from Germany)
```

---

## Step 7: Run with Systemd

Create service file:
```bash
sudo nano /etc/systemd/system/polybot.service
```

```ini
[Unit]
Description=Polybot Trading Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/polybot
Environment="PATH=/home/ubuntu/polybot/venv/bin"
ExecStart=/home/ubuntu/polybot/venv/bin/python -m src.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable polybot
sudo systemctl start polybot
```

---

## Step 8: Verify It's Working

```bash
# Check status
sudo systemctl status polybot

# View logs
sudo journalctl -u polybot -f

# Check latency in logs (should show ms not seconds)
sudo journalctl -u polybot | grep -i latency
```

---

## Multi-Region Setup (Optional)

Once the Oracle VPS is running, you can set up **multi-region consensus**:

### On Oracle VPS (US):
- Run as primary bot
- Fastest signal detection

### On Hetzner VPS (EU):
- Run as shadow/backup
- Compare detection timestamps
- Failover if US goes down

To enable multi-region, set in `.env`:
```bash
REGION=us-east
PEER_REGION_URL=http://your-hetzner-ip:8080
```

---

## Troubleshooting

### "Shape VM.Standard.A1.Flex is not available"
- ARM instances are in high demand
- Try different availability domain (AD)
- Try again in a few hours
- Use the [OCI CLI to auto-retry](https://github.com/hitrov/oci-arm-host-capacity)

### SSH Connection Refused
- Check Security List has SSH ingress rule (port 22)
- Verify correct public IP
- Check instance is running

### Bot Can't Connect to Exchanges
- Check Security List has egress rule for all outbound
- Test with `curl https://api.binance.com/api/v3/time`

---

## Cost Summary

| Service | Cost |
|---------|------|
| Oracle Free Tier | €0/month |
| Hetzner (backup) | €4.35/month |
| **Total** | **€4.35/month** |

You get **6x the resources** and **10x lower latency** for the same price!

