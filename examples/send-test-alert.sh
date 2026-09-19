#!/usr/bin/env sh
# Send a sample Alertmanager payload to the workflow's webhook.
#
#   FTW_WEBHOOK_SECRET=... ./examples/send-test-alert.sh
#   FTW_WEBHOOK_SECRET=... ./examples/send-test-alert.sh https://n8n.example.com examples/samples/alertmanager-resolved.json
set -eu
BASE="${1:-http://localhost:5678}"
PAYLOAD="${2:-$(dirname "$0")/samples/alertmanager-firing.json}"

if [ -z "${FTW_WEBHOOK_SECRET:-}" ]; then
  echo "set FTW_WEBHOOK_SECRET to the value stored in the n8n credential (without 'Bearer ')" >&2
  exit 1
fi

echo "POST $BASE/webhook/ftw-alertmanager-triage  <- $PAYLOAD"
curl -sS -X POST "$BASE/webhook/ftw-alertmanager-triage" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $FTW_WEBHOOK_SECRET" \
  --data-binary "@$PAYLOAD"
echo
echo "Now open n8n -> Executions. The chat message follows within a few seconds."
