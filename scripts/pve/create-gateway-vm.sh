#!/usr/bin/env bash
# Run ON a PVE node as root. Creates the long-lived VM that hosts VMGateway itself
# (NOT in pool vmgw, so the gateway never treats it as a lease).
#   ssh root@pve 'VMID=150 NODE=pve1 TEMPLATE=9024 bash -s' < scripts/pve/create-gateway-vm.sh < ~/.ssh/id.pub
# stdin after the script = SSH public key(s) to authorize for user "vmgw".
set -euo pipefail

VMID=${VMID:-150}
NODE=${NODE:-$(hostname)}
TEMPLATE=${TEMPLATE:-9024}
NAME=${NAME:-vmgateway}
STORAGE=${STORAGE:-local-lvm}

KEYFILE=$(mktemp)
cat > "$KEYFILE"
[ -s "$KEYFILE" ] || { echo "pass an SSH public key on stdin" >&2; exit 1; }

if pvesh get /cluster/resources --type vm --output-format json | grep -q "\"vmid\":$VMID,"; then
  echo "VM $VMID already exists" >&2; exit 1
fi

pvesh create /nodes/$NODE/qemu/$TEMPLATE/clone --newid $VMID --name $NAME --full 1 --storage $STORAGE >/dev/null
for i in $(seq 1 120); do
  pvesh get /nodes/$NODE/qemu/$VMID/config --output-format json | grep -q '"lock"' || break
  sleep 2
done
pvesh set /nodes/$NODE/qemu/$VMID/config --cores 2 --memory 2048 --ciuser vmgw --ipconfig0 ip=dhcp \
  --sshkeys "$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(open(sys.argv[1]).read().strip(),safe=""))' "$KEYFILE")" \
  --onboot 1 --description "VMGateway service (do not delete)" >/dev/null
pvesh set /nodes/$NODE/qemu/$VMID/resize --disk scsi0 --size 16G >/dev/null
pvesh create /nodes/$NODE/qemu/$VMID/status/start >/dev/null
rm -f "$KEYFILE"

for i in $(seq 1 60); do
  ip=$(pvesh get /nodes/$NODE/qemu/$VMID/agent/network-get-interfaces --output-format json 2>/dev/null \
       | python3 -c 'import json,sys
d=json.load(sys.stdin)
for i in d.get("result",[]):
    if i.get("name")=="lo": continue
    for a in i.get("ip-addresses",[]):
        if a.get("ip-address-type")=="ipv4" and not a["ip-address"].startswith("169.254"): print(a["ip-address"]); raise SystemExit' 2>/dev/null || true)
  [ -n "$ip" ] && { echo "IP=$ip"; exit 0; }
  sleep 3
done
echo "started, but no IP yet" >&2
