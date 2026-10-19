#!/bin/bash
# App 端到端检查：临时数据库 + 假大模型起一个服务，无头 Chrome 逐页点一遍（需要本机装了 Google Chrome）
# 运行：bash tests/app_e2e/run.sh   （用装了服务端依赖的 Python，如 conda 的 xiaozhi 环境）
cd "$(dirname "$0")"
PY=${PY:-python}
LOG=$(mktemp)
$PY server.py 18013 > "$LOG" 2>&1 & SP=$!
for i in $(seq 1 60); do grep -q ready "$LOG" && break; sleep 0.25; done
$PY check.py 18013; RC=$?
kill $SP
exit $RC
