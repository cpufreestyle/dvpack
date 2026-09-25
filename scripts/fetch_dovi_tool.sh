#!/usr/bin/env bash
# Fetch dovi_tool into tools/bin/. tools/ is gitignored so the binary never
# enters the repo; this script IS the "download step" referenced by
# dvpack.tools.require()'s error message.
#
# Why bottles instead of upstream: github.com / pypi.org / crates.io direct
# TLS is blocked by the local TUN, while Homebrew bottle mirrors are reachable.
# Version pinned: 2026-09-25 measured, 2.3.4 is what satisfies the P7->P5
# chain (mode 5 produces the self-consistent single-layer RPU tvOS accepts).
set -euo pipefail

VERSION=2.3.4
DEST="$(cd "$(dirname "$0")/.." && pwd)/tools/bin/dovi_tool"

if [ -x "$DEST" ] && [ "${FORCE:-0}" != "1" ]; then
    echo "already present: $DEST (FORCE=1 to re-fetch)"
    exit 0
fi

case "$(uname -m)" in
    arm64) BREW_ARCH=arm64 ;;
    x86_64) BREW_ARCH=x86_64 ;;
    *) echo "unknown arch $(uname -m)"; exit 1 ;;
esac

# Mirror first, then bottle tag. Bottles from a neighbouring macOS tag still
# run on this machine (verified: macOS 27.0 with the tahoe bottle).
MIRRORS=(
    https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles
    https://mirrors.ustc.edu.cn/brew-bottles
    https://mirrors.aliyun.com/homebrew/homebrew-bottles
)
TAGS=(tahoe sequoia sonoma)

mkdir -p "$(dirname "$DEST")"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

for tag in "${TAGS[@]}"; do
    for mirror in "${MIRRORS[@]}"; do
        url="$mirror/dovi_tool-$VERSION.${BREW_ARCH}_$tag.bottle.tar.gz"
        echo "try $url"
        if ! curl -fsSL --connect-timeout 5 --max-time 120 "$url" -o "$work/bottle.tar.gz"; then
            continue
        fi
        rm -rf "$work/x"
        mkdir -p "$work/x"
        tar -xzf "$work/bottle.tar.gz" -C "$work/x"
        bin="$(find "$work/x" -type f -name dovi_tool -perm -u+x | head -1 || true)"
        if [ -z "$bin" ]; then
            echo "  no binary inside, next source"
            continue
        fi
        rm -f "$DEST" 2>/dev/null || true
        cp "$bin" "$DEST"
        chmod +x "$DEST"
        if "$DEST" --version >/dev/null 2>&1; then
            echo "OK $("$DEST" --version 2>&1 | head -1) -> $DEST [tag=$tag mirror=$mirror]"
            exit 0
        fi
        echo "  binary does not run, next source"
    done
done

echo "all mirrors/tags failed; check reachability: curl -sI ${MIRRORS[0]}"
exit 1
