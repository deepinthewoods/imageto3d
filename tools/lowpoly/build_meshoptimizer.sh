#!/usr/bin/env bash
# Build lib/libmeshoptimizer.so (loaded via ctypes by adaptive_lod04.py).
# Pinned to the meshoptimizer commit the reduction results were produced with.
# Needs git and g++.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
src=$(mktemp -d)
trap 'rm -rf "$src"' EXIT
git clone -q https://github.com/zeux/meshoptimizer "$src/meshoptimizer"
git -C "$src/meshoptimizer" checkout -q 73583c335e541c139821d0de2bf5f12960a04941
mkdir -p "$here/lib"
cat "$src"/meshoptimizer/src/*.cpp | g++ -O2 -x c++ - -I "$src/meshoptimizer/src" \
  -shared -fPIC -o "$here/lib/libmeshoptimizer.so"
echo "built $here/lib/libmeshoptimizer.so"
