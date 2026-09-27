#!/usr/bin/env bash
# Build the fPKG helper from backend/native/src/ffpfsc-pkg-tool into backend/native/:
# the executable ffpfsc-pkg-tool and the ImageMagick library it loads from its own folder.
# Neither is tracked in git. Needs the .NET SDK 9 or newer. BUILD_MACOS_APP.sh runs this
# first; on a fresh clone run it yourself before the tests.
set -euo pipefail
cd "$(dirname "$0")"

DOTNET="${DOTNET:-$(command -v dotnet || true)}"
[[ -n "${DOTNET}" ]] || DOTNET="${HOME}/.dotnet/dotnet"
if [[ ! -x "${DOTNET}" ]]; then
    echo "BUILD_PKG_TOOL.sh: the .NET SDK (9 or newer) is required: https://dotnet.microsoft.com/download" >&2
    echo "  (or point DOTNET at the dotnet executable)" >&2
    exit 1
fi

SRC="backend/native/src/ffpfsc-pkg-tool"
OUT="${SRC}/out"
rm -rf "${OUT}"
"${DOTNET}" publish "${SRC}/PkgTool.csproj" -c Release -r osx-arm64 --self-contained true -o "${OUT}"

for f in ffpfsc-pkg-tool Magick.Native-Q8-arm64.dll.dylib; do
    # Delete first: copying over a signed Mach-O in place can make macOS kill every later
    # run of it (the kernel keeps the old code signature for that inode).
    rm -f "backend/native/${f}"
    cp "${OUT}/${f}" "backend/native/${f}"
done
chmod +x backend/native/ffpfsc-pkg-tool
backend/native/ffpfsc-pkg-tool version | head -1
