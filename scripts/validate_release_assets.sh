#!/usr/bin/env bash
set -euo pipefail

PLATFORM="${1:-}"
RELEASE_DIR="${2:-dist/release}"
DATE_TAG="${3:-}"
HUMAN_SL_MODEL_FILE_NAME="b18c384nbt-humanv0.bin.gz"
HUMAN_SL_MODEL_SHA256="637746e44f0efe00ad1245a50aa9bbf0716efe364c43965ead97bd6835d84ab5"
HUMAN_SL_MODEL_BYTES="99066230"

if [[ -z "$PLATFORM" || -z "$DATE_TAG" ]]; then
  echo "Usage: $0 <windows|mac-arm64|mac-amd64|linux> [release_dir] <date_tag>"
  exit 1
fi

if [[ ! -d "$RELEASE_DIR" ]]; then
  echo "Release directory not found: $RELEASE_DIR"
  exit 1
fi

assert_not_standalone_humansl_asset() {
  local path="$1"
  local name
  local lower_name
  name="$(basename "$path")"
  lower_name="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')"
  if [[ "$lower_name" == *"$HUMAN_SL_MODEL_FILE_NAME"* || "$lower_name" == *"human-sl-models"* ]]; then
    echo "HumanSL model must be bundled inside app packages, not published as a standalone asset: $name"
    exit 1
  fi
}

verify_humansl_model_file() {
  local model_path="$1"
  local actual_bytes
  local actual_sha

  actual_bytes="$(wc -c <"$model_path" | tr -d '[:space:]')"
  if [[ "$actual_bytes" != "$HUMAN_SL_MODEL_BYTES" ]]; then
    echo "Unexpected HumanSL model size in release asset"
    echo "Expected: $HUMAN_SL_MODEL_BYTES bytes"
    echo "Actual:   $actual_bytes bytes"
    exit 1
  fi
  actual_sha="$(shasum -a 256 "$model_path" | awk '{print $1}')"
  if [[ "$actual_sha" != "$HUMAN_SL_MODEL_SHA256" ]]; then
    echo "Unexpected HumanSL model SHA256 in release asset"
    echo "Expected: $HUMAN_SL_MODEL_SHA256"
    echo "Actual:   $actual_sha"
    exit 1
  fi
}

assert_humansl_model_in_zip() {
  local path="$1"
  local name
  local entry
  local tmp_model

  name="$(basename "$path")"
  if [[ "$name" != *.zip ]]; then
    return 0
  fi
  if ! command -v unzip >/dev/null 2>&1; then
    echo "unzip command not found; cannot verify bundled HumanSL model in $name"
    exit 1
  fi

  entry="$(unzip -Z1 "$path" | grep -E '(^|/)human-sl-models/b18c384nbt-humanv0\.bin\.gz$' | head -n 1 || true)"
  if [[ -z "$entry" ]]; then
    echo "Missing bundled HumanSL model in release zip: $name"
    exit 1
  fi
  tmp_model="$(mktemp)"
  unzip -p "$path" "$entry" >"$tmp_model"
  verify_humansl_model_file "$tmp_model"
  rm -f "$tmp_model"
}

expected=()
case "$PLATFORM" in
  windows)
    expected=(
      "${DATE_TAG}-windows64.opencl.installer.exe"
      "${DATE_TAG}-windows64.opencl.portable.zip"
      "${DATE_TAG}-windows64.nvidia.installer.exe"
      "${DATE_TAG}-windows64.nvidia.portable.zip"
      "${DATE_TAG}-windows64.nvidia50.cuda.installer.exe"
      "${DATE_TAG}-windows64.nvidia50.cuda.portable.zip"
      "${DATE_TAG}-windows64.with-katago.installer.exe"
      "${DATE_TAG}-windows64.with-katago.portable.zip"
      "${DATE_TAG}-windows64.without.engine.installer.exe"
      "${DATE_TAG}-windows64.without.engine.portable.zip"
    )
    trt_prefix="${DATE_TAG}-windows64.nvidia.tensorrt.portable.7z"
    trt_readme="${DATE_TAG}-windows64.nvidia.tensorrt.portable.README.txt"
    trt_manifest="${DATE_TAG}-windows64.nvidia.tensorrt.portable.manifest.json"
    trt_checksum="${DATE_TAG}-windows64.nvidia.tensorrt.portable.sha256.txt"
    shopt -s nullglob
    trt_parts=("$RELEASE_DIR/${trt_prefix}".[0-9][0-9][0-9])
    shopt -u nullglob
    if [[ "${#trt_parts[@]}" -eq 0 ]]; then
      echo "Missing advanced optional TensorRT split package: ${trt_prefix}.001"
      exit 1
    fi
    for index in "${!trt_parts[@]}"; do
      expected_part="$(printf '%s.%03d' "$trt_prefix" "$((index + 1))")"
      if [[ "$(basename "${trt_parts[$index]}")" != "$expected_part" ]]; then
        echo "TensorRT split volumes must be contiguous from .001"
        printf 'Expected: %s\nActual:   %s\n' "$expected_part" "$(basename "${trt_parts[$index]}")"
        exit 1
      fi
      expected+=("$(basename "${trt_parts[$index]}")")
    done
    expected+=("$trt_readme" "$trt_manifest" "$trt_checksum")
    ;;
  mac-arm64)
    expected=("${DATE_TAG}-mac-apple-silicon.with-katago.dmg")
    ;;
  mac-amd64)
    expected=("${DATE_TAG}-mac-intel.with-katago.dmg")
    ;;
  linux)
    expected=(
      "${DATE_TAG}-linux64.opencl.zip"
      "${DATE_TAG}-linux64.nvidia.zip"
      "${DATE_TAG}-linux64.with-katago.zip"
    )
    ;;
  *)
    echo "Unsupported platform: $PLATFORM"
    exit 1
    ;;
esac

actual=()
shopt -s nullglob
for path in "$RELEASE_DIR"/*; do
  [[ -f "$path" ]] || continue
  actual+=("$(basename "$path")")
done
shopt -u nullglob

if [[ "${#actual[@]}" -eq 0 ]]; then
  echo "No release assets found in $RELEASE_DIR"
  exit 1
fi

for name in "${actual[@]}"; do
  assert_not_standalone_humansl_asset "$RELEASE_DIR/$name"
  assert_humansl_model_in_zip "$RELEASE_DIR/$name"
  case "$name" in
    *.txt|*.sha256|*.sha256.txt|*.md)
      if [[ "$PLATFORM" != "windows" ]] || [[ "$name" != "${DATE_TAG}-windows64.nvidia.tensorrt.portable.README.txt" && "$name" != "${DATE_TAG}-windows64.nvidia.tensorrt.portable.sha256.txt" ]]; then
        echo "Unexpected helper file in public release set: $name"
        exit 1
      fi
      ;;
  esac
done

if [[ "${#actual[@]}" -ne "${#expected[@]}" ]]; then
  echo "Unexpected asset count for $PLATFORM"
  printf 'Expected (%s):\n' "${#expected[@]}"
  printf '  %s\n' "${expected[@]}"
  printf 'Actual (%s):\n' "${#actual[@]}"
  printf '  %s\n' "${actual[@]}"
  exit 1
fi

for name in "${expected[@]}"; do
  if [[ ! -f "$RELEASE_DIR/$name" ]]; then
    echo "Missing expected asset: $name"
    exit 1
  fi
done

for name in "${actual[@]}"; do
  match="false"
  for expected_name in "${expected[@]}"; do
    if [[ "$name" == "$expected_name" ]]; then
      match="true"
      break
    fi
  done
  if [[ "$match" != "true" ]]; then
    echo "Unexpected asset in public release set: $name"
    exit 1
  fi
done

echo "Validated public release assets for $PLATFORM:"
printf '  %s\n' "${actual[@]}"
