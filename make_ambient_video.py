#!/usr/bin/env python3
"""Build long ambient videos from short clips with FFmpeg.

Example:
python make_ambient_video.py --clips-dir clips --audio waves.wav fire.wav \
  --output ambient.mp4 --duration 30 --preview --seed 42
"""
from __future__ import annotations
import argparse, json, math, os, random, shutil, subprocess, sys, tempfile, time
from dataclasses import dataclass, asdict
from pathlib import Path

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}

# A PyInstaller build places ffmpeg.exe and ffprobe.exe beside the extracted app.
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    os.environ["PATH"] = str(Path(sys._MEIPASS)) + os.pathsep + os.environ.get("PATH", "")

class BuildError(RuntimeError): pass

def run(cmd, *, capture=False):
    p = subprocess.run(cmd, stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.PIPE, text=True)
    if p.returncode:
        raise BuildError(("Команда FFmpeg завершилась с ошибкой:\n" +
                          " ".join(map(str, cmd)) + "\n\n" + p.stderr[-3500:]))
    return p.stdout if capture else ""

def ffprobe(path: Path) -> dict:
    return json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                           "-of", "json", str(path)], capture=True))

def duration(path: Path, kind: str) -> float:
    data = ffprobe(path)
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == kind), None)
    if not stream: raise BuildError(f"Нет {kind}-потока: {path}")
    value = stream.get("duration") or data.get("format", {}).get("duration")
    try: value = float(value)
    except (TypeError, ValueError): raise BuildError(f"Не удалось определить длительность: {path}")
    if not math.isfinite(value) or value <= 0: raise BuildError(f"Некорректная длительность: {path}")
    return value

def check_tools():
    missing = [x for x in ("ffmpeg", "ffprobe") if not shutil.which(x)]
    if missing: raise BuildError("Не найдены в PATH: " + ", ".join(missing))

def q(s: float, fps: int) -> int: return max(0, round(s * fps))

def esc_concat(p: Path) -> str:
    # concat demuxer escaping: apostrophe is represented as '\\''
    return str(p.resolve()).replace("'", "'\\''")

@dataclass
class Scene:
    index: int
    clip: int
    frames: int
    start_frame: int

class Builder:
    def __init__(self, a):
        self.a = a; self.fps = a.fps; self.warn: list[str] = []; self.actual_seed = a.seed
        self.report: dict = {"version": "1.0", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                             "warnings": self.warn, "settings": vars(a).copy()}

    def normalize(self, src: Path, dst: Path):
        if self.a.scale_mode == "fill":
            geom = f"scale={self.a.width}:{self.a.height}:force_original_aspect_ratio=increase,crop={self.a.width}:{self.a.height}"
        else:
            geom = f"scale={self.a.width}:{self.a.height}:force_original_aspect_ratio=decrease,pad={self.a.width}:{self.a.height}:(ow-iw)/2:(oh-ih)/2:black"
        vf = f"{geom},setsar=1,fps={self.fps},format=yuv420p,setpts=PTS-STARTPTS"
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-map", "0:v:0",
             "-vf", vf, "-an", "-c:v", "libx264", "-preset", "medium", "-crf", str(self.a.crf),
             "-movflags", "+faststart", str(dst)])

    def make_video_loop(self, src: Path, dst: Path, clip_no: int):
        n = round(duration(src, "video") * self.fps)
        if n < 3: raise BuildError(f"Слишком короткий клип после нормализации: {src}")
        requested = q(self.a.crossfade, self.fps)
        f = min(requested, (n - 1) // 2)
        if requested and f != requested: self.warn.append(f"Клип {clip_no}: crossfade уменьшен до {f/self.fps:.3f} с.")
        if f == 0:
            run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-map", "0:v", "-an", "-c:v", "copy", str(dst)])
            return n
        d, c = n / self.fps, f / self.fps
        # Start at frame F. Then the tail is blended into the head; next loop starts at F again.
        graph = (f"[0:v]trim=start_frame={f}:end_frame={n-f},setpts=PTS-STARTPTS[main];"
                 f"[0:v]trim=start_frame={n-f}:end_frame={n},setpts=PTS-STARTPTS[tail];"
                 f"[0:v]trim=start_frame=0:end_frame={f},setpts=PTS-STARTPTS[head];"
                 f"[tail][head]xfade=transition=fade:duration={c:.9f}:offset=0[blend];"
                 f"[main][blend]concat=n=2:v=1:a=0[out]")
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-filter_complex", graph,
             "-map", "[out]", "-an", "-r", str(self.fps), "-c:v", "libx264", "-preset", "medium",
             "-crf", str(self.a.crf), str(dst)])
        return n - f

    def plan(self, nclips: int, target: int) -> list[Scene]:
        rng = random.Random(self.actual_seed); lo, hi = q(self.a.min_seg, self.fps), q(self.a.max_seg, self.fps)
        scene_x = q(self.a.scene_crossfade, self.fps); scenes=[]; used=0; last=-1; order=list(range(nclips))
        while used < target:
            remaining = target - used
            # scenes overlap with prior one, except the first. Allocate visible contribution exactly.
            visible = min(rng.randint(lo, hi), remaining)
            if remaining < lo and scenes: visible = remaining
            rng.shuffle(order)
            idx = next((x for x in order if x != last), order[0]) if nclips > 1 else 0
            frames = visible + (scene_x if scenes else 0)
            scenes.append(Scene(len(scenes), idx, frames, used))
            used += visible; last = idx
        self.report["target_frames"] = target; self.report["scenes"] = [asdict(x) for x in scenes]
        return scenes

    def render_scene(self, loop: Path, dst: Path, frames: int):
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-stream_loop", "-1", "-i", str(loop),
             "-filter:v", f"trim=end_frame={frames},setpts=PTS-STARTPTS", "-an", "-r", str(self.fps),
             "-c:v", "libx264", "-preset", "medium", "-crf", str(self.a.crf), str(dst)])

    def join_scenes(self, scenes: list[Path], dst: Path):
        if len(scenes) == 1: shutil.copy2(scenes[0], dst); return
        x = q(self.a.scene_crossfade, self.fps)
        if x == 0:
            lst = dst.with_suffix(".txt")
            lst.write_text("".join(f"file '{esc_concat(p)}'\n" for p in scenes), encoding="utf-8")
            run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(dst)]); return
        inputs=[]; graph=[]; last="0:v"; cumulative=round(duration(scenes[0], "video")*self.fps)
        for i,p in enumerate(scenes): inputs += ["-i", str(p)]
        for i in range(1, len(scenes)):
            offset=(cumulative-x)/self.fps
            out=f"v{i}"; graph.append(f"[{last}][{i}:v]xfade=transition=fade:duration={x/self.fps:.9f}:offset={offset:.9f}[{out}]")
            cumulative += round(duration(scenes[i], "video")*self.fps)-x; last=out
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", ";".join(graph),
             "-map", f"[{last}]", "-an", "-r", str(self.fps), "-c:v", "libx264", "-preset", "medium", "-crf", str(self.a.crf), str(dst)])

    def make_audio_loop(self, src: Path, dst: Path, no: int):
        d=duration(src,"audio"); c=min(self.a.audio_crossfade, (d-0.03)/2)
        if c <= 0: raise BuildError(f"Слишком короткий аудиофайл: {src}")
        if c < self.a.audio_crossfade: self.warn.append(f"Аудио {no}: crossfade уменьшен до {c:.3f} с.")
        # Starts at C and after tail->head blend starts at C again.
        g=(f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=start={c:.9f}:end={d-c:.9f},asetpts=PTS-STARTPTS[main];"
           f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=start={d-c:.9f}:end={d:.9f},asetpts=PTS-STARTPTS[tail];"
           f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=start=0:end={c:.9f},asetpts=PTS-STARTPTS[head];"
           f"[tail][head]acrossfade=d={c:.9f}:c1=tri:c2=tri[blend];[main][blend]concat=n=2:v=0:a=1[out]")
        run(["ffmpeg","-hide_banner","-loglevel","error","-y","-i",str(src),"-filter_complex",g,"-map","[out]","-c:a","pcm_s16le",str(dst)])

    def final_with_audio(self, video: Path, audio: list[Path], target_s: float, out: Path):
        if not audio:
            if self.a.audio_mode != "silent": raise BuildError("Не указан звук. Добавь --audio или укажи --audio-mode silent.")
            run(["ffmpeg","-hide_banner","-loglevel","error","-y","-i",str(video),"-map","0:v","-c:v","copy","-movflags","+faststart",str(out)]); return
        inputs=["-i",str(video)]; graph=[]
        for i,p in enumerate(audio,1):
            inputs += ["-stream_loop","-1","-i",str(p)]
            graph.append(f"[{i}:a]atrim=end={target_s:.9f},asetpts=PTS-STARTPTS,volume={self.a.audio_gain[i-1]}dB[a{i}]")
        mix="".join(f"[a{i}]" for i in range(1,len(audio)+1))+f"amix=inputs={len(audio)}:normalize=0:duration=longest,alimiter=limit=0.891251,afade=t=in:st=0:d={min(2,target_s/2):.3f},afade=t=out:st={max(0,target_s-min(2,target_s/2)):.3f}:d={min(2,target_s/2):.3f}[aout]"
        graph.append(mix)
        run(["ffmpeg","-hide_banner","-loglevel","error","-y",*inputs,"-filter_complex",";".join(graph),"-map","0:v","-map","[aout]","-c:v","copy","-c:a","aac","-b:a","192k","-ar","48000","-shortest","-movflags","+faststart",str(out)])

    def validate_final(self, out: Path, target_frames: int):
        data=ffprobe(out); vs=next(s for s in data["streams"] if s["codec_type"]=="video")
        frames=int(vs.get("nb_frames") or 0); got=round(float(vs.get("duration",0))*self.fps)
        if abs(got-target_frames)>1: raise BuildError(f"Финальная длительность неверна: {got} кадров вместо {target_frames}")
        run(["ffmpeg","-hide_banner","-loglevel","error","-v","error","-i",str(out),"-f","null","-"])
        self.report["final"]={"path":str(out),"video_frames":frames,"duration_seconds":float(vs.get("duration",0))}

    def build(self, clips: list[Path], audios: list[Path]):
        target=q(self.a.duration*60,self.fps); tmp_root=Path(self.a.work_dir) if self.a.work_dir else None
        if self.a.dry_run:
            self.plan(len(clips), target)
            print(json.dumps(self.report,ensure_ascii=False,indent=2))
            return
        with tempfile.TemporaryDirectory(dir=tmp_root, prefix="ambient_") as td:
            td=Path(td); norm=[]; loops=[]
            print("[1/5] Нормализация клипов")
            for i,c in enumerate(clips):
                n=td/f"norm_{i}.mp4"; self.normalize(c,n); norm.append(n)
            print("[2/5] Создание циклов")
            for i,n in enumerate(norm):
                l=td/f"loop_{i}.mp4"; self.make_video_loop(n,l,i+1); loops.append(l)
            scenes=self.plan(len(loops),target)
            print(f"[3/5] Рендер сцен: {len(scenes)}")
            rendered=[]
            for s in scenes:
                p=td/f"scene_{s.index:03d}.mp4"; self.render_scene(loops[s.clip],p,s.frames); rendered.append(p)
            video=td/"video.mp4"; print("[4/5] Монтаж видео"); self.join_scenes(rendered,video)
            if self.a.preview:
                out=Path(self.a.output); prev=out.with_name(out.stem+"_preview"+out.suffix)
                # Preview contains final plan's first 20 seconds, enough to check format and transitions.
                run(["ffmpeg","-hide_banner","-loglevel","error","-y","-i",str(video),"-t","20","-c:v","libx264","-crf",str(self.a.crf),str(prev)])
                print("Превью:",prev); return
            print("[5/5] Сборка звука и экспорт")
            al=[]
            for i,a in enumerate(audios):
                p=td/f"audio_loop_{i}.wav"; self.make_audio_loop(a,p,i+1); al.append(p)
            out=Path(self.a.output); temp=out.with_name(out.stem+".partial"+out.suffix)
            self.final_with_audio(video,al,target/self.fps,temp); self.validate_final(temp,target); os.replace(temp,out)
            report_path=out.with_name(out.stem+"_report.json"); report_path.write_text(json.dumps(self.report,ensure_ascii=False,indent=2),encoding="utf-8")
            print("Готово:",out); print("Отчёт:",report_path)

def parse_args(argv=None):
    p=argparse.ArgumentParser(description="Собрать длинное ambient-видео")
    src=p.add_mutually_exclusive_group(required=True); src.add_argument("--clips",nargs="+"); src.add_argument("--clips-dir")
    p.add_argument("--output",required=True); p.add_argument("--duration",type=float,default=30)
    p.add_argument("--min-seg",type=float,default=120); p.add_argument("--max-seg",type=float,default=300)
    p.add_argument("--crossfade",type=float,default=1); p.add_argument("--scene-crossfade",type=float,default=2)
    p.add_argument("--audio",nargs="*"); p.add_argument("--audio-mode",choices=("external","silent"),default="external")
    p.add_argument("--audio-crossfade",type=float,default=2); p.add_argument("--audio-gain",nargs="*",type=float)
    p.add_argument("--width",type=int,default=1920);p.add_argument("--height",type=int,default=1080);p.add_argument("--fps",type=int,default=30)
    p.add_argument("--scale-mode",choices=("fit","fill"),default="fit");p.add_argument("--crf",type=int,default=18);p.add_argument("--seed",type=int)
    p.add_argument("--preview",action="store_true");p.add_argument("--dry-run",action="store_true");p.add_argument("--work-dir");p.add_argument("--overwrite",action="store_true");return p.parse_args(argv)

def main(argv=None):
    try:
        a=parse_args(argv); check_tools()
        if a.width<=0 or a.height<=0 or a.width%2 or a.height%2 or a.fps<=0: raise BuildError("Ширина и высота должны быть положительными чётными, FPS — положительным.")
        if a.duration<=0 or a.min_seg<=0 or a.max_seg<=0 or a.min_seg>a.max_seg: raise BuildError("Проверь duration, min-seg и max-seg.")
        if min(a.crossfade,a.scene_crossfade,a.audio_crossfade)<0: raise BuildError("Crossfade не может быть отрицательным.")
        output_resolved = Path(a.output).resolve()
        clips=sorted(Path(a.clips_dir).iterdir()) if a.clips_dir else [Path(x) for x in a.clips]
        clips=[x for x in clips if x.suffix.lower() in VIDEO_EXT and x.resolve() != output_resolved]
        if not clips: raise BuildError("Не найдено ни одного видеоклипа.")
        for x in clips:
            if not x.is_file(): raise BuildError(f"Нет файла: {x}")
        audios=[Path(x) for x in (a.audio or [])]
        for x in audios:
            if not x.is_file(): raise BuildError(f"Нет аудиофайла: {x}")
        if a.audio_gain is None: a.audio_gain=[0.0]*len(audios)
        if len(a.audio_gain)!=len(audios): raise BuildError("Число --audio-gain должно совпадать с числом аудиофайлов.")
        out=Path(a.output)
        if out.exists() and not a.overwrite: raise BuildError(f"Файл уже существует: {out}. Выбери новое имя или укажи --overwrite.")
        if a.seed is None: a.seed=random.SystemRandom().randint(0,2**31-1)
        Builder(a).build(clips,audios)
    except BuildError as e:
        print("Ошибка:",e,file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nОтменено пользователем.",file=sys.stderr)
        return 130
    return 0
if __name__=="__main__": sys.exit(main())
