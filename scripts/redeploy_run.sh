#!/bin/bash
# Redeploy one completed run's Terraform onto a freshly reset LocalStack.
#
# Usage: ./scripts/redeploy_run.sh <run_id>
#
# Copies runs/<run_id>/infra to a temporary directory (the stored run is
# never modified), resets the emulator, and applies the Terraform there.
# Afterwards the deployment is live for:
#   .venv/bin/python scripts/app_probe.py <run_id>            (probe all endpoints)
#   .venv/bin/python scripts/app_probe.py <run_id> --serve    (keep the app up)
#   .venv/bin/python scripts/behavioral_checks.py <scenario>  (behavioural rules)
set -euo pipefail
RUN_ID="${1:?usage: redeploy_run.sh <run_id>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/runs/$RUN_ID/infra"
[ -d "$SRC" ] || { echo "no infra directory for run $RUN_ID"; exit 1; }

"$ROOT/scripts/reset_localstack.sh" --confirm >/dev/null
echo "emulator reset"

WORK="$(mktemp -d /tmp/cloudforge-redeploy-XXXXXX)"
cp -R "$SRC/." "$WORK/"
rm -rf "$WORK/.terraform" "$WORK/terraform.tfstate" "$WORK/.terraform.lock.hcl"

export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
export TF_PLUGIN_CACHE_DIR="$ROOT/.tfcache"
mkdir -p "$TF_PLUGIN_CACHE_DIR"
"$ROOT/.venv/bin/tflocal" -chdir="$WORK" init -input=false -no-color >/dev/null
"$ROOT/.venv/bin/tflocal" -chdir="$WORK" apply -auto-approve -input=false -no-color 2>&1 | grep -E "Apply complete|Error" | head -3
echo "deployed $RUN_ID from $WORK"
