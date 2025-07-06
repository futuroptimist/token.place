# Raspberry Pi Deployment Guide

This guide combines the runbook, k3s cluster instructions and bill of materials into a single place.
It documents how we built a three-node Raspberry Pi 5 cluster for token.place
 and captures some lessons learned along the way.

## Bill of Materials

- **Raspberry Pi 5** boards (4GB or 8GB RAM)
- **PoE+ HAT with M.2 slot** (2230/2242) for each Pi. Use an NVMe-capable HAT if you want PCIe speeds for the SSD.
- **256GB M.2 SATA or NVMe SSD** (TS256GMTS430S or similar) per Pi
- **64GB microSD card** (one card can be reused for all nodes)
- **PoE+ network switch** and **Ethernet cables**
- **Cooling solution** such as a fan case or heatsink
- **Optional: USB-C power supply** if you are not using PoE
- **Cloudflare account** with a registered domain for tunneling

This list reflects our setup. Other hardware choices will also work.
Contributions describing different configurations are welcome so the project can support a wide variety of hardware.

## Preparing the Hardware

1. Flash Raspberry Pi OS 64-bit to the microSD card using Raspberry Pi Imager.
2. Boot the first Pi with the SD card inserted and login via console or SSH.
3. Update firmware:
   ```bash
   sudo apt update && sudo apt full-upgrade  # refresh package list and upgrade the OS
   sudo rpi-eeprom-update -a                 # install the latest firmware
   ```
4. Clone the repository and install Docker:
   ```bash
   git clone https://github.com/futuroptimist/token.place.git  # download the project
   cd token.place                                             # enter the project folder
   sudo apt install -y docker.io docker-compose               # install Docker runtime
   sudo usermod -aG docker $USER                              # allow the current user to run Docker
   newgrp docker                                              # apply the new group membership
   ```
5. Copy the OS to the SSD and enable USB/M.2 boot:
   ```bash
   lsblk  # identify your SSD (e.g. /dev/sda)
   cd ..  # leave the token.place directory so rpi-clone isn't cloned inside it
   git clone https://github.com/billw2/rpi-clone.git            # fetch the cloning utility
   sudo cp rpi-clone/rpi-clone /usr/local/sbin/                 # install rpi-clone
   sudo rpi-clone /dev/sda                                      # copy the running OS to the SSD
   # rsync warnings about chown on vfat are normal
   sudo raspi-config  # Advanced Options -> Boot Order -> USB Boot
   sudo poweroff                                             # shut down to switch boot devices
   ```

### Verify and clean the FAT /boot slice

```bash
# Repeat until no output means the dirty bit is clear
sudo dosfsck -a /dev/sda1
fatlabel /dev/sda1 BOOT      # give it a label the firmware can mount
```

Make sure `cmdline.txt` and `fstab` reference the same `LABEL=BOOT` or the new PARTUUID.
The `rpi-clone` command will ask four questions:
1. When asked to *Initialize and clone to the destination disk*, type `yes`.
2. At the optional rootfs label prompt, press Enter (or provide a custom label).
3. When prompted to *Run setup script (no)*, just press Enter.
4. For *Verbose mode (no)*, press Enter.
You can skip these questions on later runs with `sudo rpi-clone -u /dev/sda`.

### Verify the clone _before_ removing the SD
1. `ls /mnt/clone/boot/firmware | head` – ensure you see `config.txt` and `start4.elf`.
2. `blkid /dev/sda2` → compare PARTUUID with `/mnt/clone/boot/firmware/cmdline.txt`.
3. (Optional) `fatlabel /dev/sda1 BOOT` if you use `LABEL=BOOT` mounts.
4. `sudo umount /mnt/clone/boot/firmware /mnt/clone && sync`.

6. Remove the SD card and power on. The Pi should boot from the SSD.
Repeat for the remaining nodes using the same SD card.

### First boot from the SSD

On the initial boot you may see a scrolling list of `Trying partition`
messages while the bootloader searches the FAT partition and the filesystem
grows to fill the new drive. This **usually finishes within 2–5 minutes** on a
USB‑attached SSD but can take slightly longer on slower media. Subsequent boots
drop back down to around **15–20&nbsp;seconds**.

If you are still staring at `Trying partition` after about **10&nbsp;minutes**,
something likely went wrong:

- Reseat the USB or M.2 cable and make sure the drive has power.
- Boot from the SD card again and run `lsblk` to confirm that `/dev/sda1` and
  `/dev/sda2` exist.
- Run `sudo fsck -fy /dev/sda2` to repair the filesystem if needed.
- As a last resort, re-run `rpi-clone /dev/sda` and verify the clone completed
  without errors.

<details>
<summary>Diagnosing endless <code>mkfs.fat</code> loops</summary>

* `dosfsck -a /dev/sda1` – clears a dirty FAT flag.
* Solid green LED + 4-long/4-short blinks → `/boot` missing, reclone.
* Solid green, no blinks, but SSD LED blinking → check BOOT_ORDER (`0xf416`) and `dtparam=nvme`.

</details>

Once the boot messages stop, you should get a normal login prompt on the
console.

### EEPROM & boot-order quick-check

```bash
sudo rpi-eeprom-update -a
vcgencmd bootloader_config | grep BOOT_ORDER
```

### Quick boot-loader sanity check

```bash
vcgencmd bootloader_config | grep BOOT_ORDER  # expect 0xf146 (USB→NVMe) or 0xf416
```

If it starts with `0x1`, the Pi will try an empty SD slot first.

### Moving the SSD to the M.2 slot

USB 3.0 on the Pi 5 typically tops out around **350–400 MB/s**.
. The PoE+ HAT connects over a PCIe ×1 lane and can run in Gen3 mode,
 reaching roughly **900 MB/s** with a capable NVMe drive—more than twice the throughput of USB.

| Storage option                | Interface     | Typical throughput |
| ----------------------------- | ------------- | ------------------ |
| Fast microSD (UHS‑I)          | SD card       | ~45&nbsp;MB/s       |
| M.2 drive over USB 3.0        | USB           | 350–400&nbsp;MB/s   |
| M.2 drive via PCIe Gen2 ×1    | PCIe Gen2 ×1  | ~500&nbsp;MB/s      |
| M.2 drive via PCIe Gen3 ×1    | PCIe Gen3 ×1  | ~900&nbsp;MB/s      |

1. Boot from the SSD over USB as described above.
2. Mount the boot partition and add the following to `/boot/config.txt`:
   ```ini
   dtparam=nvme
   # Optional: force PCIe Gen3 speeds
   dtparam=pciex1_gen=3
   ```
   Optionally run `sudo rpi-eeprom-config --edit` and ensure `PCIE_PROBE=1` is present.
   Forcing Gen3 avoids link-down errors on cold boot with some HAT cables.
   3. Power down, move the SSD into the PoE HAT’s M.2 slot and boot again.

Because the EEPROM’s “USB boot” setting also covers NVMe devices,
 the Pi will continue to boot from this drive without further changes.

## Running the Relay with Docker Compose

On any single Pi you can run the relay directly:

```bash
docker compose up -d  # start the relay container in the background
```

The relay listens on port 5000. To expose it publicly, create a Cloudflare Tunnel:

```bash
sudo apt install -y cloudflared               # install Cloudflare Tunnel client
cloudflared tunnel login                      # authenticate your account
cloudflared tunnel create tokenplace-relay    # create a tunnel named tokenplace-relay
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
cloudflared tunnel run tokenplace-relay  # run the tunnel using the above config
```

## Deploying on a k3s Cluster

With each Pi booting from its SSD, you can stitch them together into a lightweight Kubernetes cluster
 using [k3s](https://k3s.io). Pick one Pi to be the control plane ("server") and run:

```bash
curl -sfL https://get.k3s.io | sh -  # install k3s on the control-plane node
```

This installs the `k3s` service and starts the Kubernetes API. Grab the join token:

```bash
sudo cat /var/lib/rancher/k3s/server/node-token  # display the join token
```

On each remaining Pi (the "agents"), join the cluster by pointing the install script
 at the control plane's IP address and providing the token:

```bash
curl -sfL https://get.k3s.io |
 K3S_URL=https://<CONTROL_PLANE_IP>:6443 K3S_TOKEN=<NODE_TOKEN> sh -  # join an agent to the cluster
```

Once all nodes show up in `kubectl get nodes`, build the relay container image
 and load it into k3s' internal container registry:

```bash
docker build -t tokenplace-relay:latest -f docker/Dockerfile.relay .  # build relay image
k3s ctr images import tokenplace-relay:latest                          # load image into k3s
```

Apply the Kubernetes manifests to deploy the relay and any supporting services:

```bash
kubectl apply -f k8s/  # deploy Kubernetes manifests
```

## Troubleshooting

### SSD not detected

- Ensure the M.2 drive and ribbon cable are fully seated.
- Check for a SATA controller with `lspci -nn`. You should see a JMicron/ASM chip.
- Trigger a PCIe rescan:
  ```bash
  echo 1 | sudo tee /sys/bus/pci/rescan  # force the kernel to rescan PCIe
  ```
- Examine logs:
  ```bash
  dmesg | grep -i sata  # look for SATA-related errors
  ```
- Test the SSD with a USB-to-SATA adapter to rule out drive failure.
- Update the Pi firmware with `sudo rpi-eeprom-update -a`.

### Booting from SSD without an SD card

Regardless of whether you use Windows, macOS, or Linux to prepare the microSD card,
 the Raspberry Pi 5 currently requires an SD-based install before it can boot from USB or M.2.
 Flash Raspberry Pi OS to a single microSD card,
boot each Pi once, copy the OS to the SSD, then remove the card. The same card can be reused for every node.

### HDMI and JetKVM capture mode
Add to **/boot/firmware/config.txt** on the SSD:

```ini
hdmi_force_hotplug=1
hdmi_group=1  # CEA
hdmi_mode=1   # 640x480@60 Hz – works on every KVM/capture
config_hdmi_boost=7
```

### Power and PoE considerations

* Use the 27 W PSU for cloning and the first NVMe boot
* Verify your switch reports **802.3at 30 W (Class 4)** before relying on PoE+

---

With these steps your Pi cluster should be ready to run token.place.
If you encounter issues or use different hardware, please open an issue or contribution so we can expand this guide.
### ACT LED codes
| LED pattern | Meaning | Fix |
| ----------- | ------- | --- |
| Solid green | /boot not mounted – dirty or wrong label | |
| 4 long + 4 short | `start4.elf` missing – clone aborted | |
| 7 short | kernel img bad – wrong PARTUUID | |

### Recommended USB-to-SATA/NVMe bridges

Adapters using **ASM1153**, **JMS578**, or **JMS583** chipsets reliably support UASP at
full USB 3 speeds. See [James Chambers' compatibility list](https://jamesachambers.com/raspberry-pi-storage-adapter-compatibility/) for
tested enclosures.

### SD vs SSD endurance for k3s

A 300&nbsp;TBW consumer SSD can withstand decades of typical k3s writes
(usually under 100&nbsp;GB per year), while even a "Max Endurance" SD card tops out
around 60&nbsp;TBW. Moving the root filesystem to an SSD greatly reduces wear concerns.
