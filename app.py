"""
OSxBGM — 把電影台詞剪輯無縫融合回完整背景音樂
執行：python app.py   （需要 numpy、scipy 與 ffmpeg）
"""
import os
import sys
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import core

APP_TITLE = "OSxBGM — 台詞 × 背景音樂 融合器"
AUDIO_TYPES = [("音訊檔", "*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.wma"), ("所有檔案", "*.*")]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("860x640")
        self.minsize(760, 560)
        try:
            self.tk.call("tk", "scaling", 1.25)
        except tk.TclError:
            pass
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        self.clips = []
        self.bgm = tk.StringVar()
        self.out_dir = tk.StringVar()
        s = core.get_ini_section("settings")           # 上次的設定
        self.mode = tk.StringVar(value=s.get("mode", "keep"))
        self.voice_db = tk.DoubleVar(value=float(s.get("voice_db", -3.0)))
        self.speed = tk.StringVar(value=s.get("speed", "1.0"))
        self.fade_ms = tk.IntVar(value=int(float(s.get("fade_ms", 150))))
        self.merge_all = tk.BooleanVar(value=s.get("merge_all", "1") == "1")
        self.ordered = tk.BooleanVar(value=s.get("ordered", "1") == "1")
        self.manual = tk.StringVar(value="")
        self.fmt = tk.StringVar(value=s.get("fmt", "mp3"))
        self.ffmpeg = tk.StringVar(value=core.get_ini("ffmpeg", "ffmpeg"))
        self.status = tk.StringVar(value="請選擇台詞剪輯與背景音樂")
        self.running = False
        self._build()

    # ------------------------------------------------------------ UI
    def _build(self):
        pad = dict(padx=8, pady=4)
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        # 輸入 1：台詞剪輯
        f1 = ttk.LabelFrame(root, text="輸入 1：台詞剪輯（可多選，長度不限；清單順序 = 混音順序）")
        f1.pack(fill="x", **pad)
        lb_frame = ttk.Frame(f1)
        lb_frame.pack(fill="x", padx=6, pady=4)
        self.lb = tk.Listbox(lb_frame, height=4, selectmode="extended")
        self.lb.pack(side="left", fill="x", expand=True)
        sb = ttk.Scrollbar(lb_frame, command=self.lb.yview)
        sb.pack(side="left", fill="y")
        self.lb.config(yscrollcommand=sb.set)
        bf = ttk.Frame(lb_frame)
        bf.pack(side="left", padx=4)
        ttk.Button(bf, text="加入…", command=self.add_clips).pack(fill="x")
        ttk.Button(bf, text="移除", command=self.remove_clips).pack(fill="x", pady=2)
        ttk.Button(bf, text="清空", command=self.clear_clips).pack(fill="x")

        # 輸入 2：背景音樂
        f2 = ttk.LabelFrame(root, text="輸入 2：完整背景音樂")
        f2.pack(fill="x", **pad)
        ttk.Entry(f2, textvariable=self.bgm).pack(side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(f2, text="瀏覽…", command=self.pick_bgm).pack(side="left", padx=6)

        # 輸出
        f3 = ttk.LabelFrame(root, text="輸出")
        f3.pack(fill="x", **pad)
        r = ttk.Frame(f3); r.pack(fill="x", padx=6, pady=4)
        ttk.Label(r, text="資料夾：").pack(side="left")
        ttk.Entry(r, textvariable=self.out_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="瀏覽…", command=self.pick_out).pack(side="left", padx=6)
        ttk.Label(r, text="格式：").pack(side="left")
        ttk.Combobox(r, textvariable=self.fmt, values=["mp3", "wav", "flac"],
                     width=5, state="readonly").pack(side="left")
        ttk.Checkbutton(f3, text="把所有剪輯合併進同一個輸出檔（否則每個剪輯各輸出一個檔案）",
                        variable=self.merge_all).pack(anchor="w", padx=6)
        ttk.Checkbutton(f3, text="強制依清單順序：各剪輯在背景音樂中的先後與清單相同、彼此不重疊（你確定順序時勾選，可排除誤判）",
                        variable=self.ordered).pack(anchor="w", padx=6, pady=(0, 4))

        # ffmpeg
        f6 = ttk.LabelFrame(root, text="ffmpeg 路徑（留空＝自動尋找：程式資料夾 → 內嵌 → PATH；設定會存到 OSxBGM.ini）")
        f6.pack(fill="x", **pad)
        ttk.Entry(f6, textvariable=self.ffmpeg).pack(side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(f6, text="瀏覽…", command=self.pick_ffmpeg).pack(side="left", padx=(0, 4))
        ttk.Button(f6, text="測試", command=self.test_ffmpeg).pack(side="left", padx=(0, 6))

        # 參數
        f4 = ttk.LabelFrame(root, text="融合方式")
        f4.pack(fill="x", **pad)
        ttk.Radiobutton(f4, text="保持原曲音量（推薦）：音樂全程維持原本音量，人聲以電影原本的人聲/音樂比例疊上",
                        variable=self.mode, value="keep").pack(anchor="w", padx=6)
        ttk.Radiobutton(f4, text="電影式壓低：剪輯原樣放入，台詞期間音樂會像電影裡一樣變小聲",
                        variable=self.mode, value="duck").pack(anchor="w", padx=6)
        r = ttk.Frame(f4); r.pack(fill="x", padx=6, pady=4)
        ttk.Label(r, text="人聲音量微調").pack(side="left")
        s = ttk.Scale(r, from_=-12, to=6, variable=self.voice_db, length=160,
                      command=lambda v: self.voice_db.set(round(float(v), 1)))
        s.pack(side="left", padx=4)
        ttk.Label(r, textvariable=self.voice_db, width=5).pack(side="left")
        ttk.Label(r, text="dB    交叉淡化").pack(side="left")
        ttk.Spinbox(r, from_=10, to=1000, increment=10, textvariable=self.fade_ms, width=6).pack(side="left", padx=4)
        ttk.Label(r, text="ms").pack(side="left")
        r = ttk.Frame(f4); r.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(r, text="手動指定起點(秒，留空＝自動對齊)").pack(side="left")
        ttk.Entry(r, textvariable=self.manual, width=9).pack(side="left", padx=4)
        ttk.Label(r, text="    剪輯速度倍率(PAL 影片填 1.04167)").pack(side="left")
        ttk.Entry(r, textvariable=self.speed, width=9).pack(side="left", padx=4)

        # 執行
        r = ttk.Frame(root); r.pack(fill="x", **pad)
        self.btn = ttk.Button(r, text="▶ 開始對齊並融合", command=self.start)
        self.btn.pack(side="left")
        self.pb = ttk.Progressbar(r, mode="indeterminate", length=200)
        self.pb.pack(side="left", padx=10)
        ttk.Label(r, textvariable=self.status).pack(side="left")

        # 結果表
        f5 = ttk.LabelFrame(root, text="結果：剪輯從背景音樂的哪個時間點插入")
        f5.pack(fill="both", expand=True, **pad)
        cols = ("clip", "start", "end", "sec", "match", "conf", "ratio", "out")
        self.tree = ttk.Treeview(f5, columns=cols, show="headings", height=6)
        for c, t, w in (("clip", "剪輯", 140), ("start", "插入點", 80), ("end", "結束", 80),
                        ("sec", "插入點(秒)", 80), ("match", "吻合", 230), ("conf", "信心", 50),
                        ("ratio", "剪輯內BGM音量", 95), ("out", "輸出檔", 140)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center" if c not in ("clip", "out") else "w")
        self.tree.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        sb = ttk.Scrollbar(f5, command=self.tree.yview); sb.pack(side="left", fill="y")
        self.tree.config(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", self.open_selected)
        ttk.Label(root, text="雙擊結果列可直接播放輸出檔。「吻合」= 剪輯中有多少秒真的來自這首歌；"
                             "⚠ 表示大半不是（電影剪接過音樂），該部分只播剪輯本身。",
                  foreground="#555").pack(anchor="w", padx=8)

    # ------------------------------------------------------- actions
    def add_clips(self):
        paths = filedialog.askopenfilenames(title="選擇台詞剪輯", filetypes=AUDIO_TYPES)
        for p in paths:
            if p not in self.clips:
                self.clips.append(p)
                self.lb.insert("end", os.path.basename(p))
        self._guess_out()

    def remove_clips(self):
        for i in reversed(self.lb.curselection()):
            self.lb.delete(i); del self.clips[i]

    def clear_clips(self):
        self.lb.delete(0, "end"); self.clips.clear()

    def pick_bgm(self):
        p = filedialog.askopenfilename(title="選擇完整背景音樂", filetypes=AUDIO_TYPES)
        if p:
            self.bgm.set(p); self._guess_out()

    def pick_ffmpeg(self):
        p = filedialog.askopenfilename(title="選擇 ffmpeg.exe",
                                       filetypes=[("ffmpeg", "ffmpeg.exe ffmpeg"), ("所有檔案", "*.*")])
        if p:
            self.ffmpeg.set(p)
            self.save_ffmpeg()

    def save_ffmpeg(self):
        try:
            core.set_ini("ffmpeg", "ffmpeg", self.ffmpeg.get().strip())
        except OSError as e:
            messagebox.showwarning(APP_TITLE, f"無法寫入 {core.ini_path()}：{e}")

    def save_settings(self):
        try:
            core.set_ini_many("settings", dict(
                mode=self.mode.get(), voice_db=self.voice_db.get(), speed=self.speed.get().strip() or "1.0",
                fade_ms=self.fade_ms.get(), merge_all=int(self.merge_all.get()),
                ordered=int(self.ordered.get()), fmt=self.fmt.get()))
        except (OSError, tk.TclError):
            pass

    def test_ffmpeg(self):
        self.save_ffmpeg()
        try:
            p = core.find_ffmpeg()
            ver = core.ffmpeg_version(p)
            messagebox.showinfo(APP_TITLE, f"OK：{p}\n{ver}")
        except Exception as e:
            messagebox.showerror(APP_TITLE, str(e))

    def pick_out(self):
        d = filedialog.askdirectory(title="選擇輸出資料夾")
        if d:
            self.out_dir.set(d)

    def _guess_out(self):
        if not self.out_dir.get():
            src = self.bgm.get() or (self.clips[0] if self.clips else "")
            if src:
                self.out_dir.set(os.path.dirname(src))

    def open_selected(self, _=None):
        sel = self.tree.selection()
        if sel:
            p = self.tree.item(sel[0], "values")[-1]
            if os.path.isfile(p):
                os.startfile(p) if os.name == "nt" else os.system(f'xdg-open "{p}"')

    def set_status(self, s):
        self.after(0, self.status.set, s)

    def start(self):
        if self.running:
            return
        if not self.clips:
            messagebox.showwarning(APP_TITLE, "請先加入至少一個台詞剪輯。"); return
        if not os.path.isfile(self.bgm.get()):
            messagebox.showwarning(APP_TITLE, "請選擇完整背景音樂檔。"); return
        if not os.path.isdir(self.out_dir.get()):
            messagebox.showwarning(APP_TITLE, "請選擇存在的輸出資料夾。"); return
        manual = None
        if self.manual.get().strip():
            try:
                manual = float(self.manual.get())
            except ValueError:
                messagebox.showwarning(APP_TITLE, "手動起點請輸入秒數，例如 12.5"); return
            if len(self.clips) > 1:
                messagebox.showwarning(APP_TITLE, "手動指定起點只適用於單一剪輯。"); return
        try:
            speed = float(self.speed.get() or 1.0)
            if not 0.5 < speed < 2.0:
                raise ValueError
        except ValueError:
            messagebox.showwarning(APP_TITLE, "速度倍率請輸入 0.5~2 之間的數字，例如 1.04167"); return
        self.save_ffmpeg()
        self.save_settings()
        try:
            core.find_ffmpeg()
        except FileNotFoundError as e:
            messagebox.showerror(APP_TITLE, str(e)); return
        self.running = True
        self.btn.config(state="disabled"); self.pb.start(12)
        self.tree.delete(*self.tree.get_children())
        threading.Thread(target=self._work, args=(manual, speed), daemon=True).start()

    def _work(self, manual, speed):
        try:
            _, note = core.run_job(
                list(self.clips), self.bgm.get(), self.out_dir.get(), sr=44100,
                mode=self.mode.get(), voice_db=self.voice_db.get(), fade_sec=self.fade_ms.get() / 1000,
                manual_offset=manual, speed=speed, ordered=self.ordered.get(),
                merge_all=self.merge_all.get(), fmt=self.fmt.get(),
                progress=self.set_status,
                on_result=lambda name, info, out_path: self.after(0, self._add_row, name, info, out_path),
                resolve=self._resolve_issues)
            self.set_status("完成 ✔" + (f"（{note}）" if note else ""))
        except core.JobAborted:
            self.set_status("已放棄")
        except Exception as e:
            traceback.print_exc()
            self.set_status("發生錯誤")
            self.after(0, messagebox.showerror, APP_TITLE, str(e))
        finally:
            self.running = False
            self.after(0, self.pb.stop)
            self.after(0, lambda: self.btn.config(state="normal"))

    def _resolve_issues(self, issues):
        """在工作執行緒中被呼叫：到主執行緒開對話框，等使用者決定後回傳 {index: 選項} 或 None。"""
        done = threading.Event()
        box = {}
        self.after(0, self._issue_dialog, issues, box, done)
        done.wait()
        return box.get("decisions")

    def _issue_dialog(self, issues, box, done):
        win = tk.Toplevel(self)
        win.title("發現異常，請選擇處理方式")
        win.transient(self)
        win.grab_set()
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="以下剪輯的對齊結果有異常，請決定每一個要怎麼處理：",
                  font=("", 10, "bold")).pack(anchor="w", pady=(0, 8))
        vars_ = {}
        for it in issues:
            lf = ttk.LabelFrame(frm, text=it["name"])
            lf.pack(fill="x", pady=4)
            for r in it["reasons"]:
                ttk.Label(lf, text="• " + r, foreground="#a00").pack(anchor="w", padx=8)
            v = tk.StringVar(value=it["default"])
            vars_[it["index"]] = v
            for opt in it["options"]:
                ttk.Radiobutton(lf, text=core.OPT_LABEL[opt], variable=v, value=opt).pack(anchor="w", padx=20)

        def finish(decisions):
            box["decisions"] = decisions
            win.grab_release()
            win.destroy()
            done.set()

        bf = ttk.Frame(frm)
        bf.pack(fill="x", pady=(10, 0))
        ttk.Button(bf, text="繼續", command=lambda: finish({k: v.get() for k, v in vars_.items()})).pack(side="right")
        ttk.Button(bf, text="放棄全部", command=lambda: finish(None)).pack(side="right", padx=8)
        win.protocol("WM_DELETE_WINDOW", lambda: finish(None))
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _add_row(self, name, info, out_path):
        if info is None:
            self.tree.insert("", "end", values=(name, "—", "—", "—", (out_path or "找不到位置") + " ⚠", "", "", ""))
            return
        conf = info["confidence"]
        conf_s = "手動" if conf != conf else f"{conf:.0f}"
        cov = info["coverage"]
        match_s = f"{info['matched_sec']:.0f}/{info['clip_len']:.0f} 秒 {cov*100:.0f}%" + ("" if cov >= 0.5 else " ⚠")
        span = info.get("match_span")
        if span and cov < 0.9:
            match_s += (f"（剪輯 {span[0]:.1f}~{span[1]:.1f}s → 歌曲 "
                        f"{core.fmt_time(info['start_sec'] + span[0])}）")
        if info.get("speed", 1.0) != 1.0:
            match_s += f" ×{info['speed']:.4f}"
        self.tree.insert("", "end", values=(
            name, core.fmt_time(info["start_sec"]), core.fmt_time(info["end_sec"]),
            f"{info['start_sec']:.3f}", match_s, conf_s, f"{info['clip_bgm_db']:+.1f} dB", out_path))


def main():
    try:
        app = App()
    except Exception:
        # 連視窗都開不起來：寫 log 並盡量用對話框顯示
        log = os.path.join(core.app_dir(), "OSxBGM_error.log")
        with open(log, "a", encoding="utf-8") as f:
            f.write(traceback.format_exc() + "\n")
        try:
            tk.Tk().withdraw()
            messagebox.showerror(APP_TITLE, f"啟動失敗，詳細錯誤已寫到：\n{log}\n\n{traceback.format_exc()[-800:]}")
        except Exception:
            pass
        raise
    try:
        core.find_ffmpeg()
    except FileNotFoundError as e:
        app.after(200, lambda: messagebox.showwarning(APP_TITLE, str(e)))
    app.mainloop()


if __name__ == "__main__":
    main()
