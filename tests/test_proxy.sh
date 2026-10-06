#!/usr/bin/env bash
# Checks the nginx site + /proxy/ allowlist against a running container:
#   docker run -d -p 18080:8080 odz-lite && tests/test_proxy.sh http://localhost:18080
set -u
BASE=${1:-http://localhost:18080}
DL=/proxy/data.stadt-zuerich.ch/dataset/politik_abstimmungen_seit1933/download/abstimmungen_seit1933
failures=0

expect() {  # expect <status> <description> <curl args...>
  local want=$1 what=$2; shift 2
  local got
  got=$(curl -s -o /dev/null -w "%{http_code}" "$@")
  if [ "$got" = "$want" ]; then echo "ok   $got $what"; else echo "FAIL $got (want $want) $what"; failures=$((failures + 1)); fi
}

expect 200 "health" "$BASE/healthz"
expect 302 "root redirects to lab" "$BASE/?package_id=x"
expect 200 "lab" "$BASE/lab/index.html"
expect 200 "template listing" "$BASE/api/contents/templates/all.json"
expect 200 "piplite index" "$BASE/pypi/all.json"
# Wheels preloaded by the kernel (jupyter-lite.json) must exist under these exact names.
for wheel in $(curl -s "$BASE/jupyter-lite.json" | grep -o '"/pypi/[^"]*\.whl"' | tr -d '"'); do
  expect 200 "preloaded wheel $wheel" "$BASE$wheel"
done
# Datopian rejects any Origin; the proxy must strip it and follow 3 redirects.
expect 200 "csv download via proxy (with Origin)" -H "Origin: https://example.org" "$BASE$DL.csv"
expect 200 "parquet download via proxy" "$BASE$DL.parquet"
expect 404 "missing resource" "$BASE/proxy/data.stadt-zuerich.ch/dataset/nope/download/nope.csv"
expect 403 "other host" "$BASE/proxy/example.org/x"
expect 403 "non-download path" "$BASE/proxy/data.stadt-zuerich.ch/api/3/action/package_show"
expect 403 "host suffix trick" "$BASE/proxy/data.stadt-zuerich.ch.evil.org/dataset/x/download/y.csv"
expect 403 "userinfo trick" "$BASE/proxy/data.stadt-zuerich.ch@evil.org/dataset/x/download/y.csv"
expect 403 "POST" -X POST "$BASE$DL.csv"

size=$(curl -s "$BASE$DL.csv" | wc -c | tr -d ' ')
if [ "$size" -gt 1000000 ]; then echo "ok   full body ($size bytes)"; else echo "FAIL body $size bytes"; failures=$((failures + 1)); fi

exit $failures
