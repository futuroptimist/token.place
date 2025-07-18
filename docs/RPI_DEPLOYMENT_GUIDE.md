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
   lsblk  # identify your SSD (e.g. /dev/nvme0n1 or /dev/sda)
   cd ..  # leave the token.place directory so rpi-clone isn't cloned inside it
   git clone https://github.com/geerlingguy/rpi-clone.git        # fetch the NVMe-aware cloning utility
   sudo cp rpi-clone/rpi-clone /usr/local/sbin/                  # install rpi-clone
   sudo rpi-clone nvme0n1                                        # copy the running OS to the SSD
   # rsync warnings about chown on vfat are normal
   sudo raspi-config  # Advanced Options -> Boot Order -> B1 (SD -> NVMe -> USB)
   sudo reboot                                               # restart to verify the clone
   ```
   After logging back in, confirm the SSD is mounted:
   ```bash
   lsblk -o NAME,FSTYPE,SIZE,MOUNTPOINTS
   ```
   If `/mnt/clone` is missing from the `MOUNTPOINTS` column, the mount points may not exist yet. Create them and mount the partitions manually:
   ```bash
   sudo mkdir -p /mnt/clone/boot/firmware  # creates /mnt/clone and the boot subdir
   sudo mount /dev/nvme0n1p2 /mnt/clone          # root filesystem
   sudo mount /dev/nvme0n1p1 /mnt/clone/boot/firmware   # FAT /boot
   ```
   Failing to create the directories first results in a “mount point does not exist” error.
   NVMe drives label partitions as `nvme0n1p1`, `nvme0n1p2`, and so on.

### Verify and clean the FAT /boot slice

```bash
# Repeat until no output means the dirty bit is clear
sudo dosfsck -a /dev/nvme0n1p1
sudo fatlabel /dev/nvme0n1p1 BOOT      # give it a label the firmware can mount
```

Make sure `cmdline.txt` and `fstab` reference the same `LABEL=BOOT` or the new PARTUUID.
The `rpi-clone` command will ask four questions:
1. When asked to *Initialize and clone to the destination disk*, type `yes`.
2. At the optional rootfs label prompt, press Enter (or provide a custom label).
3. When prompted to *Run setup script (no)*, just press Enter.
4. For *Verbose mode (no)*, press Enter.
You can skip these questions on later runs with `sudo rpi-clone -u nvme0n1`.

### Verify the clone _before_ removing the SD
1. `ls /mnt/clone/boot/firmware | head` – ensure you see `config.txt` and `start4.elf`.
2. `blkid /dev/nvme0n1p2` → compare PARTUUID with `/mnt/clone/boot/firmware/cmdline.txt`.
3. (Optional) `sudo fatlabel /dev/nvme0n1p1 BOOT` if you use `LABEL=BOOT` mounts.
4. `sudo umount /mnt/clone/boot/firmware /mnt/clone && sync`.

6. Remove the SD card and power on. The Pi should boot from the SSD.

### Setting up additional nodes

Use the very same microSD card for each of the remaining Pis. Because the OS is
mutable, any changes you made while configuring the first node (package installs,
Docker group membership, etc.) will still be present when you insert the card
into another board.

1. Boot the next Pi from the shared SD card and log in.
2. Update its EEPROM with `sudo rpi-eeprom-update -a` – this must be run on each
   board individually.
   If your other Pis or PoE HATs haven't arrived yet, you can still prepare
   their drives by temporarily swapping each SSD into the first board's
   M.2 (or USB) slot.  Power the Pi off, insert a blank SSD, boot from the
   shared SD card and run `sudo rpi-clone nvme0n1`.  Repeat this process for
   the remaining drives so that every SSD is ready before you assemble the
   additional nodes. This saves a lot of teardown and reassembly time and lets
   you continue even if some hardware hasn't arrived yet.
3. Clone the running system to that Pi's SSD with `sudo rpi-clone nvme0n1` and
   reboot without the SD card.
4. Repeat for the third node to end up with a three-Pi k3s cluster.

### First boot from the SSD

On the initial boot you may see a scrolling list of `Trying partition`
messages while the bootloader searches the FAT partition and the filesystem
grows to fill the new drive. This **usually finishes within 2–5 minutes** on a
USB‑attached SSD but can take slightly longer on slower media. Subsequent boots
drop back down to around **15–20&nbsp;seconds**.

If you are still staring at `Trying partition` after about **10&nbsp;minutes**,
something likely went wrong:

- Reseat the USB or M.2 cable and make sure the drive has power.
- Boot from the SD card again and run `lsblk` to confirm that `/dev/nvme0n1p1` and
  `/dev/nvme0n1p2` exist.
- Run `sudo fsck -fy /dev/nvme0n1p2` to repair the filesystem if needed.
- As a last resort, re-run `rpi-clone nvme0n1` and verify the clone completed
  without errors.

<details>
<summary>Diagnosing endless <code>mkfs.fat</code> loops</summary>

* `dosfsck -a /dev/nvme0n1p1` – clears a dirty FAT flag.
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

The steps below create a tiny Kubernetes cluster across several Pi boards and expose the relay via a
Cloudflare Tunnel.

### Prerequisites

k3s will not start without memory cgroups enabled. Raspberry Pi OS disables them by default, so update
`cmdline.txt` to enable the necessary controllers before installing k3s. *Bookworm moved
cmdline.txt; edit `/boot/firmware/cmdline.txt` instead of `/boot/cmdline.txt`.*

Use this one-liner:

```bash
FILE=$(test -f /boot/firmware/cmdline.txt && echo /boot/firmware/cmdline.txt || echo /boot/cmdline.txt)
sudo sed -i -e 's/\<cgroup_disable=memory\>//g' \
            -e 's/\<cgroup_enable=cpuset\>//g' \
            -e 's/\<cgroup_memory=1\>//g' \
            -e 's/\<cgroup_enable=memory\>//g' \
            -e 's/$/ cgroup_enable=cpuset cgroup_memory=1 cgroup_enable=memory/' "$FILE"
```

Reboot after editing the file. See the [k3s requirements](https://docs.k3s.io/installation/requirements#control-plane-nodes), [GitHub issue #2067](https://github.com/k3s-io/k3s/issues/2067) and [StackOverflow question 74294548](https://stackoverflow.com/questions/74294548) for background.

1. **Set a unique hostname for each node**

   The k3s installer uses the current hostname as the node name. Pick names ahead of time so you can
   easily identify the control plane and workers. For example:

   ```bash
   sudo hostnamectl set-hostname controlplane0  # on the first Pi
   sudo reboot
   ```

   On the remaining Pis:

   ```bash
   sudo hostnamectl set-hostname workernode0    # second Pi
   sudo reboot

   sudo hostnamectl set-hostname workernode1    # third Pi
   sudo reboot
   ```

   Rebooting ensures the new hostname propagates everywhere before installing k3s.

2. **Install k3s on the control-plane node**

   ```bash
   curl -sfL https://get.k3s.io | sh -
   ```

   The command installs the `k3s` service and starts the API server on port 6443. After it finishes,
   retrieve the join token:

   ```bash
   sudo cat /var/lib/rancher/k3s/server/node-token
   ```

3. **Join additional Pi nodes as agents**

   Run the installer on each extra Pi, pointing it at the control plane:

   ```bash
   curl -sfL https://get.k3s.io | \
     K3S_URL=https://<CONTROL_PLANE_IP>:6443 \
     K3S_TOKEN=<NODE_TOKEN> sh -
   ```

   Confirm all nodes appear:

   ```bash
   sudo kubectl get nodes -o wide
   ```

4. **Build the relay container and load it into the cluster**

   ```bash
   docker build -t tokenplace-relay:latest -f docker/Dockerfile.relay .
   sudo k3s ctr images import tokenplace-relay:latest
   ```

5. **Deploy the Kubernetes manifests**

   ```bash
   kubectl create namespace tokenplace
   kubectl -n tokenplace apply -f k8s/
   ```

6. **Expose the relay service**

   Patch the service to type `NodePort` so `cloudflared` can reach it:

   ```bash
   kubectl -n tokenplace patch svc tokenplace-relay \
     -p '{"spec": {"type": "NodePort", "ports": [{"port": 5000, "nodePort": 30500}]}}'
   kubectl -n tokenplace get svc tokenplace-relay
   ```

7. **Create a Cloudflare Tunnel to the NodePort**

   On the control-plane node:

   ```bash
   sudo apt install -y cloudflared
   cloudflared tunnel login
   cloudflared tunnel create tokenplace-prod
   ```

   Write `~/.cloudflared/config.yml`:

   ```yaml
   tunnel: TUNNEL_ID
   credentials-file: /home/pi/.cloudflared/TUNNEL_ID.json

   ingress:
     - hostname: relay.your-domain.com
       service: http://localhost:30500
     - service: http_status:404
   ```

   > **Tip:** The `hostname` value can also be the zone's apex domain. Replace
   > `relay.your-domain.com` with your root domain to expose the relay at the
   > base URL:
   >
   > ```yaml
   > ingress:
   >   - hostname: your-domain.com
   >     service: http://localhost:30500
   >   - service: http_status:404
   > ```
   >
   > Cloudflare handles the DNS record via CNAME flattening.

   Start the tunnel and keep it running:

   ```bash
   cloudflared tunnel run tokenplace-prod
   ```

After the tunnel is active, your relay is reachable at `https://relay.your-domain.com` (or `https://your-domain.com` if you used the apex domain) and traffic is
forwarded into the k3s cluster.

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

### Reinstalling k3s

If the install fails or you need to start over, remove all k3s components with:

```bash
sudo /usr/local/bin/k3s-uninstall.sh
```

After the uninstall completes, you can run the installation commands again for a clean deployment.
### ACT LED codes
| LED pattern | Meaning | Fix |
| ----------- | ------- | --- |
| Solid green | /boot not mounted – dirty or wrong label | |
| 4 long + 4 short | `start4.elf` missing – clone aborted | |
| 7 short | kernel img bad – wrong PARTUUID | |

### Recommended USB-to-SATA/NVMe bridges

Adapters using **ASM1153**, **JMS578**, or **JMS583** chipsets reliably support UASP at
full USB 3 speeds. See [Jeff Geerling’s Raspberry Pi 5 NVMe-SSD boot guide](https://www.jeffgeerling.com/blog/2023/nvme-ssd-boot-raspberry-pi-5) for
recommended M.2/NVMe adapters, compatible drives, and the required Pi 5 boot-configuration steps.

### SD vs SSD endurance for k3s

A 300&nbsp;TBW consumer SSD can withstand decades of typical k3s writes
(usually under 100&nbsp;GB per year), while even a "Max Endurance" SD card tops out
around 60&nbsp;TBW. Moving the root filesystem to an SSD greatly reduces wear concerns.
