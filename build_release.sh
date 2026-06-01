#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_DIR="${1:-${RELEASE_DIR:-${PROJECT_ROOT}/../Spica-Chatbot_release}}"
TMP_DIR="${RELEASE_DIR}.tmp"

PROJECT_ROOT_REAL="$(realpath -m "${PROJECT_ROOT}")"
RELEASE_DIR_REAL="$(realpath -m "${RELEASE_DIR}")"
TMP_DIR_REAL="$(realpath -m "${TMP_DIR}")"

GPT_DIR="agent_tools/tts/vendors/GPT-SoVITS-v2pro-20250604-nvidia50"
APPLIO_DIR="agent_tools/function_tools/song/Applio"
SPICA_DATA_DIR="spica_data"
THIRD_PARTY_DIR="third_party"
GENERATED_VOICE_DIR="static/generated_voice"
GENERATED_SONG_DIR="static/generated_song"

SOURCE_DIRS=(
  agent
  agent_tools
  common
  config
  examples
  memory
  static
  templates
  tests
  ui
)

SOURCE_FILES=(
  .gitignore
  README.md
  build_release.sh
  run_ibus.sh
  webui_qt.py
)

TAR_EXCLUDES=(
  --exclude='*/__pycache__'
  --exclude='*/__pycache__/*'
  --exclude='__pycache__'
  --exclude='__pycache__/*'
  --exclude='*.pyc'
  --exclude='*.pyo'
  --exclude='*$py.class'
  --exclude='.pytest_cache'
  --exclude='.pytest_cache/*'
  --exclude='.DS_Store'
  --exclude='*.env'
  --exclude='.env'
  --exclude="${GPT_DIR}"
  --exclude="${GPT_DIR}/*"
  --exclude="${APPLIO_DIR}"
  --exclude="${APPLIO_DIR}/*"
  --exclude="${GENERATED_VOICE_DIR}/*"
  --exclude="${GENERATED_SONG_DIR}/*"
)

die() {
  echo "$*" >&2
  exit 1
}

copy_release_item() {
  local item="$1"

  if [[ ! -e "${PROJECT_ROOT}/${item}" ]]; then
    return
  fi

  tar -C "${PROJECT_ROOT}" "${TAR_EXCLUDES[@]}" -cf - "${item}" \
    | tar -C "${TMP_DIR_REAL}" -xf -
}

copy_directory_skeleton() {
  local dir="$1"
  local rel

  if [[ -d "${PROJECT_ROOT}/${dir}" ]]; then
    while IFS= read -r -d '' rel; do
      mkdir -p "${TMP_DIR_REAL}/${rel}"
    done < <(
      cd "${PROJECT_ROOT}"
      find "${dir}" \
        -type d \
        ! -name '__pycache__' \
        ! -name '.pytest_cache' \
        -print0
    )
  else
    mkdir -p "${TMP_DIR_REAL}/${dir}"
  fi
}

if [[ -z "${PROJECT_ROOT_REAL}" || "${PROJECT_ROOT_REAL}" == "/" ]]; then
  die "Refusing to package from an invalid project root: ${PROJECT_ROOT_REAL}"
fi

if [[ "${RELEASE_DIR_REAL}" == "/" || "${TMP_DIR_REAL}" == "/" ]]; then
  die "Refusing to overwrite unsafe release path: ${RELEASE_DIR_REAL}"
fi

if [[ "${RELEASE_DIR_REAL}" == "${PROJECT_ROOT_REAL}" || "${TMP_DIR_REAL}" == "${PROJECT_ROOT_REAL}" ]]; then
  die "Refusing to overwrite the project root: ${PROJECT_ROOT_REAL}"
fi

case "${RELEASE_DIR_REAL}/" in
  "${PROJECT_ROOT_REAL}/"*)
    die "Refusing to create the release directory inside the project tree: ${RELEASE_DIR_REAL}"
    ;;
esac

rm -rf "${TMP_DIR_REAL}"
mkdir -p "${TMP_DIR_REAL}"

for item in "${SOURCE_FILES[@]}"; do
  copy_release_item "${item}"
done

for item in "${SOURCE_DIRS[@]}"; do
  copy_release_item "${item}"
done

mkdir -p "${TMP_DIR_REAL}/${GPT_DIR}"
mkdir -p "${TMP_DIR_REAL}/${APPLIO_DIR}"

copy_directory_skeleton "${SPICA_DATA_DIR}"
copy_directory_skeleton "${THIRD_PARTY_DIR}"

mkdir -p "${TMP_DIR_REAL}/${GENERATED_VOICE_DIR}"
mkdir -p "${TMP_DIR_REAL}/${GENERATED_SONG_DIR}"

if [[ -f "${PROJECT_ROOT}/xiaosan.env" ]]; then
  awk '
    /^[[:space:]]*$/ { print; next }
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
echo "Applio placeholder is empty: ${RELEASE_DIR_REAL}/${APPLIO_DIR}"
echo "spica_data files were stripped; directory skeleton was preserved."
echo "third_party files were stripped; directory skeleton was preserved."
echo "Generated voice files were stripped; output directory was preserved."
echo "Generated song files were stripped; output directory was preserved."
