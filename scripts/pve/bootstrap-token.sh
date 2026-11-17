#!/usr/bin/env bash
# Run ON a PVE node as root: creates the least-privilege user/token VMGateway uses.
#   ssh root@pve 'bash -s' < scripts/pve/bootstrap-token.sh
# Prints the token secret ONCE as the last line: TOKEN_SECRET=<uuid>
set -euo pipefail

USER_ID="vmgw@pve"
TOKEN_NAME="gateway"
POOL_VMS="vmgw"
POOL_TPL="vmgw-templates"

ensure_role() {  # name privs
  if pveum role list --output-format json | grep -q "\"roleid\":\"$1\""; then
    pveum role modify "$1" --privs "$2"
  else
    pveum role add "$1" --privs "$2"
  fi
}

pvesh get /pools --output-format json | grep -q "\"poolid\":\"$POOL_VMS\"" || pveum pool add "$POOL_VMS" --comment "VMs leased by VMGateway"
pvesh get /pools --output-format json | grep -q "\"poolid\":\"$POOL_TPL\"" || pveum pool add "$POOL_TPL" --comment "Templates VMGateway may clone"

# Full control over VMs the gateway creates (they always live in pool vmgw).
ensure_role VMGatewayVM "VM.Allocate,VM.Audit,VM.Clone,VM.Config.CDROM,VM.Config.CPU,VM.Config.Cloudinit,VM.Config.Disk,VM.Config.HWType,VM.Config.Memory,VM.Config.Network,VM.Config.Options,VM.Console,VM.GuestAgent.Audit,VM.GuestAgent.Unrestricted,VM.PowerMgmt,Pool.Audit"
# Templates: read + clone only.
ensure_role VMGatewayTemplate "VM.Audit,VM.Clone,Pool.Audit"
# Infra: allocate disk, read node status, attach to bridges.
ensure_role VMGatewayInfra "Datastore.AllocateSpace,Datastore.Audit,Sys.Audit,SDN.Use"

pveum user list --output-format json | grep -q "\"userid\":\"$USER_ID\"" || pveum user add "$USER_ID" --comment "VMGateway service account"

pveum acl modify "/pool/$POOL_VMS" --users "$USER_ID" --roles VMGatewayVM
pveum acl modify "/pool/$POOL_TPL" --users "$USER_ID" --roles VMGatewayTemplate
for p in /storage /nodes /sdn; do
  pveum acl modify "$p" --users "$USER_ID" --roles VMGatewayInfra
done

# Recreate the token so we can print its secret.
pveum user token remove "$USER_ID" "$TOKEN_NAME" 2>/dev/null || true
secret=$(pveum user token add "$USER_ID" "$TOKEN_NAME" --privsep 0 --comment "VMGateway API" --output-format json | sed -n 's/.*"value":"\([^"]*\)".*/\1/p')
echo "TOKEN_SECRET=$secret"
