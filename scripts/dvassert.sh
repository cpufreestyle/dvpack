#!/usr/bin/env bash
# dvassert.sh -- 可复用的杜比视界 HLS 断言器（开源三件套：dovi_tool + Bento4 mp4dump + ffmpeg）。
#   信号面：master VIDEO-RANGE=PQ + CODECS=dvh1.05.* ；init hvcC Tier=0 + dvcC profile5/rpu1/bl1/el0/compat0
#   数据面：抽 8s 成 Annex-B HEVC，dovi_tool extract-rpu 得非 0 RPU。任一面 FAIL 都不该上真机测。
#
# 口径依据（2026-10-02 真机实测）：tier / non_packed 一律清 0，与 Apple 官方 P5
#   参考流同构；两位置 1 的那一臂（variant D）真机实测「D 没亮」。见 dvpack/dplus.py
#   _clear_bit 注释。
# dvcC 字段走 mp4dump 结构化输出（Bento4 原生解析），不 grep ffprobe trace ——
#   ffmpeg 版本间 trace 文案会变（9.0 已与旧版不一致），会给出假 FAIL。
# non_packed 约束位 Bento4 不打印，那一层用 `python -m dvpack init <pack目录>` 补。
set -u
B="$(cd "$(dirname "$0")" && pwd)"
TOOLS="${DVPACK_TOOLS:-$B/../tools}"

pick() { # 优先 tools 自带（无网环境），其次 PATH
  if [ -x "$TOOLS/$1" ]; then echo "$TOOLS/$1"; else command -v "$2" 2>/dev/null || true; fi
}
FF="$(pick ffmpeg/bin/ffmpeg ffmpeg)"
FFP="$(pick ffmpeg/bin/ffprobe ffprobe)"
DV="$(pick bin/dovi_tool dovi_tool)"
MP4D="$(pick bin/mp4dump mp4dump)"
missing=0
for pair in ffmpeg="$FF" ffprobe="$FFP" dovi_tool="$DV" mp4dump="$MP4D"; do
  name="${pair%%=*}"; path="${pair#*=}"
  # ${name} 而不是 $name：bash 3.2（macOS 自带）在 set -u 下把紧跟全角括号的
  # $name 当未绑定变量，整个检查段直接崩（rc=1 而不是打印 FAIL）。
  if [ -z "$path" ]; then echo "FAIL  missing tool: ${name}（tools/bin 与 PATH 都没有）"; missing=1; fi
done
[ "$missing" -eq 0 ] || exit 2

MASTER="${1:-http://127.0.0.1:8899/master.m3u8}"
BASE="$(dirname "$MASTER")"
WORK="$(mktemp -d /tmp/dvassert.XXXXXX)"
pass=0; fail=0
ok(){ echo "PASS  $1"; pass=$((pass+1)); }
no(){ echo "FAIL  $1"; fail=$((fail+1)); }
echo "== dvassert master=$MASTER tools=$TOOLS =="
curl -fsS "$MASTER" -o "$WORK/master.m3u8" || { echo "FAIL  cannot fetch master"; exit 2; }
grep -q 'VIDEO-RANGE=PQ' "$WORK/master.m3u8" && ok 'master VIDEO-RANGE=PQ' || no 'master missing VIDEO-RANGE=PQ'
grep -qE 'CODECS="?dvh1[.]05' "$WORK/master.m3u8" && ok 'master CODECS=dvh1.05.*' || no 'master CODECS not dvh1.05'
if [ -f "$BASE/init.mp4" ]; then INIT="$BASE/init.mp4"; else curl -fsS "$BASE/init.mp4" -o "$WORK/init.mp4" && INIT="$WORK/init.mp4"; fi
INIT="${INIT:-$WORK/init.mp4}"
"$MP4D" "$INIT" > "$WORK/mp4dump.txt" 2>/dev/null
grep -q 'Tier = 0' "$WORK/mp4dump.txt" && ok 'hvcC Tier=0（点亮口径）' || no 'hvcC Tier not 0（两位置 1 的 variant D 实测不亮）'
grep -q 'dv_profile = 5' "$WORK/mp4dump.txt" && ok 'dvcC profile 5' || no 'dvcC not profile 5'
grep -q 'rpu_present_flag = 1' "$WORK/mp4dump.txt" && ok 'dvcC rpu flag=1' || no 'dvcC rpu flag not 1'
grep -q 'bl_present_flag = 1' "$WORK/mp4dump.txt" && ok 'dvcC bl flag=1' || no 'dvcC bl flag not 1'
grep -q 'el_present_flag = 0' "$WORK/mp4dump.txt" && ok 'dvcC el flag=0 single-layer' || no 'dvcC el flag not 0'
grep -q 'dv_bl_signal_compatibility_id = 0' "$WORK/mp4dump.txt" && ok 'dvcC compat id=0' || no 'dvcC compat id not 0'
"$FF" -hide_banner -v error -y -f hls -i "$MASTER" -c copy -t 8 -f hevc "$WORK/clip.hevc" 2>/dev/null
if [ -s "$WORK/clip.hevc" ]; then
  "$DV" extract-rpu -i "$WORK/clip.hevc" -o "$WORK/rpu.bin" >/dev/null 2>&1
  if [ -s "$WORK/rpu.bin" ]; then
    ok "segment inband RPU present ($(wc -c < "$WORK/rpu.bin") bytes)"
  else
    no 'no RPU in segments (extract-rpu failed)'
  fi
else
  no 'ffmpeg 抽 8s clip 失败（媒体列表或分段拉不下来）'
fi
echo "== result: PASS=$pass FAIL=$fail  workdir=$WORK =="
[ "$fail" -eq 0 ]
