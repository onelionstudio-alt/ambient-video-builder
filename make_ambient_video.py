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
    # FFmpeg on Windows sometimes writes useful diagnostics to stdout, and its
    # Unicode output must never hide the actual error from the GUI.
    # A GUI application has no parent console.  Without this flag Windows
    # creates an empty black window for every FFmpeg invocation.
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    p = subprocess.run(cmd, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True, errors="replace",
                       creationflags=creationflags)
    if p.returncode:
        detail = "\n".join(x for x in (p.stderr.strip(), p.stdout.strip()) if x)
        raise BuildError(("Команда FFmpeg завершилась с ошибкой:\n" +
                          " ".join(map(str, cmd)) + "\n\n" +
                          (detail[-3500:] or "FFmpeg не вернул текст ошибки. "
                           "В этой сборке будет использован запасной видеокодек.")))
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
        self.report: dict = {"version": "2.0", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                             "warnings": self.warn, "settings": vars(a).copy()}
        self.encoder, self.encoder_args = self.select_encoder()
        self.report["encoder"] = self.encoder

    def encoder_available(self, name: str, args: list[str]) -> bool:
        """An encoder can be listed by FFmpeg even when matching hardware is absent."""
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
               "-i", "color=black:s=128x72:r=24:d=0.1", "-frames:v", "1",
               "-c:v", name, *args, "-f", "null", "-"]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              creationflags=creationflags).returncode == 0

    def select_encoder(self) -> tuple[str, list[str]]:
        cpu = ("libx264", ["-preset", self.a.cpu_preset, "-crf", str(self.a.crf)])
        candidates = {
            "nvenc": ("h264_nvenc", ["-preset", "p4", "-tune", "hq", "-rc", "vbr", "-cq", str(self.a.gpu_quality), "-b:v", "0"]),
            "qsv": ("h264_qsv", ["-preset", "medium", "-global_quality", str(self.a.gpu_quality)]),
            "amf": ("h264_amf", ["-quality", "balanced", "-rc", "cqp", "-qp_i", str(self.a.gpu_quality), "-qp_p", str(self.a.gpu_quality + 2)]),
        }
        if self.a.encoder == "cpu": return cpu
        wanted = list(candidates) if self.a.encoder == "auto" else [self.a.encoder]
        for key in wanted:
            name, args = candidates[key]
            if self.encoder_available(name, args):
                return name, args
        if self.a.encoder != "auto":
            self.warn.append(f"Кодировщик {self.a.encoder} недоступен; используется CPU (libx264).")
        else:
            self.warn.append("Совместимый GPU-кодировщик не найден; используется CPU (libx264).")
        return cpu

    def encode_args(self) -> list[str]:
        return ["-c:v", self.encoder, *self.encoder_args, "-pix_fmt", "yuv420p"]

    def normalize(self, src: Path, dst: Path, keep_audio=False):
        """Put every clip on one video (and, when requested, audio) format.

        Montage transitions need an audio stream on every input.  A silent
        stereo track for a clip without sound is much safer than making the
        whole render fail just because one Flow clip happened to be mute.
        """
        if self.a.scale_mode == "fill":
            geom = f"scale={self.a.width}:{self.a.height}:force_original_aspect_ratio=increase,crop={self.a.width}:{self.a.height}"
        else:
            geom = f"scale={self.a.width}:{self.a.height}:force_original_aspect_ratio=decrease,pad={self.a.width}:{self.a.height}:(ow-iw)/2:(oh-ih)/2:black"
        vf = f"{geom},setsar=1,fps={self.fps},format=yuv420p,setpts=PTS-STARTPTS"
        base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
        output = ["-map", "0:v:0", "-vf", vf]
        if keep_audio:
            has_audio = any(s.get("codec_type") == "audio" for s in ffprobe(src).get("streams", []))
            if has_audio:
                output += ["-map", "0:a:0", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest"]
            else:
                base += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
                output += ["-map", "1:a:0", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest"]
        else:
            output += ["-an"]
        # libx264 is preferred.  A few Windows FFmpeg bundles omit it; retry
        # with the native Media Foundation encoder rather than just failing.
        try:
            run([*base, *output, "-c:v", "libx264", "-preset", "medium", "-crf", str(self.a.crf),
                 "-movflags", "+faststart", str(dst)])
        except BuildError as primary:
            self.warn.append("libx264 недоступен или не запустился; повторная попытка через Windows H.264 encoder.")
            try:
                run([*base, *output, "-c:v", "h264_mf", "-b:v", "12M", "-movflags", "+faststart", str(dst)])
            except BuildError as fallback:
                raise BuildError(f"Не удалось нормализовать клип:\n{src}\n\n"
                                 f"Первая попытка (libx264):\n{primary}\n\n"
                                 f"Запасная попытка (h264_mf):\n{fallback}") from fallback

    def make_video_loop(self, src: Path, dst: Path, clip_no: int):
        n = round(duration(src, "video") * self.fps)
        if n < 3: raise BuildError(f"Слишком короткий клип после нормализации: {src}")
        if self.a.already_looped:
            run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                 "-map", "0:v", "-an", "-c:v", "copy", str(dst)])
            return n
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
        scene_x = q(self.a.scene_crossfade, self.fps); scenes=[]; used=0; last=-1; order=list(range(nclips)); bag=[]
        while used < target:
            remaining = target - used
            # scenes overlap with prior one, except the first. Allocate visible contribution exactly.
            visible = min(rng.randint(lo, hi), remaining)
            if remaining < lo and scenes: visible = remaining
            # Use every selected clip before starting a new shuffled round.
            # This prevents a clip from silently disappearing from the final video.
            if not bag:
                bag = order[:]
                rng.shuffle(bag)
                if nclips > 1 and bag[0] == last:
                    bag[0], bag[1] = bag[1], bag[0]
            idx = bag.pop(0)
            frames = visible + (scene_x if scenes else 0)
            scenes.append(Scene(len(scenes), idx, frames, used))
            used += visible; last = idx
        self.report["target_frames"] = target; self.report["scenes"] = [asdict(x) for x in scenes]
        return scenes

    def plan_montage(self, clips: list[Path], target: int | None = None) -> list[Scene]:
        """Play every source forward; never loop or reverse an individual clip."""
        rng = random.Random(self.actual_seed); x = q(self.a.transition_duration, self.fps)
        lengths = [round(duration(p, "video") * self.fps) for p in clips]
        if any(n <= x + 2 for n in lengths):
            raise BuildError("Один из клипов короче перехода. Уменьши длительность перехода.")
        scenes=[]; visible=0; last=-1
        rounds = 1 if target is not None else self.a.montage_rounds
        while len(scenes) < len(clips) * rounds or (target is not None and visible < target):
            order=list(range(len(clips))); rng.shuffle(order)
            if len(order)>1 and order[0]==last: order[0],order[1]=order[1],order[0]
            for idx in order:
                frames=lengths[idx]
                contribution=frames-(x if scenes else 0)
                if target is not None and visible+contribution>target:
                    contribution=target-visible
                    frames=contribution+(x if scenes else 0)
                scenes.append(Scene(len(scenes),idx,frames,visible))
                visible += contribution; last=idx
                if target is not None and visible>=target: break
            if target is not None and visible>=target: break
        self.report["target_frames"] = target or visible
        self.report["scenes"] = [asdict(s) for s in scenes]
        return scenes

    def montage_transition(self, index: int) -> str:
        if self.a.transition == "calm":
            # Mostly invisible dissolves, with an occasional white veil like
            # the successful CapCut test. Avoid flashy motion transitions.
            return "fadewhite" if index % 4 == 0 else "fade"
        return self.a.transition

    def render_montage(self, clips: list[Path], scenes: list[Scene], dst: Path, edge_fade=True):
        inputs=[]; graph=[]; x=q(self.a.transition_duration,self.fps)
        for i,s in enumerate(scenes):
            inputs += ["-i",str(clips[s.clip])]
            graph.append(f"[{i}:v]trim=end_frame={s.frames},setpts=PTS-STARTPTS[s{i}]")
            if self.a.keep_scene_audio:
                graph.append(f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=end={s.frames/self.fps:.9f},asetpts=PTS-STARTPTS[a{i}]")
        last="s0"; cumulative=scenes[0].frames
        for i in range(1,len(scenes)):
            offset=(cumulative-x)/self.fps; out=f"m{i}"
            transition=self.montage_transition(i)
            graph.append(f"[{last}][s{i}]xfade=transition={transition}:duration={x/self.fps:.9f}:offset={offset:.9f}[{out}]")
            cumulative += scenes[i].frames-x; last=out
        mapped=last
        if edge_fade:
            edge=min(0.65,max(0.15,cumulative/self.fps/4))
            end=max(0,cumulative/self.fps-edge)
            graph.append(f"[{last}]fade=t=in:st=0:d={edge:.3f}:color=white,fade=t=out:st={end:.9f}:d={edge:.3f}:color=white[reel]")
            mapped="reel"
        audio_args=[]
        if self.a.keep_scene_audio:
            alast="a0"
            for i in range(1,len(scenes)):
                out=f"ma{i}"
                graph.append(f"[{alast}][a{i}]acrossfade=d={x/self.fps:.9f}:c1=tri:c2=tri[{out}]")
                alast=out
            if edge_fade:
                graph.append(f"[{alast}]afade=t=in:st=0:d={edge:.3f},afade=t=out:st={end:.9f}:d={edge:.3f}[areel]")
                alast="areel"
            audio_args=["-map",f"[{alast}]","-c:a","aac","-b:a","192k","-ar","48000"]
        else:
            audio_args=["-an"]
        run(["ffmpeg","-hide_banner","-loglevel","error","-y",*inputs,
             "-filter_complex",";".join(graph),"-map",f"[{mapped}]","-r",str(self.fps),
             *self.encode_args(),*audio_args,"-movflags","+faststart",str(dst)])

    def repeat_montage(self, reel: Path, dst: Path, target_frames: int):
        target_s=target_frames/self.fps
        audio_args=["-map","0:a?","-c:a","copy"] if self.a.keep_scene_audio else ["-an"]
        run(["ffmpeg","-hide_banner","-loglevel","error","-y","-stream_loop","-1","-i",str(reel),
             "-t",f"{target_s:.9f}","-frames:v",str(target_frames),"-map","0:v","-c:v","copy",*audio_args,
             "-movflags","+faststart",str(dst)])

    def render_timeline(self, loops: list[Path], scenes: list[Scene], dst: Path):
        """Loop, trim and join the full timeline in one encode."""
        inputs=[]; graph=[]
        for i, scene in enumerate(scenes):
            inputs += ["-stream_loop", "-1", "-i", str(loops[scene.clip])]
            graph.append(f"[{i}:v]trim=end_frame={scene.frames},setpts=PTS-STARTPTS[s{i}]")
        if len(scenes) == 1:
            last = "s0"
        else:
            last = "s0"
            x = q(self.a.scene_crossfade, self.fps)
            cumulative = scenes[0].frames
            if x == 0:
                labels = "".join(f"[s{i}]" for i in range(len(scenes)))
                graph.append(f"{labels}concat=n={len(scenes)}:v=1:a=0[vout]")
                last = "vout"
            else:
                for i in range(1, len(scenes)):
                    offset=(cumulative-x)/self.fps
                    out=f"v{i}"; graph.append(f"[{last}][s{i}]xfade=transition=fade:duration={x/self.fps:.9f}:offset={offset:.9f}[{out}]")
                    cumulative += scenes[i].frames-x; last=out
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
             "-filter_complex", ";".join(graph), "-map", f"[{last}]", "-an", "-r", str(self.fps),
             *self.encode_args(), "-movflags", "+faststart", str(dst)])

    def join_scenes(self, scenes: list[Path], dst: Path):
        """Legacy helper retained for compatibility with older callers."""
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
            if self.a.audio_mode == "silent":
                run(["ffmpeg","-hide_banner","-loglevel","error","-y","-i",str(video),"-map","0:v","-c:v","copy","-movflags","+faststart",str(out)]); return
            if self.a.keep_scene_audio:
                run(["ffmpeg","-hide_banner","-loglevel","error","-y","-i",str(video),"-map","0:v","-map","0:a?","-c","copy","-movflags","+faststart",str(out)]); return
            raise BuildError("Не указан звук. Добавь --audio, включи сохранение звука сцен или укажи --audio-mode silent.")
        inputs=["-i",str(video)]; graph=[]
        labels=[]
        if self.a.keep_scene_audio:
            graph.append(f"[0:a]atrim=end={target_s:.9f},asetpts=PTS-STARTPTS,volume={self.a.scene_audio_gain}dB[scene]")
            labels.append("[scene]")
        for i,p in enumerate(audio,1):
            inputs += ["-stream_loop","-1","-i",str(p)]
            graph.append(f"[{i}:a]atrim=end={target_s:.9f},asetpts=PTS-STARTPTS,volume={self.a.audio_gain[i-1]}dB[a{i}]")
            labels.append(f"[a{i}]")
        mix="".join(labels)+f"amix=inputs={len(labels)}:normalize=0:duration=longest,alimiter=limit=0.891251,afade=t=in:st=0:d={min(2,target_s/2):.3f},afade=t=out:st={max(0,target_s-min(2,target_s/2)):.3f}:d={min(2,target_s/2):.3f}[aout]"
        graph.append(mix)
        run(["ffmpeg","-hide_banner","-loglevel","error","-y",*inputs,"-filter_complex",";".join(graph),
             "-map","0:v","-map","[aout]","-c:v","copy","-c:a","aac","-b:a","192k","-ar","48000",
             "-t",f"{target_s:.9f}","-movflags","+faststart",str(out)])

    def validate_final(self, out: Path, target_frames: int):
        data=ffprobe(out); vs=next(s for s in data["streams"] if s["codec_type"]=="video")
        frames=int(vs.get("nb_frames") or 0); got=round(float(vs.get("duration",0))*self.fps)
        if abs(got-target_frames)>1: raise BuildError(f"Финальная длительность неверна: {got} кадров вместо {target_frames}")
        run(["ffmpeg","-hide_banner","-loglevel","error","-v","error","-i",str(out),"-f","null","-"])
        self.report["final"]={"path":str(out),"video_frames":frames,"duration_seconds":float(vs.get("duration",0))}

    def build(self, clips: list[Path], audios: list[Path]):
        requested_target=q(self.a.duration*60,self.fps)
        target=min(requested_target, q(20,self.fps)) if self.a.preview else requested_target
        tmp_root=Path(self.a.work_dir) if self.a.work_dir else None
        if self.a.dry_run:
            if self.a.mode == "montage": self.plan_montage(clips,target if self.a.preview else None)
            else: self.plan(len(clips), target)
            print(json.dumps(self.report,ensure_ascii=False,indent=2))
            return
        with tempfile.TemporaryDirectory(dir=tmp_root, prefix="ambient_") as td:
            td=Path(td); norm=[]; loops=[]
            print(f"[1/4] Нормализация клипов: 0/{len(clips)}")
            for i,c in enumerate(clips):
                print(f"      клип {i+1}/{len(clips)}")
                n=td/f"norm_{i}.mp4"; self.normalize(c,n,keep_audio=(self.a.mode == "montage" and self.a.keep_scene_audio)); norm.append(n)
            if self.a.mode == "montage":
                scenes=self.plan_montage(norm,target if self.a.preview else None)
                reel=td/"montage_reel.mp4"
                print(f"[2/4] Монтаж без loop: {len(scenes)} клипов; кодировщик {self.encoder}")
                self.render_montage(norm,scenes,reel,edge_fade=not self.a.preview)
                video=td/"video.mp4"
                if self.a.preview: shutil.copy2(reel,video)
                else:
                    print("[3/4] Повтор готового монтажного блока до нужной длительности")
                    self.repeat_montage(reel,video,target)
            else:
                print(f"[2/4] Создание циклов: 0/{len(norm)}")
                for i,n in enumerate(norm):
                    print(f"      цикл {i+1}/{len(norm)}")
                    l=td/f"loop_{i}.mp4"; self.make_video_loop(n,l,i+1); loops.append(l)
                scenes=self.plan(len(loops),target)
                video=td/"video.mp4"
                print(f"[3/4] Финальный монтаж в один проход: {len(scenes)} сцен; кодировщик {self.encoder}")
                self.render_timeline(loops,scenes,video)
            if self.a.preview:
                out=Path(self.a.output); prev=out.with_name(out.stem+"_preview"+out.suffix)
                shutil.copy2(video, prev)
                print("Превью:",prev); return
            print("[4/4] Сборка звука и экспорт")
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
    # Shorter scenes make every selected clip appear even in a 3-minute test.
    p.add_argument("--min-seg",type=float,default=25); p.add_argument("--max-seg",type=float,default=45)
    p.add_argument("--crossfade",type=float,default=1); p.add_argument("--scene-crossfade",type=float,default=2)
    p.add_argument("--audio",nargs="*"); p.add_argument("--audio-mode",choices=("external","silent"),default="external")
    p.add_argument("--audio-crossfade",type=float,default=2); p.add_argument("--audio-gain",nargs="*",type=float)
    p.add_argument("--keep-scene-audio",action="store_true",help="Сохранить локальный звук, уже встроенный в видеосцены.")
    p.add_argument("--scene-audio-gain",type=float,default=0,help="Громкость локального звука сцен в dB.")
    # The generated source clips are 24 fps.  Converting them to 30 fps by
    # duplicating frames produces visible global judder, so preserve 24 fps.
    p.add_argument("--width",type=int,default=1920);p.add_argument("--height",type=int,default=1080);p.add_argument("--fps",type=int,default=24)
    p.add_argument("--scale-mode",choices=("fit","fill"),default="fit");p.add_argument("--crf",type=int,default=18);p.add_argument("--seed",type=int)
    p.add_argument("--encoder",choices=("auto","cpu","nvenc","qsv","amf"),default="auto")
    p.add_argument("--cpu-preset",choices=("ultrafast","superfast","veryfast","faster","fast","medium","slow"),default="fast")
    p.add_argument("--gpu-quality",type=int,default=20)
    p.add_argument("--mode",choices=("loop","montage"),default="loop")
    p.add_argument("--transition",choices=("calm","fade","fadewhite","fadeblack"),default="calm")
    p.add_argument("--transition-duration",type=float,default=0.8)
    p.add_argument("--montage-rounds",type=int,default=2)
    p.add_argument("--already-looped", action="store_true",
                   help="Клипы уже зациклены: не добавлять внутреннее плавное наложение.")
    p.add_argument("--preview",action="store_true");p.add_argument("--dry-run",action="store_true");p.add_argument("--work-dir");p.add_argument("--overwrite",action="store_true");return p.parse_args(argv)

def main(argv=None):
    try:
        a=parse_args(argv); check_tools()
        if a.width<=0 or a.height<=0 or a.width%2 or a.height%2 or a.fps<=0: raise BuildError("Ширина и высота должны быть положительными чётными, FPS — положительным.")
        if a.duration<=0 or a.min_seg<=0 or a.max_seg<=0 or a.min_seg>a.max_seg: raise BuildError("Проверь duration, min-seg и max-seg.")
        if min(a.crossfade,a.scene_crossfade,a.audio_crossfade,a.transition_duration)<0: raise BuildError("Crossfade не может быть отрицательным.")
        if a.montage_rounds < 1: raise BuildError("Число кругов монтажа должно быть не меньше одного.")
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
