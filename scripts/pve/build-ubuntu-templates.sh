#!/usr/bin/env bash
# Run ON a PVE node as root. Builds Ubuntu cloud-init templates (with qemu-guest-agent baked in)
# on every node listed, and puts them in pool vmgw-templates.
#   ssh root@pve 'SHARED=nas TEMPLATES="9024:pve1:24.04:noble 9124:pve2:24.04:noble" bash -s' \
#       < scripts/pve/build-ubuntu-templates.sh
# One template per node when disks live on node-local storage (linked clones need the template locally).
set -euo pipefail

SHARED=${SHARED:-shared}              # shared storage (dir/NFS/CIFS) that holds images + snippets
SHARED_PATH=/mnt/pve/$SHARED
DISK_STORAGE=${DISK_STORAGE:-local-lvm}
BRIDGE=${BRIDGE:-vmbr0}
POOL=${POOL:-vmgw-templates}
# vmid:node:release:codename
TEMPLATES=${TEMPLATES:-"9024:$(hostname):24.04:noble 9022:$(hostname):22.04:jammy"}

mkdir -p "$SHARED_PATH/snippets"
cat > "$SHARED_PATH/snippets/vmgw-ubuntu-prep.yaml" <<'EOF'
#cloud-config
package_update: true
packages: [qemu-guest-agent]
runcmd:
  - systemctl enable --now qemu-guest-agent
EOF

for codename in noble jammy; do
  img="$codename-server-cloudimg-amd64.img"
  if [ ! -f "$SHARED_PATH/template/iso/$img" ]; then
    echo "downloading $img"
    pvesh create /nodes/$(hostname)/storage/$SHARED/download-url --content iso --filename "$img" \
      --url "https://cloud-images.ubuntu.com/$codename/current/$img" >/dev/null
  fi
done

build() {
  local id=$1 node=$2 rel=$3 codename=$4
  local img="$SHARED_PATH/template/iso/$codename-server-cloudimg-amd64.img"
  if pvesh get /cluster/resources --type vm --output-format json | grep -q "\"vmid\":$id,"; then
    echo "[$id] exists, skipping"; return
  fi
  echo "[$id] create on $node"
  pvesh create /nodes/$node/qemu --vmid $id --name "ubuntu-$rel-cloud" --ostype l26 \
    --memory 2048 --cores 2 --cpu host --scsihw virtio-scsi-single --agent enabled=1 \
    --net0 "virtio,bridge=$BRIDGE" \
    --scsi0 "$DISK_STORAGE:0,import-from=$img,discard=on,iothread=1,ssd=1" \
    --ide2 "$DISK_STORAGE:cloudinit" --boot order=scsi0 \
    --ciuser vmgwprep --ipconfig0 ip=dhcp \
    --cicustom "vendor=$SHARED:snippets/vmgw-ubuntu-prep.yaml" >/dev/null
  pvesh set /nodes/$node/qemu/$id/resize --disk scsi0 --size 10G >/dev/null
  pvesh create /nodes/$node/qemu/$id/status/start >/dev/null
  echo "[$id] waiting for guest agent"
  for i in $(seq 1 90); do
    pvesh create /nodes/$node/qemu/$id/agent/ping >/dev/null 2>&1 && break
    sleep 5
    [ $i -eq 90 ] && { echo "[$id] agent timeout"; return 1; }
  done
  echo "[$id] agent up, sealing"
  pvesh create /nodes/$node/qemu/$id/agent/exec --command sh --command -c \
    --command "apt-get clean; cloud-init clean --logs --machine-id; rm -f /etc/ssh/ssh_host_*; sync" >/dev/null
  sleep 15
  pvesh create /nodes/$node/qemu/$id/status/shutdown --timeout 120 >/dev/null || true
  for i in $(seq 1 30); do
    pvesh get /nodes/$node/qemu/$id/status/current --output-format json | grep -q '"status":"stopped"' && break
    sleep 4
  done
  pvesh set /nodes/$node/qemu/$id/config --delete cicustom,ciuser >/dev/null
  pvesh create /nodes/$node/qemu/$id/template >/dev/null
  pvesh set /pools/$POOL --vms $id >/dev/null 2>&1 || pveum pool modify $POOL --vms $id
  echo "[$id] template ready"
}

pids=()
for t in $TEMPLATES; do
  IFS=: read -r id node rel codename <<<"$t"
  build "$id" "$node" "$rel" "$codename" &
  pids+=($!)
done
rc=0; for p in "${pids[@]}"; do wait "$p" || rc=1; done
exit $rc
