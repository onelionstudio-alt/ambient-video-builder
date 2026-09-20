#!/usr/bin/env python3
"""A simple desktop window for Ambient Video Builder."""
from __future__ import annotations
import contextlib, io, queue, threading, traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from make_ambient_video import main as build

VIDEO_TYPES = [("Video files", "*.mp4 *.mov *.m4v *.mkv"), ("All files", "*.*")]
AUDIO_TYPES = [("Audio files", "*.wav *.mp3 *.m4a *.aac *.flac *.ogg"), ("All files", "*.*")]

class QueueWriter(io.TextIOBase):
    def __init__(self, q): self.q=q
    def write(self, text):
        if text.strip(): self.q.put(text)
        return len(text)
    def flush(self): pass

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Ambient Video Builder")
        self.geometry("760x640"); self.minsize(680, 560)
        self.clips=[]; self.audio=[]; self.events=queue.Queue(); self.running=False
        self.duration=tk.StringVar(value="30")
        self.preview=tk.BooleanVar(value=False); self.silent=tk.BooleanVar(value=False)
        self.already_looped=tk.BooleanVar(value=True)
        self.output=tk.StringVar(value=str(Path.home()/"Videos"/"ambient_video.mp4"))
        self._ui(); self.after(100,self._read_events)

    def _ui(self):
        root=ttk.Frame(self,padding=16); root.pack(fill="both",expand=True)
        root.columnconfigure(0,weight=1); root.rowconfigure(7,weight=1)
        ttk.Label(root,text="Ambient Video Builder",font=("Segoe UI",18,"bold")).grid(row=0,column=0,sticky="w")
        ttk.Label(root,text="Собирает длинное спокойное видео из коротких клипов. Сначала лучше сделать превью.").grid(row=1,column=0,sticky="w",pady=(2,12))
        clips=ttk.LabelFrame(root,text="Видеоклипы",padding=8); clips.grid(row=2,column=0,sticky="nsew",pady=4); clips.columnconfigure(0,weight=1)
        self.clip_box=tk.Listbox(clips,height=5); self.clip_box.grid(row=0,column=0,rowspan=2,sticky="nsew")
        ttk.Button(clips,text="Добавить видео…",command=self.add_clips).grid(row=0,column=1,padx=(8,0),sticky="ew")
        ttk.Button(clips,text="Убрать выбранное",command=self.remove_clip).grid(row=1,column=1,padx=(8,0),pady=(5,0),sticky="ew")
        audio=ttk.LabelFrame(root,text="Фоновый звук (необязательно, можно добавить несколько слоёв)",padding=8); audio.grid(row=3,column=0,sticky="nsew",pady=4); audio.columnconfigure(0,weight=1)
        self.audio_box=tk.Listbox(audio,height=3); self.audio_box.grid(row=0,column=0,rowspan=2,sticky="nsew")
        ttk.Button(audio,text="Добавить звук…",command=self.add_audio).grid(row=0,column=1,padx=(8,0),sticky="ew")
        ttk.Button(audio,text="Убрать выбранное",command=self.remove_audio).grid(row=1,column=1,padx=(8,0),pady=(5,0),sticky="ew")
        opts=ttk.Frame(root);opts.grid(row=4,column=0,sticky="ew",pady=8)
        ttk.Label(opts,text="Длительность, минут:").pack(side="left")
        ttk.Entry(opts,textvariable=self.duration,width=8).pack(side="left",padx=(7,18))
        ttk.Checkbutton(opts,text="Сделать короткое превью вместо финала",variable=self.preview).pack(side="left")
        ttk.Checkbutton(opts,text="Видео без звука",variable=self.silent).pack(side="left",padx=(18,0))
        ttk.Checkbutton(opts,text="Клипы уже loop",variable=self.already_looped).pack(side="left",padx=(18,0))
        out=ttk.Frame(root);out.grid(row=5,column=0,sticky="ew",pady=4);out.columnconfigure(1,weight=1)
        ttk.Label(out,text="Готовый файл:").grid(row=0,column=0,sticky="w")
        ttk.Entry(out,textvariable=self.output).grid(row=0,column=1,sticky="ew",padx=8)
        ttk.Button(out,text="Куда сохранить…",command=self.choose_output).grid(row=0,column=2)
        buttons=ttk.Frame(root);buttons.grid(row=6,column=0,sticky="ew",pady=10)
        self.go=ttk.Button(buttons,text="Собрать видео",command=self.start);self.go.pack(side="left")
        ttk.Button(buttons,text="Что нужно подготовить?",command=self.help).pack(side="left",padx=8)
        self.status=tk.StringVar(value="Выбери клипы, затем нажми «Собрать видео».")
        ttk.Label(root,textvariable=self.status).grid(row=7,column=0,sticky="nw")
        self.log=tk.Text(root,height=11,wrap="word",state="disabled",background="#f5f5f5");self.log.grid(row=8,column=0,sticky="nsew",pady=(5,0))

    def refresh(self, box, items):
        box.delete(0,"end")
        for p in items: box.insert("end",str(p))
    def add_clips(self):
        for p in filedialog.askopenfilenames(title="Выбери видеоклипы",filetypes=VIDEO_TYPES):
            x=Path(p)
            if x not in self.clips: self.clips.append(x)
        self.refresh(self.clip_box,self.clips)
    def add_audio(self):
        for p in filedialog.askopenfilenames(title="Выбери звук",filetypes=AUDIO_TYPES):
            x=Path(p)
            if x not in self.audio: self.audio.append(x)
        self.refresh(self.audio_box,self.audio)
    def remove_clip(self):
        for i in reversed(self.clip_box.curselection()): self.clips.pop(i)
        self.refresh(self.clip_box,self.clips)
    def remove_audio(self):
        for i in reversed(self.audio_box.curselection()): self.audio.pop(i)
        self.refresh(self.audio_box,self.audio)
    def choose_output(self):
        p=filedialog.asksaveasfilename(title="Сохранить готовое видео",defaultextension=".mp4",filetypes=[("MP4 video","*.mp4")],initialfile="ambient_video.mp4")
        if p:self.output.set(p)
    def help(self):
        messagebox.showinfo("Подготовка","1. Выбери 1–6 спокойных коротких клипов.\n2. По желанию добавь WAV/MP3 с волнами, огнём или птицами.\n3. Для первого раза включи превью.\n4. «Клипы уже loop» оставь включённым для Flow/Kling, если у ролика уже бесшовный конец.\n5. Выключи его только для обычного короткого видео без готового loop.")
    def logline(self,t):
        self.log.configure(state="normal");self.log.insert("end",t);self.log.see("end");self.log.configure(state="disabled")
    def start(self):
        if self.running:return
        if not self.clips:return messagebox.showerror("Нет видео","Добавь хотя бы один видеоклип.")
        try:
            d=float(self.duration.get().replace(",","."))
            if d<=0:raise ValueError
        except ValueError:return messagebox.showerror("Длительность","Укажи положительное число минут, например 30.")
        out=Path(self.output.get()).expanduser()
        overwrite=out.exists()
        if overwrite and not messagebox.askyesno("Файл уже есть",f"Заменить существующий файл после успешной сборки?\n{out}"):return
        # Short previews need frequent changes; a 30-minute video does not.
        # Keeping the long version to 6–10 scenes makes its final montage fast
        # and reliable instead of creating a huge 50+ scene FFmpeg graph.
        if d <= 5:
            min_seg, max_seg = 25, 45
        elif d <= 15:
            min_seg, max_seg = 75, 120
        else:
            min_seg, max_seg = 180, 300
        args=["--clips",*[str(x) for x in self.clips],"--output",str(out),"--duration",str(d),
              "--min-seg",str(min_seg),"--max-seg",str(max_seg)]
        if self.preview.get():args.append("--preview")
        if self.already_looped.get(): args.append("--already-looped")
        if overwrite: args.append("--overwrite")
        if self.silent.get():args += ["--audio-mode","silent"]
        elif self.audio:args += ["--audio",*[str(x) for x in self.audio]]
        else:return messagebox.showerror("Нет звука","Добавь звуковой файл или включи «Видео без звука».")
        self.running=True;self.go.configure(state="disabled");self.status.set("Идёт сборка…")
        threading.Thread(target=self.worker,args=(args,),daemon=True).start()
    def worker(self,args):
        w=QueueWriter(self.events)
        try:
            with contextlib.redirect_stdout(w),contextlib.redirect_stderr(w): code=build(args)
            self.events.put(("done",code))
        except Exception:
            self.events.put(traceback.format_exc());self.events.put(("done",1))
    def _read_events(self):
        try:
            while True:
                x=self.events.get_nowait()
                if isinstance(x,tuple) and x[0]=="done":
                    self.running=False;self.go.configure(state="normal")
                    if x[1]==0:
                        self.status.set("Готово. Файл сохранён рядом с указанным именем.");messagebox.showinfo("Готово","Видео собрано.")
                    else:self.status.set("Сборка остановилась с ошибкой.");messagebox.showerror("Ошибка","Посмотри сообщение внизу окна.")
                else:self.logline(x)
        except queue.Empty:pass
        self.after(100,self._read_events)

if __name__=="__main__": App().mainloop()
