#!/usr/bin/env bash
# Build the exact release snapshot using the checked-in dependency lock patch.
set -euo pipefail
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_dir="$repo_dir/build/rpxy"
for tool in git cargo cc cmake; do
  command -v "$tool" >/dev/null || { echo "Missing build tool: $tool" >&2; exit 1; }
done
if [[ -e "$source_dir" ]]; then
  echo "Build directory already exists: $source_dir. Use a fresh checkout to build this snapshot." >&2
  exit 1
fi
mkdir -p "$repo_dir/build" "$repo_dir/dist"
git clone --no-checkout https://github.com/junkurihara/rust-rpxy.git "$source_dir"
git -C "$source_dir" checkout --detach ba492b585fe86a279dbaab467d521f2af398027f
git -C "$source_dir" submodule update --init --depth 1
git -C "$source_dir" apply --check "$repo_dir/patches/rpxy-forks.patch"
git -C "$source_dir" apply "$repo_dir/patches/rpxy-forks.patch"
(
  cd "$source_dir"
  CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}" cargo build --locked --release -p rpxy
)
install -m 755 "$source_dir/target/release/rpxy" "$repo_dir/dist/rpxy-opencloud-linux-amd64"
echo "Built: $repo_dir/dist/rpxy-opencloud-linux-amd64"
