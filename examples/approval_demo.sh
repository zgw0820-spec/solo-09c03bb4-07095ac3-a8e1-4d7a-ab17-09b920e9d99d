#!/usr/bin/env bash
# 磁带外送审批全流程演示（服务需已在 BASE 上运行，默认 http://localhost:8000）。
#
#   docker compose up --build -d
#   bash examples/approval_demo.sh
#
# 流程：登记目录与灾备装带上限 → 查询在库目录 → 申请外送 T3（批准）→
#       查询结果直接提交 /schedule → 再申请外送 T2（拒绝，目录不变）。
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

step() { printf '\n=== %s ===\n' "$1"; }

step "1. PUT /library  登记在库目录与灾备装带次数上限（上限=2）"
curl -s -X PUT "$BASE/library" \
  -H 'Content-Type: application/json' \
  --data-binary @"$HERE/library_register.json"

step "2. GET /library  查询当前在库目录（可直接提交 /schedule）"
curl -s "$BASE/library" | tee /tmp/library_catalog.json

step "3. POST /schedule  用查询结果直接排程"
curl -s -X POST "$BASE/schedule" \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/library_catalog.json

step "4. POST /library/approvals  申请外送 T3（应批准并落盘）"
curl -s -X POST "$BASE/library/approvals" \
  -H 'Content-Type: application/json' \
  --data-binary @"$HERE/approval_request.json"

step "5. GET /library/status  在库概览与已外送记录"
curl -s "$BASE/library/status"

step "6. POST /library/approvals  再申请外送 T2（应拒绝：T1 再坏则无解，目录不变）"
curl -s -X POST "$BASE/library/approvals" \
  -H 'Content-Type: application/json' \
  -d '{"tape": "T2"}'

step "7. GET /library  拒绝后目录不变（仍含 T1、T2）"
curl -s "$BASE/library"

printf '\n提示：docker compose restart api 后再查 GET /library/status，'
printf '已外送的 T3 不会被算回来（状态在命名卷 library-data 里）。\n'
