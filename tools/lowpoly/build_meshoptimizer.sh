#!/usr/bin/env bash
# Build lib/libmeshoptimizer.so (Linux) or lib/libmeshoptimizer.dll (Windows, Git Bash + MinGW-w64 g++), loaded via ctypes by
# adaptive_lod04.py and tripo_pipeline/segment_first.py (portable.meshopt_library picks the file by OS).
# Pinned to the meshoptimizer commit the reduction results were produced with (1.0, 2025-12-06).
# Needs git and g++. The Windows DLL links the C/C++ runtime statically so it loads in python and in Blender's python alike.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
src=$(mktemp -d)
trap 'rm -rf "$src"' EXIT
git clone -q https://github.com/zeux/meshoptimizer "$src/meshoptimizer"
git -C "$src/meshoptimizer" checkout -q 73583c335e541c139821d0de2bf5f12960a04941
mkdir -p "$here/lib"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    out="$here/lib/libmeshoptimizer.dll"
    flags=(-shared -static -static-libgcc -static-libstdc++) ;;
  *)
    out="$here/lib/libmeshoptimizer.so"
    flags=(-shared -fPIC) ;;
esac
cat "$src"/meshoptimizer/src/*.cpp | g++ -O2 -x c++ - -I "$src/meshoptimizer/src" "${flags[@]}" -o "$out"
echo "built $out"
