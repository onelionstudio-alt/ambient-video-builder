#!/usr/bin/env python3
"""A simple desktop window for Ambient Video Builder."""
from __future__ import annotations
import contextlib, io, queue, re, threading, traceback
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
        self.geometry("900x780"); self.minsize(760, 680)
        self.clips=[]; self.audio=[]; self.events=queue.Queue(); self.running=False
        self.duration=tk.StringVar(value="30")
        self.resolution=tk.StringVar(value="4K (3840×2160)")
        self.encoder=tk.StringVar(value="Авто — GPU, если доступен")
        self.mode=tk.StringVar(value="Монтаж — обычные клипы (рекомендуется)")
        self.transition=tk.StringVar(value="Спокойное чередование")
        self.preview=tk.BooleanVar(value=False); self.silent=tk.BooleanVar(value=False)
        self.keep_scene_audio=tk.BooleanVar(value=True)
        self.already_looped=tk.BooleanVar(value=True)
        self.progress=tk.DoubleVar(value=0); self.progress_text=tk.StringVar(value="Готов к сборке")
        self.output=tk.StringVar(value=str(Path.home()/"Videos"/"ambient_video.mp4"))
        self._ui(); self.after(100,self._read_events)

    def _ui(self):
        bg="#101827"; card="#182235"; text="#F8FAFC"; muted="#A7B4C8"; accent="#42D3B4"; entry="#0C1321"
        self.configure(background=bg)
        style=ttk.Style(self); style.theme_use("clam")
        style.configure("TFrame",background=bg); style.configure("TLabel",background=bg,foreground=text,font=("Segoe UI",10))
        style.configure("Muted.TLabel",background=bg,foreground=muted,font=("Segoe UI",10))
        style.configure("Title.TLabel",background=bg,foreground=text,font=("Segoe UI Semibold",22))
        style.configure("Card.TLabelframe",background=card,foreground=text,bordercolor="#26334A",relief="flat")
        style.configure("Card.TLabelframe.Label",background=card,foreground=text,font=("Segoe UI Semibold",10))
        style.configure("TButton",background="#26334A",foreground=text,borderwidth=0,padding=(11,7))
        style.map("TButton",background=[("active","#34445F")])
        style.configure("Accent.TButton",background=accent,foreground="#06231F",font=("Segoe UI Semibold",10),padding=(16,9))
        style.map("Accent.TButton",background=[("active","#68E6CB")])
        style.configure("TCheckbutton",background=bg,foreground=text); style.map("TCheckbutton",background=[("active",bg)])
        style.configure("TEntry",fieldbackground=entry,foreground=text,insertcolor=text,bordercolor="#34445F")
        style.configure("TCombobox",fieldbackground=entry,background=entry,foreground=text,arrowcolor=accent)
        style.configure("Accent.Horizontal.TProgressbar",troughcolor="#26334A",background=accent,lightcolor=accent,darkcolor=accent,bordercolor="#26334A")
        root=ttk.Frame(self,padding=20); root.pack(fill="both",expand=True)
        root.columnconfigure(0,weight=1); root.rowconfigure(12,weight=1)
        ttk.Label(root,text="AMBIENT  /  STUDIO",style="Muted.TLabel").grid(row=0,column=0,sticky="w")
        ttk.Label(root,text="Собери спокойный мир",style="Title.TLabel").grid(row=1,column=0,sticky="w",pady=(1,0))
        ttk.Label(root,text="Flow / Kling → плавный монтаж → 4K. Сначала лучше сделать короткое превью.",style="Muted.TLabel").grid(row=2,column=0,sticky="w",pady=(2,14))
        clips=ttk.LabelFrame(root,text="01  ВИДЕОСЦЕНЫ",padding=10,style="Card.TLabelframe"); clips.grid(row=3,column=0,sticky="nsew",pady=4); clips.columnconfigure(0,weight=1)
        self.clip_box=tk.Listbox(clips,height=5); self.clip_box.grid(row=0,column=0,rowspan=2,sticky="nsew")
        ttk.Button(clips,text="Добавить видео…",command=self.add_clips).grid(row=0,column=1,padx=(8,0),sticky="ew")
        ttk.Button(clips,text="Убрать выбранное",command=self.remove_clip).grid(row=1,column=1,padx=(8,0),pady=(5,0),sticky="ew")
        audio=ttk.LabelFrame(root,text="02  ФОН НА ВЕСЬ РОЛИК",padding=10,style="Card.TLabelframe"); audio.grid(row=4,column=0,sticky="nsew",pady=4); audio.columnconfigure(0,weight=1)
        self.audio_box=tk.Listbox(audio,height=3); self.audio_box.grid(row=0,column=0,rowspan=2,sticky="nsew")
        ttk.Button(audio,text="Добавить звук…",command=self.add_audio).grid(row=0,column=1,padx=(8,0),sticky="ew")
        ttk.Button(audio,text="Убрать выбранное",command=self.remove_audio).grid(row=1,column=1,padx=(8,0),pady=(5,0),sticky="ew")
        ttk.Label(audio,text="Например: один 30-минутный прибой. Мурлыканье, огонь и птицы остаются внутри сцен.",style="Muted.TLabel").grid(row=2,column=0,columnspan=2,sticky="w",pady=(8,0))
        montage=ttk.Frame(root);montage.grid(row=5,column=0,sticky="ew",pady=8)
        ttk.Label(montage,text="Режим:").pack(side="left")
        ttk.Combobox(montage,textvariable=self.mode,state="readonly",width=39,
                     values=("Монтаж — обычные клипы (рекомендуется)","Loop — зацикливать каждый клип")).pack(side="left",padx=(7,18))
        ttk.Label(montage,text="Переходы:").pack(side="left")
        ttk.Combobox(montage,textvariable=self.transition,state="readonly",width=24,
                     values=("Спокойное чередование","Белая дымка","Мягкое растворение","Через чёрный")).pack(side="left",padx=7)
        opts=ttk.Frame(root);opts.grid(row=6,column=0,sticky="ew",pady=4)
        ttk.Label(opts,text="Длительность, минут:").pack(side="left")
        ttk.Entry(opts,textvariable=self.duration,width=8).pack(side="left",padx=(7,18))
        ttk.Checkbutton(opts,text="Сделать короткое превью вместо финала",variable=self.preview).pack(side="left")
        ttk.Checkbutton(opts,text="Видео без звука",variable=self.silent).pack(side="left",padx=(18,0))
        ttk.Checkbutton(opts,text="Сохранять звук в сценах",variable=self.keep_scene_audio).pack(side="left",padx=(18,0))
        ttk.Checkbutton(opts,text="Клипы уже loop",variable=self.already_looped).pack(side="left",padx=(18,0))
        quality=ttk.Frame(root);quality.grid(row=7,column=0,sticky="ew",pady=4)
        ttk.Label(quality,text="Разрешение:").pack(side="left")
        ttk.Combobox(quality,textvariable=self.resolution,state="readonly",width=20,
                     values=("4K (3840×2160)","Full HD (1920×1080)")).pack(side="left",padx=(7,18))
        ttk.Label(quality,text="Скорость:").pack(side="left")
        ttk.Combobox(quality,textvariable=self.encoder,state="readonly",width=27,
                     values=("Авто — GPU, если доступен","CPU — максимум совместимости")).pack(side="left",padx=7)
        out=ttk.Frame(root);out.grid(row=8,column=0,sticky="ew",pady=8);out.columnconfigure(1,weight=1)
        ttk.Label(out,text="Готовый файл:").grid(row=0,column=0,sticky="w")
        ttk.Entry(out,textvariable=self.output).grid(row=0,column=1,sticky="ew",padx=8)
        ttk.Button(out,text="Куда сохранить…",command=self.choose_output).grid(row=0,column=2)
        progress=ttk.Frame(root);progress.grid(row=9,column=0,sticky="ew",pady=(6,3));progress.columnconfigure(0,weight=1)
        ttk.Progressbar(progress,variable=self.progress,maximum=100,style="Accent.Horizontal.TProgressbar").grid(row=0,column=0,sticky="ew")
        ttk.Label(progress,textvariable=self.progress_text,style="Muted.TLabel").grid(row=1,column=0,sticky="w",pady=(5,0))
        buttons=ttk.Frame(root);buttons.grid(row=10,column=0,sticky="ew",pady=10)
        self.go=ttk.Button(buttons,text="Собрать видео",command=self.start,style="Accent.TButton");self.go.pack(side="left")
        ttk.Button(buttons,text="Что нужно подготовить?",command=self.help).pack(side="left",padx=8)
        self.status=tk.StringVar(value="Выбери клипы, затем нажми «Собрать видео».")
        ttk.Label(root,textvariable=self.status,style="Muted.TLabel").grid(row=11,column=0,sticky="nw")
        self.log=tk.Text(root,height=10,wrap="word",state="disabled",background=entry,foreground="#D9E2F2",insertbackground=text,relief="flat",padx=10,pady=9);self.log.grid(row=12,column=0,sticky="nsew",pady=(5,0))

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
        messagebox.showinfo("Подготовка","Режим «Монтаж» предназначен для обычных клипов Flow/Kling: они всегда идут вперёд, а программа соединяет их спокойными переходами и повторяет весь большой блок.\n\nПоставь длинный прибой в «Фон на весь ролик». Включённая галка «Сохранять звук в сценах» оставит в готовых клипах локальные эффекты: мурлыканье, треск огня, птиц. Не добавляй прибой внутрь каждой сцены — он уже идёт одним слоем на весь ролик.\n\nРежим «Loop» оставлен только для действительно бесшовных исходников.")
    def logline(self,t):
        self.log.configure(state="normal");self.log.insert("end",t);self.log.see("end");self.log.configure(state="disabled")
        if "[1/4]" in t:
            self.progress.set(14); self.progress_text.set("01 / 04  Подготавливаю сцены")
        elif "клип" in t and "/" in t:
            found=re.search(r"(\d+)\s*/\s*(\d+)",t)
            if found:
                done,total=map(int,found.groups())
                self.progress.set(14+max(0,min(24,24*done/max(1,total))))
        elif "[2/4]" in t:
            self.progress.set(42); self.progress_text.set("02 / 04  Соединяю сцены")
        elif "[3/4]" in t:
            self.progress.set(66); self.progress_text.set("03 / 04  Продлеваю атмосферу")
        elif "[4/4]" in t:
            self.progress.set(84); self.progress_text.set("04 / 04  Смешиваю фон и локальный звук")
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
        if self.resolution.get().startswith("4K"):
            args += ["--width","3840","--height","2160"]
        else:
            args += ["--width","1920","--height","1080"]
        args += ["--encoder", "auto" if self.encoder.get().startswith("Авто") else "cpu"]
        if self.mode.get().startswith("Монтаж"):
            names={"Спокойное чередование":"calm","Белая дымка":"fadewhite","Мягкое растворение":"fade","Через чёрный":"fadeblack"}
            args += ["--mode","montage","--transition",names[self.transition.get()],"--transition-duration","0.8","--montage-rounds","2"]
            if self.keep_scene_audio.get() and not self.silent.get(): args += ["--keep-scene-audio","--scene-audio-gain","0"]
        else:
            args += ["--mode","loop"]
            if self.already_looped.get():args.append("--already-looped")
        if self.preview.get():args.append("--preview")
        if overwrite: args.append("--overwrite")
        if self.silent.get():args += ["--audio-mode","silent"]
        elif self.audio:
            # A continuous sound-bank bed stays behind the local purr/fire
            # already embedded in the scenes.  -12 dB is a safe starting mix.
            args += ["--audio",*[str(x) for x in self.audio],"--audio-gain",*(["-12"]*len(self.audio))]
        else:return messagebox.showerror("Нет звука","Добавь звуковой файл или включи «Видео без звука».")
        self.running=True;self.go.configure(state="disabled");self.status.set("Идёт сборка…")
        self.progress.set(4); self.progress_text.set("Сцены в очереди на сборку")
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
                        self.progress.set(100); self.progress_text.set("Готово — можно смотреть и загружать.")
                        self.status.set("Готово. Файл сохранён рядом с указанным именем.");messagebox.showinfo("Готово","Видео собрано.")
                    else:
                        self.progress_text.set("Сборка остановилась — детали ниже.")
                        self.status.set("Сборка остановилась с ошибкой.");messagebox.showerror("Ошибка","Посмотри сообщение внизу окна.")
                else:self.logline(x)
        except queue.Empty:pass
        self.after(100,self._read_events)

if __name__=="__main__": App().mainloop()
