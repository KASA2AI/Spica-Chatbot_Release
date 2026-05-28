#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_DIR="${PROJECT_ROOT}/../Spica-Chatbot_release"
TMP_DIR="${RELEASE_DIR}.tmp"
PROJECT_ROOT_REAL="$(realpath -m "${PROJECT_ROOT}")"
RELEASE_DIR_REAL="$(realpath -m "${RELEASE_DIR}")"
TMP_DIR_REAL="$(realpath -m "${TMP_DIR}")"
GPT_DIR="GPT-SoVITS-v2pro-20250604-nvidia50"
SPICA_DATA_DIR="spica_data"

if [[ -z "${PROJECT_ROOT_REAL}" || "${PROJECT_ROOT_REAL}" == "/" ]]; then
  echo "Refusing to package from an invalid project root: ${PROJECT_ROOT_REAL}" >&2
  exit 1
fi

if [[ "${RELEASE_DIR_REAL}" == "${PROJECT_ROOT_REAL}" || "${TMP_DIR_REAL}" == "${PROJECT_ROOT_REAL}" || "${RELEASE_DIR_REAL}" == "/" ]]; then
  echo "Refusing to overwrite unsafe release path: ${RELEASE_DIR_REAL}" >&2
  exit 1
fi

rm -rf "${TMP_DIR_REAL}"
mkdir -p "${TMP_DIR_REAL}"

tar -C "${PROJECT_ROOT}" \
  --exclude="./.git" \
  --exclude="./.git/*" \
  --exclude="./.idea" \
  --exclude="./.idea/*" \
  --exclude="./.agents" \
  --exclude="./.agents/*" \
  --exclude="./.codex" \
  --exclude="./.codex/*" \
  --exclude="./.venv" \
  --exclude="./.venv/*" \
  --exclude="./venv" \
  --exclude="./venv/*" \
  --exclude="./.pytest_cache" \
  --exclude="./.pytest_cache/*" \
  --exclude="./__pycache__" \
  --exclude="./__pycache__/*" \
  --exclude="./tests/__pycache__" \
  --exclude="./tests/__pycache__/*" \
  --exclude="./*.pyc" \
  --exclude="./.DS_Store" \
  --exclude="./${GPT_DIR}" \
  --exclude="./${GPT_DIR}/*" \
  --exclude="./${SPICA_DATA_DIR}" \
  --exclude="./${SPICA_DATA_DIR}/*" \
  --exclude="./static/generated_voice/*" \
  --exclude="./.env" \
  --exclude="./*.env" \
  --exclude="./xiaosan.env" \
  -cf - . | tar -C "${TMP_DIR_REAL}" -xf -

mkdir -p "${TMP_DIR_REAL}/${GPT_DIR}"

if [[ -d "${PROJECT_ROOT}/${SPICA_DATA_DIR}" ]]; then
  while IFS= read -r -d '' dir; do
    rel="${dir#${PROJECT_ROOT}/}"
    mkdir -p "${TMP_DIR_REAL}/${rel}"
  done < <(find "${PROJECT_ROOT}/${SPICA_DATA_DIR}" -type d -print0)
else
  mkdir -p "${TMP_DIR_REAL}/${SPICA_DATA_DIR}"
fi

mkdir -p "${TMP_DIR_REAL}/static/generated_voice"

if [[ -f "${PROJECT_ROOT}/xiaosan.env" ]]; then
  awk '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { print; next }
    {
      eq = index($0, "=")
      if (eq == 0) {
        print $0
      } else {
        print substr($0, 1, eq)
      }
    }
  ' "${PROJECT_ROOT}/xiaosan.env" > "${TMP_DIR_REAL}/xiaosan.env"
else
  {
    printf "# Fill these values before running Spica Chatbot.\n"
    printf "OPENAI_API_KEY=\n"
    printf "OPENAI_BASE_URL=\n"
    printf "MODEL=\n"
  } > "${TMP_DIR_REAL}/xiaosan.env"
fi

rm -rf "${RELEASE_DIR_REAL}"
mv "${TMP_DIR_REAL}" "${RELEASE_DIR_REAL}"

echo "Release directory created: ${RELEASE_DIR_REAL}"
echo "GPT-SoVITS placeholder is empty: ${RELEASE_DIR_REAL}/${GPT_DIR}"
echo "spica_data files were stripped; directory skeleton was preserved."
