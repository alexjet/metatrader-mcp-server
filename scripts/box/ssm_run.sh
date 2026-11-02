#!/usr/bin/env bash
# Run a PowerShell payload on the Windows MT5 box through SSM Run Command and print its output.
#
#   bash scripts/box/ssm_run.sh <payload.ps1>
#
# Run it from YOUR terminal: the agent's policy blocks SSM send-command. Needs AWS
# credentials for the deploy account (`aws login` first). The box has no git, so code is
# delivered as self-verifying payloads built by scripts/box/gen_payloads.py.
set -euo pipefail

PAYLOAD="${1:?usage: ssm_run.sh <payload.ps1>}"
INSTANCE="${MT5_INSTANCE_ID:-i-0116064e1290d8154}"
REGION="${MT_TUNNEL_REGION:-eu-central-1}"
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

[ -f "$PAYLOAD" ] || { echo "no such payload: $PAYLOAD" >&2; exit 1; }

PARAMS="$(mktemp)"
trap 'rm -f "$PARAMS"' EXIT
python3 -c 'import json,sys; print(json.dumps({"commands": open(sys.argv[1], encoding="utf-8").read().splitlines()}))' \
  "$PAYLOAD" >"$PARAMS"

CMD_ID="$(aws ssm send-command \
  --region "$REGION" \
  --instance-ids "$INSTANCE" \
  --document-name AWS-RunPowerShellScript \
  --comment "$(basename "$PAYLOAD")" \
  --parameters "file://$PARAMS" \
  --query Command.CommandId --output text)"
echo "command $CMD_ID sent ($(basename "$PAYLOAD")), waiting..." >&2

for _ in $(seq 1 60); do
  STATUS="$(aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" \
    --instance-id "$INSTANCE" --query Status --output text 2>/dev/null || echo Pending)"
  case "$STATUS" in Pending|InProgress|Delayed) sleep 3 ;; *) break ;; esac
done

echo "status: $STATUS"
echo "===== STDOUT ====="
aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" \
  --instance-id "$INSTANCE" --query StandardOutputContent --output text
echo "===== STDERR ====="
aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" \
  --instance-id "$INSTANCE" --query StandardErrorContent --output text
