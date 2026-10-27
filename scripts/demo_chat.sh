#!/usr/bin/env bash
set -euo pipefail
: "${COMMERCEFLOW_ACCESS_TOKEN:?请先登录并设置 COMMERCEFLOW_ACCESS_TOKEN}"
BASE=${COMMERCEFLOW_BASE_URL:-http://localhost:8000}
FRAME_FILE=$(mktemp)
trap 'rm -f "$FRAME_FILE"' EXIT
echo "=== 第一轮 ==="
curl -fsSN "$BASE/api/chat" -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $COMMERCEFLOW_ACCESS_TOKEN" \
  -d '{"message":"我叫王小明，昨天买了你们的智能猫砂盆"}' | tee "$FRAME_FILE"
CID=$(python3 - "$FRAME_FILE" <<'PY'
import json, sys
for line in open(sys.argv[1], encoding="utf-8"):
    if line.startswith("data: ") and line.strip() != "data: [DONE]":
        value = json.loads(line[6:])
        if value.get("event") == "done":
            print(int(value["conversation_id"]))
            break
else:
    raise SystemExit("第一轮没有返回会话，停止续接")
PY
)
echo "=== 第二轮 ==="
PAYLOAD=$(python3 -c 'import json,sys; print(json.dumps({"conversation_id":int(sys.argv[1]), "message":"还记得我叫什么、买了什么吗？"}))' "$CID")
curl -fsSN "$BASE/api/chat" -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $COMMERCEFLOW_ACCESS_TOKEN" -d "$PAYLOAD"
