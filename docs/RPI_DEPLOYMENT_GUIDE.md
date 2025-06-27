# Raspberry Pi Deployment Guide

This guide combines the runbook, k3s cluster instructions and bill of materials into a single place. It documents how we built a three-node Raspberry Pi 5 cluster for token.place and captures some lessons learned along the way.

## Bill of Materials

- **Raspberry Pi 5** boards (4GB or 8GB RAM)
- **PoE+ HAT with M.2 SATA slot** (2230/2242) for each Pi. Most PoE+ HATs include an M.2 connector, which we prefer for SSD boot.
- **256GB M.2 SATA SSD** (TS256GMTS430S) per Pi
- **64GB microSD card** (one card can be reused for all nodes)
- **PoE+ network switch** and **Ethernet cables**
- **Cooling solution** such as a fan case or heatsink
- **Optional: USB-C power supply** if you are not using PoE
- **Cloudflare account** with a registered domain for tunneling

This list reflects our setup. Other hardware choices will also work. Contributions describing different configurations are welcome so the project can support a wide variety of hardware.

## Preparing the Hardware

1. Flash Raspberry Pi OS 64-bit to the microSD card using Raspberry Pi Imager.
2. Boot the first Pi with the SD card inserted and login via console or SSH.
3. Update firmware:
   ```bash
   sudo apt update && sudo apt full-upgrade
   sudo rpi-eeprom-update -a
   ```
4. Clone the repository and install Docker:
   ```bash
   git clone https://github.com/futuroptimist/token.place.git
   cd token.place
   sudo apt install -y docker.io docker-compose
   sudo usermod -aG docker $USER
   newgrp docker
   ```
5. Copy the OS to the SSD and enable USB/M.2 boot:
   ```bash
   lsblk  # identify your SSD (e.g. /dev/sda)
   git clone https://github.com/billw2/rpi-clone.git
   sudo cp rpi-clone/rpi-clone /usr/local/sbin/
   sudo rpi-clone /dev/sda
   sudo raspi-config  # Advanced Options -> Boot Order -> USB Boot
   sudo poweroff
   ```
6. Remove the SD card and power on. The Pi should boot from the SSD. Repeat for the remaining nodes using the same SD card.

## Running the Relay with Docker Compose

On any single Pi you can run the relay directly:

```bash
docker compose up -d
```

The relay listens on port 5000. To expose it publicly, create a Cloudflare Tunnel:

```bash
sudo apt install -y cloudflared
cloudflared tunnel login
cloudflared tunnel create tokenplace-relay
```

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: TUNNEL_ID
credentials-file: /home/pi/.cloudflared/TUNNEL_ID.json

ingress:
  - hostname: relay.your-domain.com
    service: http://localhost:5000
  - service: http_status:404
```

Run the tunnel:

```bash
cloudflared tunnel run tokenplace-relay
```

## Deploying on a k3s Cluster

After preparing each Pi and verifying SSD boot, install k3s on the control plane node:

```bash
curl -sfL https://get.k3s.io | sh -
```

Retrieve the node token and join worker nodes:

```bash
sudo cat /var/lib/rancher/k3s/server/node-token
curl -sfL https://get.k3s.io | K3S_URL=https://<CONTROL_PLANE_IP>:6443 K3S_TOKEN=<NODE_TOKEN> sh -
```

Build and load the relay image:

```bash
docker build -t tokenplace-relay:latest -f docker/Dockerfile.relay .
k3s ctr images import tokenplace-relay:latest
```

Deploy the manifests:

```bash
kubectl apply -f k8s/
```

## Troubleshooting

### SSD not detected

- Ensure the M.2 drive and ribbon cable are fully seated.
- Check for a SATA controller with `lspci -nn`. You should see a JMicron/ASM chip.
- Trigger a PCIe rescan:
  ```bash
  echo 1 | sudo tee /sys/bus/pci/rescan
  ```
- Examine logs:
  ```bash
  dmesg | grep -i sata
  ```
- Test the SSD with a USB-to-SATA adapter to rule out drive failure.
- Update the Pi firmware with `sudo rpi-eeprom-update -a`.

### Booting from SSD without an SD card

The Pi 5 cannot be flashed directly from Windows. Use one microSD card to install Raspberry Pi OS, then copy to SSD as described above. You can reuse the same SD card for all nodes.

### Power and PoE considerations

If the PoE HAT does not provide enough power for the SSD, ensure you are using a PoE+ switch and that cooling fans are spinning. USB-C power can be used as a fallback.

---

With these steps your Pi cluster should be ready to run token.place. If you encounter issues or use different hardware, please open an issue or contribution so we can expand this guide.

