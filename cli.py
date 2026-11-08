"""
命令列版本：python cli.py 台詞1.mp3 [台詞2.mp3 …] 背景音樂.mp3 [-d 輸出資料夾] [--mode keep|duck]
                           [--voice-db -3] [--ordered] [--merge-all]
最後一個位置參數是背景音樂，前面的全部是台詞剪輯。
"""
import argparse
import os
import sys
import core

p = argparse.ArgumentParser(description="把台詞剪輯無縫融合回完整背景音樂")
p.add_argument("files", nargs="+", help="台詞剪輯（可多個）…，最後一個是完整背景音樂")
p.add_argument("-d", "--out-dir", help="輸出資料夾（預設：背景音樂所在資料夾）")
p.add_argument("--fmt", choices=["mp3", "wav", "flac"], default="mp3", help="輸出格式（預設 mp3）")
p.add_argument("--mode", choices=["keep", "duck"], default="keep",
               help="keep=保持原曲音量（預設）；duck=電影式壓低音樂")
p.add_argument("--voice-db", type=float, default=-3.0, help="人聲音量微調 dB（預設 -3）")
p.add_argument("--fade", type=float, default=0.15, help="交叉淡化秒數（預設 0.15）")
p.add_argument("--offset", type=float, help="手動指定起點（秒），略過自動對齊（只適用單一剪輯）")
p.add_argument("--speed", type=float, default=1.0, help="剪輯速度倍率，PAL 影片為 1.04167（留 1 會自動偵測）")
p.add_argument("--ordered", action="store_true", help="強制依清單順序：各剪輯在背景音樂中的先後與清單相同且不重疊")
p.add_argument("--merge-all", action="store_true", help="把所有剪輯合併進同一個輸出檔")
a = p.parse_args()

if len(a.files) < 2:
    p.error("至少要有一個台詞剪輯和一個背景音樂")
clips, bgm = a.files[:-1], a.files[-1]
if a.offset is not None and len(clips) > 1:
    p.error("--offset 只適用於單一剪輯")
out_dir = a.out_dir or (os.path.dirname(os.path.abspath(bgm)))


def resolve(issues):
    """互動模式逐一詢問；非互動（重導向）時採預設選項。"""
    print("\n發現異常：")
    decisions = {}
    for it in issues:
        print(f"  {it['name']}：" + "；".join(it["reasons"]))
        if not sys.stdin.isatty():
            decisions[it["index"]] = it["default"]
            print(f"    → 自動採用：{core.OPT_LABEL[it['default']]}")
            continue
        keys = {o[0]: o for o in it["options"]}            # k/t/s/b
        menu = "  ".join(f"[{o[0]}] {core.OPT_LABEL[o]}" for o in it["options"]) + "  [a] 放棄全部"
        while True:
            ans = input(f"    {menu}\n    選擇（預設 {it['default'][0]}）：").strip().lower()
            if ans == "":
                decisions[it["index"]] = it["default"]; break
            if ans == "a":
                return None
            if ans in keys:
                decisions[it["index"]] = keys[ans]; break
    return decisions


def show(name, info, out_path):
    if info is None:
        print(f"\n{name}：{out_path or '找不到在背景音樂中的位置'}")
        return
    cov = info["coverage"] * 100
    print(f"\n{name}")
    print(f"  插入時間點：{core.fmt_time(info['offset_sec'])}（{info['offset_sec']:.3f} 秒）"
          f"  → 結束於 {core.fmt_time(info['end_sec'])}")
    print(f"  吻合：{info['matched_sec']:.1f}/{info['clip_len']:.1f} 秒（{cov:.0f}%）"
          + ("" if cov >= 50 else "  ⚠ 剪輯大部分不是這首歌，請檢查"))
    span = info.get("match_span")
    if span and cov < 90:
        print(f"  吻合段：剪輯 {span[0]:.1f}~{span[1]:.1f} 秒 → 歌曲 {core.fmt_time(info['offset_sec'] + span[0])}"
              f"~{core.fmt_time(info['offset_sec'] + span[1])}")
    if info["confidence"] == info["confidence"]:
        print(f"  粗搜尋信心：{info['confidence']:.1f}")
    if info["speed"] != 1.0:
        print(f"  速度倍率：{info['speed']:.5f}")
    print(f"  剪輯內 BGM 相對原曲：{info['clip_bgm_db']:+.1f} dB；剪輯增益 {info['clip_gain_db']:+.1f} dB；"
          f"限幅 {info['limiter_db']:.1f} dB")
    print(f"  輸出：{out_path}")


try:
    results, note = core.run_job(clips, bgm, out_dir, mode=a.mode, voice_db=a.voice_db, fade_sec=a.fade,
                                 manual_offset=a.offset, speed=a.speed, ordered=a.ordered,
                                 merge_all=a.merge_all, fmt=a.fmt,
                                 progress=lambda s: print("  " + s), on_result=show, resolve=resolve)
except core.JobAborted:
    print("已放棄。")
    sys.exit(1)
if note:
    print(f"\n備註：{note}")
