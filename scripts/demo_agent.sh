#!/usr/bin/env bash
set -euo pipefail
: "${COMMERCEFLOW_ACCESS_TOKEN:?请先登录并设置 COMMERCEFLOW_ACCESS_TOKEN}"
BASE=${COMMERCEFLOW_BASE_URL:-http://localhost:8000}
ORDER_ID=${COMMERCEFLOW_ORDER_ID:-1001} # 必须属于该登录账户
for MESSAGE in "订单 $ORDER_ID 的物流到哪了" "退货政策是什么" "邮费是多少"; do
  PAYLOAD=$(python3 -c 'import json,sys; print(json.dumps({"message":sys.argv[1]}))' "$MESSAGE")
  curl -fsS "$BASE/api/agent" -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $COMMERCEFLOW_ACCESS_TOKEN" -d "$PAYLOAD" | python3 -m json.tool
done
