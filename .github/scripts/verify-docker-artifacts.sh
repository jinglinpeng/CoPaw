#!/usr/bin/env bash
set -euo pipefail

image="qwenpaw-verify:$ARCH"
report="$RUNNER_TEMP/docker-artifacts"
actual=$(docker image inspect "$image" --format '{{.Architecture}}')
test "$actual" = "$ARCH"
actual=$(docker image inspect "$image" \
  --format '{{ index .Config.Labels "io.qwenpaw.managed-runtime-boundary.version" }}')
test "$actual" = "$VERSION"
mkdir -p "$RUNNER_TEMP/working" "$RUNNER_TEMP/working.secret" "$RUNNER_TEMP/working.backups"
docker run -d --name qwenpaw-artifact-test --platform "linux/$ARCH" \
  -p 127.0.0.1:18088:8088 \
  -v "$RUNNER_TEMP/working:/app/working" \
  -v "$RUNNER_TEMP/working.secret:/app/working.secret" \
  -v "$RUNNER_TEMP/working.backups:/app/working.backups" "$image"
ready=false
for i in $(seq 1 120); do
  if curl -sf http://127.0.0.1:18088/api/version > "$report/version.json"; then
    ready=true
    break
  fi
  sleep 2
done
test "$ready" = true
python3 -c 'import json, os; assert json.load(open(os.environ["RUNNER_TEMP"] + "/docker-artifacts/version.json"))["version"] == os.environ["VERSION"]'
docker cp .github/scripts/docker-native-probe.py qwenpaw-artifact-test:/tmp/native_probe.py
docker exec qwenpaw-artifact-test /app/venv/bin/python /tmp/native_probe.py | tee "$report/runtime.json"
python3 -c 'import json,os; x=json.load(open(os.environ["RUNNER_TEMP"] + "/docker-artifacts/runtime.json")); assert x["machine"] == {"amd64":"x86_64", "arm64":"aarch64"}[os.environ["ARCH"]]; assert x["python"].startswith("3.11."); assert x["openssl"].startswith("OpenSSL 3.5.")'
shot=$(python3 -c 'import json,os; print(json.load(open(os.environ["RUNNER_TEMP"] + "/docker-artifacts/runtime.json"))["functional_probes"]["browser_screenshot"])')
docker cp "qwenpaw-artifact-test:$shot" "$report/browser.png"
docker exec qwenpaw-artifact-test /app/venv/bin/python -m pip check | tee "$report/pip-check.log"
docker inspect qwenpaw-artifact-test > "$report/container-inspect.json"
