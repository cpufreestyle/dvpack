#!/usr/bin/env bash
# dvassert.sh -- 可复用的杜比视界 HLS 断言器（复用开源三件套 dovi_tool + Bento4 mp4dump + ffprobe）。
#   信号面：master VIDEO-RANGE=PQ + CODECS=dvh1.05.* ；init hvcC Tier=1(High) ；dvcC profile5/rpu1/bl1/el0
#   数据面：抽 8s 成 Annex-B HEVC，dovi_tool extract-rpu 得非 0 RPU。任一面 FAIL 都不该上真机测。
set -u
B="$(cd "$(dirname "$0")" && pwd)"
TOOLS="${DVPACK_TOOLS:-$B/../tools}"
FF="$TOOLS/ffmpeg/bin/ffmpeg"; FFP="$TOOLS/ffmpeg/bin/ffprobe"; DV="$TOOLS/bin/dovi_tool"; MP4D="$TOOLS/bin/mp4dump"
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
"$MP4D" "$INIT" 2>/dev/null | grep -q 'Tier = 1' && ok 'hvcC Tier=1 (High)' || no 'hvcC Tier not 1'
$FFP -v trace "$INIT" 2>&1 | grep -q 'profile: 5' && ok 'dvcC profile 5' || no 'dvcC not profile 5'
$FFP -v trace "$INIT" 2>&1 | grep -q 'rpu flag: 1' && ok 'dvcC rpu flag=1' || no 'dvcC rpu flag not 1'
$FFP -v trace "$INIT" 2>&1 | grep -q 'bl flag: 1' && ok 'dvcC bl flag=1' || no 'dvcC bl flag not 1'
$FFP -v trace "$INIT" 2>&1 | grep -q 'el flag: 0' && ok 'dvcC el flag=0 single-layer' || no 'dvcC el flag not 0'
$FF -hide_banner -v error -y -f hls -i "$MASTER" -c copy -t 8 -f hevc "$WORK/clip.hevc" 2>/dev/null
$DV extract-rpu -i "$WORK/clip.hevc" -o "$WORK/rpu.bin" >/dev/null 2>&1
sz=$(wc -c < "$WORK/rpu.bin" 2>/dev/null || echo 0)
[ "$sz" -gt 0 ] && ok "segment inband RPU present ($sz bytes)" || no 'no RPU in segments (extract-rpu=0)'
echo "== result: PASS=$pass FAIL=$fail  workdir=$WORK =="
[ "$fail" -eq 0 ]
