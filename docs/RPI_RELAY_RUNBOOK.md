# Running relay.py on Raspberry Pi 5

This runbook explains how to deploy `relay.py` on a Raspberry Pi 5 using Docker Compose and expose it through a Cloudflare Tunnel. The goal is to keep the workflow simple so a single `docker compose up` command can start everything.

## Prerequisites

- Raspberry Pi OS 64‑bit (tested on Raspberry Pi 5)
- Docker and Docker Compose installed on the Pi
- A Cloudflare account with a registered domain
- The `token.place` repository cloned to the Pi

## 1. Install Docker

```bash
sudo apt update
sudo apt install -y docker.io docker-compose
sudo usermod -aG docker $USER
newgrp docker
```

Log out and back in if needed so Docker commands work without `sudo`.

## 2. Clone the repository

```bash
git clone https://github.com/futuroptimist/token.place.git
cd token.place
```

 
The relay listens on port 5000 and is already configured to connect to the
default server container, so no manual setup is required.
## 3. Start relay with Docker Compose

The compose file defines the relay service. Start it with:

```bash
docker compose up -d
```

The relay listens on port 5000 by default.

## 4. Set up Cloudflare Tunnel

1. Install cloudflared on the Pi:
   ```bash
   sudo apt install -y cloudflared
   ```
2. Authenticate with Cloudflare and create a tunnel:
   ```bash
   cloudflared tunnel login
   cloudflared tunnel create tokenplace-relay
   ```
   Note the generated tunnel ID.
3. Create `~/.cloudflared/config.yml` and point it at the relay:
   ```yaml
tunnel: TUNNEL_ID
credentials-file: /home/pi/.cloudflared/TUNNEL_ID.json

ingress:
  - hostname: relay.your-domain.com
    service: http://localhost:5000
  - service: http_status:404
   ```
4. Run the tunnel (or add it as a service):
   ```bash
   cloudflared tunnel run tokenplace-relay
   ```

Once the tunnel is active, requests to `relay.your-domain.com` will reach `relay.py` running in Docker on the Pi.

## 5. Verify connectivity

Open a browser and navigate to your Cloudflare hostname. The token.place landing page should load, and chatting in the UI will send requests to your existing server instance.

That's it! You now have a repeatable way to run `relay.py` on a Raspberry Pi 5 and expose it securely through Cloudflare.
