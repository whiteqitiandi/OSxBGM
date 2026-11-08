"""
OSxBGM 核心：把短台詞剪輯（含背景音樂）對齊到完整背景音樂並無縫融合。
只依賴 numpy / scipy 與 ffmpeg 執行檔。
"""
import os
import shutil
import subprocess
import sys
import numpy as np
from scipy import signal
from scipy.ndimage import uniform_filter1d, maximum_filter1d


# ---------------------------------------------------------------- ffmpeg
import configparser

INI_NAME = "OSxBGM.ini"


def app_dir():
    """程式所在資料夾：打包成 exe 時是 exe 的資料夾，否則是 .py 的資料夾。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def ini_path():
    return os.path.join(app_dir(), INI_NAME)


def load_ini():
    cfg = configparser.ConfigParser()
    cfg.read(ini_path(), encoding="utf-8")
    return cfg


def save_ini(cfg):
    with open(ini_path(), "w", encoding="utf-8") as f:
        f.write("; OSxBGM 設定檔\n; ffmpeg = ffmpeg.exe 的完整路徑，例如 C:\\ffmpeg64\\bin\\ffmpeg.exe\n")
        cfg.write(f)


def get_ini(section, key, default=""):
    return load_ini().get(section, key, fallback=default)


def set_ini(section, key, value):
    set_ini_many(section, {key: value})


def set_ini_many(section, mapping):
    cfg = load_ini()
    if not cfg.has_section(section):
        cfg.add_section(section)
    for k, v in mapping.items():
        cfg.set(section, k, str(v))
    save_ini(cfg)


def get_ini_section(section):
    cfg = load_ini()
    return dict(cfg.items(section)) if cfg.has_section(section) else {}


def find_ffmpeg(name="ffmpeg"):
    """依序找：OSxBGM.ini 指定路徑 → 程式所在資料夾 → exe 內嵌 → PATH。"""
    exe = name + (".exe" if os.name == "nt" else "")
    p = get_ini("ffmpeg", name if name != "ffmpeg" else "ffmpeg").strip().strip('"')
    if p:
        if os.path.isdir(p):                      # 使用者填了資料夾
            p = os.path.join(p, exe)
        if name != "ffmpeg":                      # 由 ffmpeg 路徑推 ffprobe 等
            p = os.path.join(os.path.dirname(p), exe)
        if os.path.isfile(p):
            return p
    for d in (app_dir(), os.path.dirname(os.path.abspath(sys.argv[0])),
              getattr(sys, "_MEIPASS", "")):
        if d and os.path.isfile(os.path.join(d, exe)):
            return os.path.join(d, exe)
    p = shutil.which(name)
    if p:
        return p
    raise FileNotFoundError(
        f"找不到 {exe}。\n請在程式介面的「ffmpeg 路徑」欄位指定（會存到 {INI_NAME}），\n"
        f"或把 {exe} 放到程式同一個資料夾，或加入 PATH。")


def ffmpeg_version(path=None):
    r = _run([path or find_ffmpeg(), "-version"])
    return r.stdout.decode("utf8", "ignore").splitlines()[0] if r.returncode == 0 else "（無法執行）"


def _run(cmd, stdin=None):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(cmd, input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, creationflags=flags)


def decode(path, sr=44100, mono=False):
    """用 ffmpeg 解碼成 float32 numpy 陣列，shape = (n, ch)。"""
    ch = 1 if mono else 2
    cmd = [find_ffmpeg(), "-v", "error", "-i", path, "-f", "f32le",
           "-acodec", "pcm_f32le", "-ac", str(ch), "-ar", str(sr), "-"]
    r = _run(cmd)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg 解碼失敗：" + r.stderr.decode("utf8", "ignore"))
    a = np.frombuffer(r.stdout, dtype=np.float32).reshape(-1, ch)
    return a.copy()


def encode(path, audio, sr=44100, bitrate="192k"):
    """把 (n, ch) float32 寫成 mp3 / wav / flac…（由副檔名決定）。"""
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim == 1:
        audio = audio[:, None]
    ch = audio.shape[1]
    cmd = [find_ffmpeg(), "-v", "error", "-y", "-f", "f32le", "-ar", str(sr),
           "-ac", str(ch), "-i", "-"]
    if path.lower().endswith(".mp3"):
        cmd += ["-codec:a", "libmp3lame", "-b:a", bitrate]
    cmd.append(path)
    r = _run(cmd, stdin=audio.tobytes())
    if r.returncode != 0:
        raise RuntimeError("ffmpeg 編碼失敗：" + r.stderr.decode("utf8", "ignore"))


# ------------------------------------------------------------- alignment
ALIGN_SR = 22050          # 粗搜尋用的取樣率
CONF_GOOD = 12.0          # 粗搜尋 z-score 門檻：以上視為可靠
VERIFY_WIN = 1.0          # 逐段驗證：視窗長度（秒）
VERIFY_HOP = 0.5          # 逐段驗證：視窗間隔（秒）
VERIFY_SEARCH = 0.02      # 逐段驗證：在預測位置 ± 幾秒內找波形互相關峰值
LAG_TOL = 8               # 逐段驗證：峰值位置與主位置差幾個取樣點內算「吻合」
MIN_OVERLAP = 4.0         # 剪輯與背景音樂至少要重疊幾秒（允許剪輯超出 BGM 頭尾）
SILENCE_DB = -45.0        # 剪輯低於此音量視為無聲（淡入淡出、空白）
GOOD_COVERAGE = 0.5       # 吻合比例低於此值時，改用分段搜尋 / 嘗試變速


def _to_low(x, sr):
    if sr % ALIGN_SR == 0:
        return signal.resample_poly(x, 1, sr // ALIGN_SR).astype(np.float32)
    return signal.resample(x, int(len(x) * ALIGN_SR / sr)).astype(np.float32)


def _speed_change(x, factor):
    """把訊號播放速度乘以 factor（>1 變快變短）。x 可為 (n,) 或 (n, ch)。"""
    n = len(x)
    idx = np.arange(0, n - 1, factor)
    if x.ndim == 1:
        return np.interp(idx, np.arange(n), x).astype(np.float32)
    return np.stack([np.interp(idx, np.arange(n), x[:, i]) for i in range(x.shape[1])], 1).astype(np.float32)


def _frac_shift(x, delta):
    """把訊號延後 delta 個取樣點（可為小數、可為負），用 FFT 相位平移。"""
    n = len(x)
    f = np.fft.rfftfreq(n)                     # cycles / sample
    ph = np.exp(-2j * np.pi * f * delta).astype(np.complex64)
    if x.ndim == 1:
        return np.fft.irfft(np.fft.rfft(x) * ph, n).astype(np.float32)
    return np.stack([np.fft.irfft(np.fft.rfft(x[:, i]) * ph, n) for i in range(x.shape[1])], 1).astype(np.float32)


def _mono(x):
    return x.mean(1) if x.ndim == 2 else x


class BgmIndex:
    """預先計算背景音樂的頻譜，讓多個剪輯 / 多個視窗 / 多個變速候選可以重複使用。"""

    def __init__(self, bgm_mono, sr):
        self.sr = sr
        self.full = np.ascontiguousarray(bgm_mono, dtype=np.float32)
        self.low = _to_low(self.full, sr)
        self.nf = 0
        self._ensure(30 * ALIGN_SR)

    def _ensure(self, clip_len_low):
        """FFT 長度至少要容納 BGM + 剪輯，否則互相關會繞回來（剪輯長度因此不受限）。"""
        need = len(self.low) + int(clip_len_low) + 1
        if need <= self.nf:
            return
        self.nf = 1 << int(np.ceil(np.log2(need)))
        self.L = np.fft.rfft(self.low, self.nf).astype(np.complex64)
        f = np.fft.rfftfreq(self.nf, 1 / ALIGN_SR)
        self.band = ((f >= 120) & (f <= 7000)).astype(np.float32)

    def corr(self, clip_low, rho=0.75):
        """加權相位相關（GCC-ρ）：對人聲、特效、EQ 失真都很強健。回傳整條相關函數。"""
        self._ensure(len(clip_low))
        S = np.fft.rfft(clip_low, self.nf).astype(np.complex64)
        X = self.L * np.conj(S)
        X = X / (np.abs(X) + 1e-9) ** rho * self.band
        return np.fft.irfft(X, self.nf).astype(np.float32)

    def search(self, clip_low, min_overlap_low=None, k=1, min_dist=None):
        """
        回傳 [(lag_low, z)] 最多 k 個候選（依 z 由高到低，彼此相距 ≥ min_dist）。
        lag_low 為剪輯起點在 BGM 中的位置（粗取樣率樣本），可為負（剪輯比 BGM 早開始）
        或超出尾端，只要重疊 ≥ min_overlap_low。
        """
        r = self.corr(clip_low)
        n, m = len(self.low), len(clip_low)
        ov = m if min_overlap_low is None else int(min(min_overlap_low, m, n))
        lo, hi = -(m - ov), n - ov
        idx = np.arange(lo, hi + 1)
        vals = r[idx % self.nf]                        # 負 lag 繞在陣列尾端
        mu, sd = float(vals.mean()), float(vals.std()) + 1e-12
        if min_dist is None:
            min_dist = 2 * ALIGN_SR
        top = min(len(vals), max(64, k * 64))
        cand = np.argpartition(-vals, top - 1)[:top]
        cand = cand[np.argsort(-vals[cand])]
        out = []
        for c in cand:
            lag = int(idx[c])
            if all(abs(lag - l0) >= min_dist for l0, _ in out):
                out.append((lag, (float(vals[c]) - mu) / sd))
            if len(out) >= k:
                break
        return out


def _ncc_peak(short, long):
    """短訊號在長訊號上的正規化互相關，回傳 (lag, score, scores)。"""
    n = len(short)
    if len(long) < n:
        raise ValueError("背景音樂比台詞剪輯還短。")
    short = (short - short.mean()) / (short.std() + 1e-9)
    corr = signal.correlate(long, short, mode="valid", method="fft")
    # 滑動視窗的平均與能量（累積和，float64 避免誤差累積）
    cs = np.concatenate([[0.0], np.cumsum(long, dtype=np.float64)])
    cs2 = np.concatenate([[0.0], np.cumsum(long.astype(np.float64) ** 2)])
    m = (cs[n:] - cs[:-n]) / n
    e = (cs2[n:] - cs2[:-n])
    sd = np.sqrt(np.maximum(e / n - m * m, 1e-12))
    scores = corr / (n * sd)
    lag = int(np.argmax(scores))
    return lag, float(scores[lag]), scores


def _runs(mask):
    """把布林陣列切成 (start, end, value) 連續段。"""
    out, i = [], 0
    while i < len(mask):
        j = i
        while j < len(mask) and mask[j] == mask[i]:
            j += 1
        out.append((i, j, bool(mask[i])))
        i = j
    return out


def verify(clip_mono, bgm_mono, sr, offset):
    """
    在指定起點（樣本，可為小數/負數）逐段檢查剪輯的哪些部分真的存在於背景音樂裡。
    每 VERIFY_HOP 秒取一個 VERIFY_WIN 秒的視窗，在預測位置 ±VERIFY_SEARCH 內做波形互相關；
    峰值位置與主位置一致（±LAG_TOL 取樣點）才算吻合——不吻合的視窗峰值位置是隨機的，
    所以這個判準不怕人聲很大聲、也不需要音量門檻。
    回傳 dict：offset（精修後起點，含小數）、matched（各視窗布林）、t（各視窗起點秒）、
    coverage、matched_sec、dlag、score、drift_speed（若發現線性漂移則為建議速度倍率）…
    """
    n = len(clip_mono)
    start = int(np.floor(offset + 0.5))
    W = min(int(VERIFY_WIN * sr), n)
    H = max(1, int(VERIFY_HOP * sr))
    R = int(VERIFY_SEARCH * sr)
    starts = list(range(0, max(1, n - W + 1), H))
    k = len(starts)
    dlag = np.full(k, np.nan)
    frac = np.full(k, np.nan)
    score = np.zeros(k)
    rms_db = np.full(k, -120.0)
    silent = np.zeros(k, bool)
    # 每個視窗先把頭尾的無聲部分切掉（剪輯開頭/結尾常有空白），只用有聲音的部分比對
    env = np.sqrt(np.maximum(uniform_filter1d(clip_mono * clip_mono, max(1, int(0.01 * sr))), 0))
    active = env > 10 ** (SILENCE_DB / 20)
    min_len = int(0.25 * sr)
    for j, i in enumerate(starts):
        idx = np.flatnonzero(active[i:i + W])
        if len(idx) == 0 or idx[-1] + 1 - idx[0] < min_len:
            silent[j] = True
            continue
        a, b = i + idx[0], i + idx[-1] + 1
        c = clip_mono[a:b]
        rms_db[j] = 20 * np.log10(float(np.sqrt(np.mean(c * c))) + 1e-9)
        lo = start + a - R
        if lo < 0 or lo + len(c) + 2 * R > len(bgm_mono):
            continue
        seg = bgm_mono[lo:lo + len(c) + 2 * R]
        lag, sc, scores = _ncc_peak(c, seg)
        dlag[j] = lag - R
        score[j] = sc
        if 0 < lag < len(scores) - 1:
            a, b, c3 = scores[lag - 1], scores[lag], scores[lag + 1]
            den = a - 2 * b + c3
            if den < 0:
                frac[j] = float(np.clip(0.5 * (a - c3) / den, -0.5, 0.5))
    valid = ~np.isnan(dlag) & (score > 0.05)
    prof = dict(offset=float(offset), start=start, t=np.array(starts) / sr, win=W / sr,
                dlag=dlag, score=score, rms_db=rms_db, silent=silent, drift_speed=None)
    if not valid.any():
        prof.update(matched=np.zeros(k, bool), coverage=0.0, matched_sec=0.0, mean_score=0.0,
                    match_span=None)
        return prof
    # 主位置：以 score 加權，找 ±LAG_TOL 內累積分數最高的 lag
    vl, vs = dlag[valid], score[valid]
    best_c, best_w = 0.0, -1.0
    for c in np.unique(vl):
        w = float(vs[np.abs(vl - c) <= LAG_TOL].sum())
        if w > best_w:
            best_c, best_w = float(c), w
    center = float(np.median(vl[np.abs(vl - best_c) <= LAG_TOL]))
    matched = valid & (np.abs(dlag - center) <= LAG_TOL)
    # 清理：吻合區中間 ≤1.5 秒的缺口補上（人聲太大聲的瞬間）；孤立 ≤1 秒的吻合視為巧合
    if matched.any():
        runs = _runs(matched)
        for a, b, v in runs:
            if not v and 0 < a and b < k and (b - a) <= 3:
                matched[a:b] = True
        runs = _runs(matched)
        longest = max((b - a) for a, b, v in runs if v)
        for a, b, v in runs:
            if v and (b - a) <= 2 and longest > 2:
                matched[a:b] = False
    # 線性漂移（剪輯與原曲速度略有不同）：峰值位置隨時間呈直線，直線上的視窗才是真的吻合
    drift = None
    if valid.sum() >= 6:
        x = np.array(starts, float)
        b, a = np.polyfit(x[valid], dlag[valid], 1)
        resid = dlag - (a + b * x)
        inl = valid & (np.abs(resid) <= LAG_TOL)
        span = float(x[valid].max() - x[valid].min())
        if inl.sum() >= max(6, 0.5 * valid.sum()) and inl.sum() > matched.sum() + 2 \
                and abs(b) * span > 10:
            drift = 1.0 + b                      # 剪輯速度倍率（<1 = 剪輯比原曲慢）
            matched = inl
            center = float(a)                    # 剪輯起點處的位置差
    # 取樣點以下精修：用吻合且相關高的視窗
    good = matched & (score > 0.3) & ~np.isnan(frac)
    fine = float(np.median((dlag + frac)[good])) if good.sum() >= 1 and drift is None else center
    msec = float(matched.sum()) * VERIFY_HOP
    if matched.any():
        idx = np.flatnonzero(matched)
        prof["match_span"] = (starts[idx[0]] / sr, min(n / sr, starts[idx[-1]] / sr + W / sr))
    else:
        prof["match_span"] = None
    prof.update(offset=start + fine, matched=matched, matched_sec=min(msec, n / sr),
                coverage=min(1.0, msec / (n / sr)),
                mean_score=float(score[matched].mean()) if matched.any() else 0.0,
                drift_speed=drift)
    return prof


def _cluster_search(clip_low, index, min_z=10.0, win=4.0, hop=4.0, k=3):
    """
    分段搜尋：把剪輯切成 win 秒的視窗各自在整首 BGM 上粗搜尋，
    把「視窗位置 − 視窗在剪輯中的時間」相近的視窗聚成一群，群內 z 總和最高者即為起點候選。
    對付剪輯只有一部分來自這首歌（電影剪接過音樂）、或剪輯超出 BGM 頭尾的情況。
    """
    W, H = int(win * ALIGN_SR), int(hop * ALIGN_SR)
    obs = []
    for i in range(0, max(1, len(clip_low) - W + 1), H):
        w = clip_low[i:i + W]
        if len(w) < ALIGN_SR:
            break
        (lag, z), = index.search(w, k=1)
        if z >= min_z:
            obs.append((i, lag - i, z))
    if not obs:
        return []
    B = int(0.05 * ALIGN_SR)
    bins = {}
    for t, d, z in obs:
        bins.setdefault(int(np.floor(d / B)), []).append((t, d, z))
    scored = []
    for b in bins:
        members = bins.get(b - 1, []) + bins[b] + bins.get(b + 1, [])
        zs = sum(z for _, _, z in members)
        d = sum(d * z for _, d, z in members) / zs
        scored.append((zs, d, len(members)))
    scored.sort(reverse=True)
    out = []
    for zs, d, cnt in scored:
        if all(abs(d - d0) > 0.2 * ALIGN_SR for _, d0, _ in out):
            out.append((zs, d, cnt))
        if len(out) >= k:
            break
    return out


def locate(clip, bgm_mono, sr, index=None, n_cand=6, progress=lambda s: None):
    """
    找出剪輯在背景音樂中的起點候選。clip 可為 (n,) 或 (n, ch)。
    回傳 (cands, clip_used, speed)：
      cands     依吻合秒數排序的 verify() 結果（含 'z'），cands[0] 最佳；可能為空
      clip_used 若自動偵測到速度差異，為已校正速度的剪輯，否則就是原剪輯
      speed     偵測到的剪輯速度倍率（1.0 = 無）
    流程：整段 GCC-ρ 粗搜尋取前幾名 → 逐段驗證 → 吻合比例不足時分段搜尋 →
    發現線性漂移則校正速度重做 → 仍不足時嘗試 PAL 25/24 與 24/25。
    """
    if index is None:
        index = BgmIndex(bgm_mono, sr)
    ratio = sr / ALIGN_SR

    def run(mono):
        c_low = _to_low(mono, sr)
        ov = int(min(MIN_OVERLAP, 0.5 * len(mono) / sr) * ALIGN_SR)
        peaks = index.search(c_low, ov, k=n_cand)
        cands, seen = [], []

        def add(off, z):
            if any(abs(off - s) < 0.05 * sr for s in seen):
                return None
            seen.append(off)
            v = verify(mono, bgm_mono, sr, off)
            v["z"] = float(z)
            cands.append(v)
            return v

        for j, (lag, z) in enumerate(peaks):
            if j >= 2 and z < 6.0:                 # 之後的弱峰值幾乎都是雜訊，不浪費時間驗證
                break
            v = add(lag * ratio, z)
            if v is not None and v["coverage"] >= 0.9:
                break
        cands.sort(key=lambda v: (v["matched_sec"], v["mean_score"]), reverse=True)
        if not cands or cands[0]["coverage"] < GOOD_COVERAGE:
            progress("整段對齊不理想，改用分段搜尋…")
            for zs, d, cnt in _cluster_search(c_low, index):
                add(d * ratio, zs)
            cands.sort(key=lambda v: (v["matched_sec"], v["mean_score"]), reverse=True)
        # 吻合太少的候選等於巧合，不如回報找不到
        floor = max(2.0, 0.05 * len(mono) / sr)
        return [v for v in cands if v["matched_sec"] >= floor]

    mono = _mono(clip)
    cands = run(mono)
    speed = 1.0
    clip_used = clip
    best = cands[0] if cands else None
    if best is not None and best["drift_speed"]:
        sp = best["drift_speed"]
        progress(f"偵測到速度差異 {sp:.5f}，校正後重新對齊…")
        c2 = _speed_change(clip, 1 / sp)
        cands2 = run(_mono(c2))
        if cands2 and cands2[0]["matched_sec"] > best["matched_sec"]:
            cands, clip_used, speed, best = cands2, c2, sp, cands2[0]
    # 幾乎完全找不到才嘗試變速（部分吻合代表速度沒問題，只是剪輯裡有別的音樂）：
    # PAL 25/24、24/25，以及 24 ↔ 23.976 fps 這類 0.1 % 等級的漂移。
    if best is None or best["matched_sec"] < max(3.0, 0.2 * len(mono) / sr):
        for sp in (25 / 24, 24 / 25, 1.001, 0.999, 1.002, 0.998):
            progress(f"嘗試速度倍率 {sp:.5f}…")
            c2 = _speed_change(clip, 1 / sp)
            cands2 = run(_mono(c2))
            if not cands2 or cands2[0]["coverage"] < GOOD_COVERAGE:
                continue
            if cands2[0]["drift_speed"]:            # 粗倍率附近再校正殘餘漂移
                sp3 = sp * cands2[0]["drift_speed"]
                c3 = _speed_change(clip, 1 / sp3)
                cands3 = run(_mono(c3))
                if cands3 and cands3[0]["matched_sec"] > cands2[0]["matched_sec"]:
                    cands2, c2, sp = cands3, c3, sp3
            if best is None or cands2[0]["matched_sec"] > 1.5 * best["matched_sec"]:
                cands, clip_used, speed, best = cands2, c2, sp, cands2[0]
            break
    return cands, clip_used, speed


def refine(clip_mono, bgm_mono, sr, offset_sec, window_sec=0.005):
    """在手動指定起點的 ±window_sec 內用波形互相關微調到取樣點精度。"""
    coarse = int(round(offset_sec * sr))
    r = int(window_sec * sr)
    lo = max(0, coarse - r)
    hi = min(len(bgm_mono), coarse + len(clip_mono) + r)
    seg = bgm_mono[lo:hi]
    if len(seg) < len(clip_mono):
        return offset_sec
    lag, _, _ = _ncc_peak(clip_mono, seg)
    return (lo + lag) / sr


def align(clip_mono, bgm_mono, sr, index=None):
    """相容舊介面：回傳 (offset_sec, z)。"""
    cands, _, _ = locate(clip_mono, bgm_mono, sr, index)
    if not cands:
        raise ValueError("找不到剪輯在背景音樂中的位置。")
    return cands[0]["offset"] / sr, cands[0]["z"]


def choose(cands_list, clip_lens, sr, ordered=False, tol_sec=2.0):
    """
    每個剪輯從候選中選一個起點。ordered=False：各自取最佳。
    ordered=True：強制與清單順序相同且不重疊（start_i ≥ 前一個已放入剪輯的結束 − tol），
    以總吻合秒數最大化做動態規劃；放不進去的剪輯略過（回傳 None）而不是硬塞。
    回傳 (chosen list, note)。
    """
    if not ordered:
        return [c[0] if c else None for c in cands_list], ""
    n = len(cands_list)
    # 狀態 = 最後一個已放入的 (clip, cand)，None 表示尚未放入任何剪輯
    states = {None: (0.0, None)}          # key -> (score, prev_key)
    hist = []                             # 每一步的狀態表，用於回溯
    for i, cands in enumerate(cands_list):
        new = dict(states)                # 略過這個剪輯：狀態不變
        for j, v in enumerate(cands or []):
            best = None
            for key, (sc, _) in states.items():
                if key is not None:
                    ip, jp = key
                    vp = cands_list[ip][jp]
                    if v["offset"] < vp["offset"] + clip_lens[ip] - tol_sec * sr:
                        continue
                if best is None or sc > best[0]:
                    best = (sc, key)
            if best is not None:
                tot = best[0] + v["matched_sec"]
                if (i, j) not in new or tot > new[(i, j)][0]:
                    new[(i, j)] = (tot, best[1])
        hist.append(new)
        states = new
    key = max(states, key=lambda k: states[k][0])
    pick = [None] * n
    while key is not None:
        i, j = key
        pick[i] = cands_list[i][j]
        key = hist[i][key][1]
    return pick, "依清單順序、不重疊"


# ---------------------------------------------------------------- mixing
def estimate_gain(clip_mono, bgm_seg_mono):
    """剪輯 ≈ g * 背景 + 人聲；最小平方估計 g。"""
    n = min(len(clip_mono), len(bgm_seg_mono))
    c, b = clip_mono[:n], bgm_seg_mono[:n]
    g = float(np.dot(c, b) / (np.dot(b, b) + 1e-12))
    return max(g, 1e-3)


def limiter(x, thr=0.97, sr=44100, win_ms=30, release_ms=120):
    """簡單峰值限幅：只壓超過門檻的片段，不影響整體音量。"""
    peak = np.abs(x).max(axis=1) if x.ndim == 2 else np.abs(x)
    if peak.max() <= thr:
        return x, 0.0
    w = max(1, int(sr * win_ms / 1000))
    env = maximum_filter1d(peak, w)                    # 前瞻峰值包絡
    a = np.exp(-1.0 / (sr * release_ms / 1000))        # 釋放平滑
    env = np.maximum(env, signal.lfilter([1 - a], [1, -a], env))
    g = np.minimum(1.0, thr / np.maximum(env, 1e-9))
    g = uniform_filter1d(g, w)                         # 增益曲線平滑，避免失真
    g = np.minimum(g, thr / np.maximum(peak, 1e-9)).astype(np.float32)
    return x * g[:, None], float(20 * np.log10(g.min()))


def _two_sided_mean(x, w):
    """回傳 (只看過去 w 個取樣點的平均, 只看未來 w 個取樣點的平均)。"""
    n = len(x)
    cs = np.concatenate([[0.0], np.cumsum(x, dtype=np.float64)])
    i = np.arange(n)
    lo = np.maximum(0, i + 1 - w)
    causal = (cs[i + 1] - cs[lo]) / (i + 1 - lo)
    hi = np.minimum(n, i + w)
    anti = (cs[hi] - cs[i]) / (hi - i)
    return causal.astype(np.float32), anti.astype(np.float32)


def _envelope_from_profile(profile, n, sr):
    """把 verify() 的逐視窗吻合結果展開成每個取樣點的 0~1 包絡（無聲視窗沿用最近的有聲視窗）。"""
    if profile is None or len(profile["t"]) == 0:
        return np.ones(n, np.float32)
    centers = (profile["t"] + profile["win"] / 2) * sr
    vals = profile["matched"].astype(np.float32)
    silent = profile.get("silent")
    if silent is not None and silent.any():
        if silent.all():
            return np.ones(n, np.float32)
        good = np.flatnonzero(~silent)
        for j in np.flatnonzero(silent):
            vals[j] = vals[good[np.argmin(np.abs(good - j))]]
    if len(centers) == 1:
        return np.full(n, vals[0], np.float32)
    mids = (centers[:-1] + centers[1:]) / 2            # 階梯：每個取樣點取最近視窗的值
    m = vals[np.searchsorted(mids, np.arange(n))].astype(np.float32)
    return uniform_filter1d(m, max(1, int(0.3 * sr)))


def merge(clip, bgm, sr, offset, mode="keep", voice_db=0.0, fade_sec=0.15, profile=None, ref=None,
          debug=None):
    """
    把剪輯無縫放進背景音樂。clip, bgm: (n, 2) float32；offset：剪輯起點（取樣點，可含小數、可為負）。
    profile：verify() 的結果，標出剪輯哪些部分真的來自這首歌（None = 全部視為吻合）。
    ref：估計音量比例用的「乾淨原曲」；多個剪輯累積混進同一個 bgm 時傳入原曲，避免被前一個剪輯的人聲干擾。
    mode="keep"  ：原曲音量全程不變。剪輯放大 1/g 倍（g = 剪輯內 BGM 相對原曲的比例），
                   並隨時補回 (1 − k·g(t)) 的原曲——剪輯裡的音樂淡入淡出或空白時，原曲自動補滿，
                   不會出現音量凹陷。voice_db < 0 時改為混回部分原曲以降低人聲。
    mode="duck"  ：剪輯原樣放入（只套用 voice_db），音樂在台詞期間會像電影一樣壓低；
                   剪輯淡出/空白處補回原曲到壓低後的音量。
    剪輯中「不是這首歌」的部分（電影剪接過的音樂）只播剪輯本身、原曲暫時靜音，
    避免兩段不同的音樂打架。兩端皆用等功率交叉淡化。回傳 (mixed, info dict)。
    """
    start = int(np.floor(offset + 0.5))
    delta = float(offset - start)
    if abs(delta) > 0.02:
        clip = _frac_shift(clip, delta)
    n = len(clip)
    out = bgm.copy()
    pad_front = 0
    end = start + n
    if end > len(out):                               # 剪輯超出 BGM 尾端：補零延長
        out = np.concatenate([out, np.zeros((end - len(out), out.shape[1]), np.float32)])
    if start < 0:                                    # 剪輯比 BGM 早開始：前面補零
        pad_front = -start
        out = np.concatenate([np.zeros((pad_front, out.shape[1]), np.float32), out])
        start, end = 0, n

    seg = out[start:end]
    if ref is not None:
        r = np.zeros_like(seg)
        a0 = start - pad_front
        lo, hi = max(0, a0), min(len(ref), a0 + n)
        if hi > lo:
            r[lo - a0:hi - a0] = ref[lo:hi]
        ref_seg = r
    else:
        ref_seg = seg
    cm, sm = clip.mean(1), ref_seg.mean(1)
    # 剪輯內 BGM 相對原曲的比例 g(t)，隨時間變化（0.25 秒視窗的最小平方估計）。
    # 用「只看過去」與「只看未來」兩個估計取較小者：剪輯硬切進來之前、淡出之後都不會高估 g，
    # 估錯的方向永遠是「原曲多補一點」而不是出現凹陷。
    # 分母加上原曲平均能量的 1%：原曲極小聲的地方估計不穩，寧可視為 0（原曲照常播）。
    wg = max(1, int(0.25 * sr))
    e_ref = float(np.mean(sm * sm)) + 1e-12
    num_c, num_a = _two_sided_mean(cm * sm, wg)
    den_c, den_a = _two_sided_mean(sm * sm, wg)
    g_t = np.minimum(num_c / (den_c + 0.01 * e_ref), num_a / (den_a + 0.01 * e_ref))
    g_t = np.clip(g_t, 0.0, 4.0).astype(np.float32)
    m_t = _envelope_from_profile(profile, n, sr)                     # 吻合包絡
    e_c, e_a = _two_sided_mean(cm * cm, max(1, int(0.05 * sr)))      # 剪輯音量包絡（雙向）
    db = 20 * np.log10(np.sqrt(np.minimum(e_c, e_a)) + 1e-9)        # 任一側無聲就算無聲
    s_t = np.clip((SILENCE_DB + 5 - db) / 10, 0, 1).astype(np.float32)  # 無聲 → 1

    msk = m_t > 0.5
    g_ref = float(np.percentile(g_t[msk], 75)) if msk.any() else estimate_gain(cm, sm)
    g_ref = float(np.clip(g_ref, 0.05, 4.0))
    trim = 10 ** (voice_db / 20)
    if mode == "duck":
        k = trim
        fill = k * np.clip(g_ref - g_t, 0, None) * np.clip(1 - g_t / (0.3 * g_ref), 0, 1)
        A = m_t * fill + (1 - m_t) * s_t * k * g_ref
    else:
        k = trim / g_ref
        A = m_t * np.clip(1 - k * g_t, 0, 1) + (1 - m_t) * s_t
        A = np.maximum(A, s_t)                   # 剪輯無聲處一律原曲補滿
    A = uniform_filter1d(A.astype(np.float32), max(1, int(0.01 * sr)))

    nf = max(1, min(int(fade_sec * sr), n // 2))
    t = np.linspace(0, np.pi / 2, nf, dtype=np.float32)
    w = np.ones(n, np.float32)
    w[:nf] = np.sin(t)
    w[-nf:] = np.cos(t)
    # 輸出 = 原曲·(1 − w·(1 − A)) + 剪輯·k·w ：w=0 處完全是原曲
    mixed = seg * (1 - w * (1 - A))[:, None] + clip * (k * w)[:, None]
    if debug is not None:
        debug.update(A=A, g_t=g_t, m_t=m_t, s_t=s_t, w=w, k=k, g_ref=g_ref, seg=seg.copy(),
                     mixed=mixed.copy(), start=start, cm=cm, sm=sm)
    out[start:end] = mixed

    raw_peak = float(np.abs(out).max())
    out, lim_db = limiter(out, sr=sr)
    msec = float(profile["matched_sec"]) if profile else n / sr
    info = dict(start_sec=(start - pad_front) / sr, end_sec=(end - pad_front) / sr,
                pad_front=pad_front, clip_bgm_ratio=g_ref, clip_bgm_db=20 * np.log10(g_ref),
                clip_gain=k, clip_gain_db=20 * np.log10(k), raw_peak=raw_peak,
                limiter_db=lim_db, matched_sec=msec, clip_len=n / sr,
                coverage=min(1.0, msec / (n / sr)))
    return out, info


# ---------------------------------------------------------------- job
def trim_to_match(clip, prof, bgm_mono, sr):
    """只保留剪輯中吻合的那一段（含頭尾各 50 ms 餘量），回傳 (新剪輯, 新 profile)。"""
    a, b = prof["match_span"]
    ia = max(0, int(a * sr) - int(0.05 * sr))
    ib = min(len(clip), int(b * sr) + int(0.05 * sr))
    c = clip[ia:ib]
    p2 = verify(_mono(c), bgm_mono, sr, prof["offset"] + ia)
    p2["z"] = prof.get("z", float("nan"))
    return c, p2


# 決策選項（resolve 回呼可回傳的值）
OPT_KEEP, OPT_TRIM, OPT_SKIP, OPT_BEST = "keep", "trim", "skip", "best"
OPT_LABEL = {OPT_KEEP: "完整保留（起點可能為負、輸出會變長）",
             OPT_TRIM: "只保留吻合的部分",
             OPT_SKIP: "跳過這個 OS",
             OPT_BEST: "忽略清單順序，放在各自最佳位置"}


def find_issues(items, chosen, bgm_len, sr):
    """
    檢查選定結果有沒有異常，回傳 list of dict(index, name, reasons, options, default)。
    異常：找不到位置、依順序放不進去、吻合不到一半、起點在歌曲開始前、結尾超出歌曲。
    """
    issues = []
    for idx, ((name, clip, cands, sp), prof) in enumerate(zip(items, chosen)):
        reasons, options = [], []
        if prof is None:
            if cands:
                reasons.append("依清單順序放不進去（與前後剪輯重疊）")
                options = [OPT_BEST, OPT_SKIP]
            else:
                reasons.append("在整首歌裡找不到位置")
                options = [OPT_SKIP]
        else:
            n = len(clip)
            if prof["coverage"] < GOOD_COVERAGE:
                span = prof.get("match_span")
                s = f"只有 {prof['coverage']*100:.0f}%（{prof['matched_sec']:.0f} 秒）屬於這首歌"
                if span:
                    s += f"：剪輯 {span[0]:.1f}~{span[1]:.1f} 秒 → 歌曲 {fmt_time(prof['offset']/sr + span[0])}"
                reasons.append(s)
            if prof["offset"] < 0:
                reasons.append(f"起點在歌曲開始前 {-prof['offset']/sr:.1f} 秒")
            if prof["offset"] + n > bgm_len:
                reasons.append(f"結尾超出歌曲 {(prof['offset'] + n - bgm_len)/sr:.1f} 秒")
            if reasons:
                options = [OPT_KEEP, OPT_SKIP] + ([OPT_TRIM] if prof.get("match_span") else [])
        if reasons:
            issues.append(dict(index=idx, name=name, reasons=reasons, options=options, default=options[0]))
    return issues


class JobAborted(Exception):
    pass


def run_job(clip_paths, bgm_path, out_dir, sr=44100, mode="keep", voice_db=0.0,
            fade_sec=0.15, manual_offset=None, speed=1.0, ordered=False, merge_all=False,
            fmt="mp3", progress=lambda s: None, on_result=lambda name, info, out_path: None,
            resolve=None):
    """
    完整流程（圖形介面與命令列共用）：解碼 → 每個剪輯找候選起點 → 依設定選定 →
    有異常時呼叫 resolve(issues) 取得決策 → 依清單順序混音 → 輸出。
    resolve 回傳 {index: 選項} 或 None（放棄，拋出 JobAborted）；不給 resolve 則一律採預設選項。
    每完成一個剪輯呼叫 on_result(name, info, out_path)；info 為 None 表示略過，out_path 位置放原因。
    回傳 (results, note)。
    """
    base = os.path.splitext(os.path.basename(bgm_path))[0]
    progress("解碼背景音樂…")
    bgm = decode(bgm_path, sr)
    bgm_mono = bgm.mean(1)
    index = None if manual_offset is not None else BgmIndex(bgm_mono, sr)
    items = []
    total = len(clip_paths)
    for i, cp in enumerate(clip_paths, 1):
        name = os.path.splitext(os.path.basename(cp))[0]
        progress(f"[{i}/{total}] 解碼 {name}…")
        clip = decode(cp, sr)
        if speed != 1.0:
            clip = _speed_change(clip, 1 / speed)
        sp = speed
        if manual_offset is not None:
            off = refine(clip.mean(1), bgm_mono, sr, float(manual_offset)) * sr
            prof = verify(clip.mean(1), bgm_mono, sr, off)
            prof["z"] = float("nan")
            cands = [prof]
        else:
            progress(f"[{i}/{total}] 搜尋 {name} 的對齊位置…")
            cands, clip, sp2 = locate(clip, bgm_mono, sr, index,
                                      progress=lambda s: progress(f"[{i}/{total}] {name}：{s}"))
            sp = sp * sp2
        items.append((name, clip, cands, sp))

    chosen, note = choose([it[2] for it in items], [len(it[1]) for it in items], sr, ordered)
    total_len = sum(len(it[1]) for it in items) / sr
    if total_len > len(bgm) / sr:
        note = (note + "；" if note else "") + \
            f"剪輯總長 {total_len:.0f} 秒超過背景音樂 {len(bgm)/sr:.0f} 秒，必然重疊"

    # 異常 → 詢問怎麼處理
    skipped_reason = {}
    issues = find_issues(items, chosen, len(bgm), sr)
    if issues:
        decisions = resolve(issues) if resolve else {it["index"]: it["default"] for it in issues}
        if decisions is None:
            raise JobAborted("使用者放棄")
        for it in issues:
            idx = it["index"]
            d = decisions.get(idx, it["default"])
            name, clip, cands, sp = items[idx]
            if d == OPT_SKIP:
                chosen[idx] = None
                skipped_reason[idx] = "已跳過：" + "；".join(it["reasons"])
            elif d == OPT_BEST:
                chosen[idx] = cands[0]
                note = (note + "；" if note else "") + f"{name} 忽略順序放在最佳位置"
            elif d == OPT_TRIM and chosen[idx] is not None and chosen[idx].get("match_span"):
                progress(f"{name}：只保留吻合部分…")
                clip2, prof2 = trim_to_match(clip, chosen[idx], bgm_mono, sr)
                items[idx] = (name, clip2, cands, sp)
                chosen[idx] = prof2
                note = (note + "；" if note else "") + f"{name} 只保留吻合部分"
            elif chosen[idx] is None:
                skipped_reason[idx] = "；".join(it["reasons"])

    acc = None
    pad = 0
    if merge_all:
        starts = [p["offset"] for p in chosen if p is not None]
        pad = int(max(0, -min(starts))) if starts else 0
        acc = np.concatenate([np.zeros((pad, bgm.shape[1]), np.float32), bgm]) if pad else bgm.copy()
    results = []
    out_path = ""
    for idx, ((name, clip, cands, sp), prof) in enumerate(zip(items, chosen)):
        if prof is None:
            reason = skipped_reason.get(idx, "找不到位置")
            on_result(name, None, reason)
            results.append((name, None, reason))
            continue
        progress(f"{name} 對齊在 {fmt_time(prof['offset'] / sr)}，混音中…")
        if acc is not None:
            acc, info = merge(clip, acc, sr, prof["offset"] + pad, mode, voice_db, fade_sec, prof,
                              ref=np.concatenate([np.zeros((pad, bgm.shape[1]), np.float32), bgm]) if pad else bgm)
            info["start_sec"] -= pad / sr
            info["end_sec"] -= pad / sr
            out_path = os.path.join(out_dir, f"{base}_合併.{fmt}")
        else:
            mixed, info = merge(clip, bgm, sr, prof["offset"], mode, voice_db, fade_sec, prof)
            out_path = os.path.join(out_dir, f"{base}_{name}.{fmt}")
            progress(f"輸出 {os.path.basename(out_path)}…")
            encode(out_path, mixed, sr)
        info.update(offset_sec=prof["offset"] / sr, confidence=prof.get("z", float("nan")),
                    speed=sp, bgm_len=len(bgm) / sr, match_span=prof.get("match_span"))
        if prof["offset"] < 0:
            note = (note + "；" if note else "") + \
                f"{name} 起點在歌曲開始前 {-prof['offset']/sr:.1f} 秒，輸出檔前面會多出這段"
        on_result(name, info, out_path)
        results.append((name, info, out_path))
    if acc is not None:
        progress("輸出合併檔…")
        encode(out_path, acc, sr)
    return results, note


def process(clip_path, bgm_path, out_path, sr=44100, mode="keep", voice_db=0.0,
            fade_sec=0.15, manual_offset=None, speed=1.0, progress=lambda s: None):
    """單一剪輯的完整流程（相容舊介面）。"""
    out_dir = os.path.dirname(os.path.abspath(out_path))
    results, _ = run_job([clip_path], bgm_path, out_dir, sr, mode, voice_db, fade_sec,
                         manual_offset, speed, fmt=os.path.splitext(out_path)[1].lstrip(".") or "mp3",
                         progress=progress)
    name, info, produced = results[0]
    if info is None:
        raise ValueError("找不到剪輯在背景音樂中的位置。")
    if os.path.abspath(produced) != os.path.abspath(out_path):
        shutil.move(produced, out_path)
    return info


def fmt_time(sec):
    sign = "-" if sec < 0 else ""
    m, s = divmod(abs(sec), 60)
    return f"{sign}{int(m):02d}:{s:06.3f}"
