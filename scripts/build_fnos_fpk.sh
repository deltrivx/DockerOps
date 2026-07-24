#!/usr/bin/env bash
# Build FnOS professional FPK package for DockerOps.
# Output: dist/dockerops-<version>-fnos.fpk
# Format (from real FnOS packages): gzip(tar( app.tgz + cmd/ + config/ + wizard/ + manifest + ICON*.PNG ))
# Note: some third-party FPKs prefix "2000\r\n"; official-style packages observed as pure gzip.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FPK_SRC="${ROOT}/fnos/fpk"
DIST="${ROOT}/dist"
# Sanitize version for filename (strip +sha.xxx etc.)
RAW_VERSION="${DOCKEROPS_VERSION:-0.3.3}"
VERSION="$(printf '%s' "${RAW_VERSION}" | sed -E 's/\+.*//; s/[^A-Za-z0-9._-]+/-/g')"
[[ -n "${VERSION}" ]] || VERSION="0.3.3"
OUT_NAME="dockerops-${VERSION}-fnos.fpk"
STAGE="$(mktemp -d)"
APP_STAGE="$(mktemp -d)"

# Avoid macOS AppleDouble (._*) junk in archives
export COPYFILE_DISABLE=1
export COPY_EXTENDED_ATTRIBUTES_DISABLE=1

cleanup() {
  rm -rf "${STAGE}" "${APP_STAGE}"
}
trap cleanup EXIT

echo "==> Building FnOS FPK ${OUT_NAME}"

# 1) app.tgz: runtime payload installed to TRIM_APPDEST
mkdir -p "${APP_STAGE}/cmd" "${APP_STAGE}/config" "${APP_STAGE}/ui/images" "${APP_STAGE}/wizard"
# rsync-like copy without resource forks
cp -R "${FPK_SRC}/cmd/." "${APP_STAGE}/cmd/"
cp -R "${FPK_SRC}/config/." "${APP_STAGE}/config/"
cp -R "${FPK_SRC}/ui/." "${APP_STAGE}/ui/"
cp -R "${FPK_SRC}/wizard/." "${APP_STAGE}/wizard/"
# strip any AppleDouble / DS_Store that slipped in
find "${APP_STAGE}" \( -name '._*' -o -name '.DS_Store' \) -delete 2>/dev/null || true
chmod +x "${APP_STAGE}/cmd/"*

# copy icons into app ui
cp -f "${FPK_SRC}/ui/images/icon_64.png" "${APP_STAGE}/ui/images/"
cp -f "${FPK_SRC}/ui/images/icon_256.png" "${APP_STAGE}/ui/images/"
# desktop launcher must be executable (FnOS ThirdParty CGI)
if [ -f "${APP_STAGE}/ui/index.cgi" ]; then
  chmod +x "${APP_STAGE}/ui/index.cgi"
fi
# default port for launcher (rewritten by cmd/main on start)
echo "8080" > "${APP_STAGE}/ui/port"

# include short README in app
cat > "${APP_STAGE}/README.txt" <<EOF
DockerOps ${VERSION} — FnOS package
Image: ghcr.io/deltrivx/dockerops:latest
Docs: https://github.com/deltrivx/DockerOps
Desktop entry: /cgi/ThirdParty/dockerops/index.cgi (avoids 127.0.0.1 black screen)
First-run: open Web UI to set admin, or set DOCKEROPS_ADMIN_PASSWORD via install wizard.
EOF

(
  cd "${APP_STAGE}"
  tar --exclude='._*' --exclude='.DS_Store' -czf "${STAGE}/app.tgz" .
)

# 2) package root structure
mkdir -p "${STAGE}/cmd" "${STAGE}/config" "${STAGE}/wizard"
cp -R "${FPK_SRC}/cmd/." "${STAGE}/cmd/"
cp -R "${FPK_SRC}/config/." "${STAGE}/config/"
cp -R "${FPK_SRC}/wizard/." "${STAGE}/wizard/"
cp -f "${FPK_SRC}/ICON.PNG" "${STAGE}/ICON.PNG"
cp -f "${FPK_SRC}/ICON_256.PNG" "${STAGE}/ICON_256.PNG"
find "${STAGE}" \( -name '._*' -o -name '.DS_Store' \) -delete 2>/dev/null || true
chmod +x "${STAGE}/cmd/"*

# manifest with md5 of app.tgz
CHECKSUM="$(md5 -q "${STAGE}/app.tgz" 2>/dev/null || md5sum "${STAGE}/app.tgz" | awk '{print $1}')"
# rewrite version + checksum
sed \
  -e "s/^version[[:space:]]*=.*/version               = ${VERSION}/" \
  -e "s/^checksum[[:space:]]*=.*/checksum              = ${CHECKSUM}/" \
  "${FPK_SRC}/manifest" > "${STAGE}/manifest"

# 3) tar + gzip → .fpk
mkdir -p "${DIST}"
OUT_PATH="${DIST}/${OUT_NAME}"
(
  cd "${STAGE}"
  tar --exclude='._*' --exclude='.DS_Store' -cf - \
    app.tgz cmd config wizard manifest ICON.PNG ICON_256.PNG | gzip -9 > "${OUT_PATH}"
)

# sidecar meta for releases
cat > "${DIST}/dockerops-${VERSION}-fnos.meta.json" <<EOF
{
  "app_name": "dockerops",
  "display_name": "DockerOps 容器运维平台",
  "version": "${VERSION}",
  "platform": "all",
  "image": "ghcr.io/deltrivx/dockerops:latest",
  "fpk": "${OUT_NAME}",
  "checksum_md5": "${CHECKSUM}",
  "file_size": $(wc -c < "${OUT_PATH}" | tr -d ' '),
  "maintainer": "deltrivx",
  "homepage": "https://github.com/deltrivx/DockerOps",
  "install_notes": "飞牛应用中心手动安装 FPK；首次打开 Web 设置管理员，或安装向导预置密码。"
}
EOF

# sha256 of fpk
if command -v shasum >/dev/null; then
  shasum -a 256 "${OUT_PATH}" | awk '{print $1}' > "${OUT_PATH}.sha256"
else
  sha256sum "${OUT_PATH}" | awk '{print $1}' > "${OUT_PATH}.sha256"
fi

echo "==> OK ${OUT_PATH}"
ls -la "${OUT_PATH}" "${OUT_PATH}.sha256" "${DIST}/dockerops-${VERSION}-fnos.meta.json"
file "${OUT_PATH}"
# verify round-trip
python3 - <<PY
import gzip, tarfile, io
from pathlib import Path
p = Path("${OUT_PATH}")
data = gzip.decompress(p.read_bytes())
tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
names = tf.getnames()
print("members:", names)
assert "manifest" in names and "app.tgz" in names and "ICON_256.PNG" in names
print("verify ok")
PY
