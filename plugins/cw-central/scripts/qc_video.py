#!/usr/bin/env python3
"""Checagens objetivas do vídeo final da esteira (31/10/2026), medidas e não estimadas:

- flash: quadros brancos de 1 a 3 frames nas transições (fim do gancho, entrada de b-roll). O time lia como "bug de frame na
  transição" (cortes 69 e 78). Desde 09/11/2026 também os quadros soltos de 1–2 frames entre dois cortes secos (sobras do Zeca). `tirar_flash` troca esses quadros pelo quadro anterior (corte seco; tempo e som não mudam).
- fim: a voz vai até o último instante do arquivo? ("o final cortou o que a pessoa ia falar", cortes 30, 31, 47, 71).

  python3 qc_video.py flash <video> [--corrigir]
  python3 qc_video.py fim <video>
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


def _rodar(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _fps(video):
    r = _rodar(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(video)], 60)
    try:
        a, b = r.stdout.strip().strip(",").split("/")
        return float(a) / float(b)
    except ValueError:
        return 30.0


def flashes(video):
    """[(primeiro, último), ...] índices dos quadros de flash: brilho bem acima do que vem ANTES e DEPOIS, por no máximo 3 quadros
    (b-roll claro que entra e fica não conta: o depois também é claro)."""
    r = _rodar(["ffmpeg", "-nostdin", "-v", "info", "-i", str(video), "-an", "-vf",
                "scale=96:-2,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-", "-f", "null", "-"], 900)
    ys = [float(x) for x in re.findall(r"YAVG=([\d.]+)", r.stdout)]
    med = lambda v: sorted(v)[len(v) // 2] if v else 255.0
    claros = [i for i, y in enumerate(ys) if y > 150 and y > max(med(ys[max(0, i - 9):max(0, i - 3)]), med(ys[i + 4:i + 10])) + 45]
    grupos = []
    for i in claros:
        if grupos and i - grupos[-1][1] <= 1:
            grupos[-1][1] = i
        else:
            grupos.append([i, i])
    brancos = [(a, b) for a, b in grupos if b - a <= 2 and a > 0]
    try:
        soltos = intrusos(video)
    except Exception:
        soltos = []
    junta = sorted(set(brancos) | set(soltos))
    out = []
    for a, b in junta:   # sem sobreposição (o freezeframes troca cada grupo uma vez)
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def intrusos(video, lado=64):
    """Quadros soltos de 1–2 frames entre dois cortes secos: sobra do plano anterior, de uma foto ou do vídeo de baixo que vaza na
    transição ("sobras de frame em momentos de transição", Zeca, lotes 20 e 31). Nenhum plano editado dura 2 quadros (0,08 s)."""
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-an", "-vf", "scale={0}:{0},format=gray".format(lado),
                        "-f", "rawvideo", "-"], capture_output=True, timeout=900)
    n = lado * lado
    fr = [r.stdout[i:i + n] for i in range(0, len(r.stdout) - n + 1, n)]
    if len(fr) < 10:
        return []
    def dif(x, y):
        return sum(abs(a - b) for a, b in zip(x[::4], y[::4])) / (n / 4)
    d = [0.0] + [dif(fr[i - 1], fr[i]) for i in range(1, len(fr))]   # d[i]: corte entre i-1 e i
    corte = [i for i in range(1, len(d)) if d[i] >= 28]
    out = []
    for k in range(len(corte) - 1):
        a, b = corte[k], corte[k + 1]
        if 1 <= b - a <= 2 and a > 1 and b < len(fr) - 1:
            out.append((a, b - 1))   # quadros a..b-1 são o intruso
    return out


def tirar_flash(video):
    """Troca cada flash pelo quadro anterior, no próprio arquivo. Devolve quantos tirou (0 = arquivo intocado)."""
    video = Path(video)
    gs = flashes(video)
    if not gs:
        return 0
    n = len(gs)
    fc = ["[0:v]split={}[s0]{}".format(n + 1, "".join("[r{}]".format(k) for k in range(n)))]
    for k, (a, b) in enumerate(gs):
        fc.append("[s{}][r{}]freezeframes=first={}:last={}:replace={}[s{}]".format(k, k, a, b, a - 1, k + 1))
    tmp = video.with_name(video.stem + ".sem_flash" + video.suffix)
    r = _rodar(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video), "-filter_complex", ";".join(fc), "-map", "[s{}]".format(n),
                "-map", "0:a?", "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "copy",
                "-movflags", "+faststart", str(tmp)], 2400)
    if r.returncode or not tmp.exists() or tmp.stat().st_size < 100000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("tirar flash: " + (r.stderr or "")[-300:])
    shutil.move(str(tmp), str(video))
    return n


def fim(video):
    """{"voz_ate_o_fim": bool, "db_fim", "db_fala"}: a voz (300–3400 Hz) nos últimos 0,3 s comparada com o nível da fala do corte."""
    import array
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "8000", "-af",
                        "highpass=f=300,lowpass=f=3400", "-f", "s16le", "-"], capture_output=True, timeout=600)
    a = array.array("h", r.stdout)
    if len(a) < 8000:
        return {"voz_ate_o_fim": False, "db_fim": None, "db_fala": None}
    import math
    jan = 320   # 40 ms
    dbs = []
    for i in range(0, len(a) - jan + 1, jan):
        s = sum(x * x for x in a[i:i + jan]) / jan
        dbs.append(10 * math.log10(s / 32768 ** 2 + 1e-10))
    fala = sorted(dbs)[int(len(dbs) * 0.75)]
    ult = dbs[-8:]   # 0,32 s
    db_fim = 10 * math.log10(sum(10 ** (d / 10) for d in ult) / len(ult))
    return {"voz_ate_o_fim": db_fim >= fala - 8, "db_fim": round(db_fim, 1), "db_fala": round(fala, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["flash", "fim"])
    ap.add_argument("video")
    ap.add_argument("--corrigir", action="store_true")
    a = ap.parse_args()
    if a.cmd == "flash":
        fps = _fps(a.video)
        gs = flashes(a.video)
        print(json.dumps({"flashes_s": [round(x / fps, 2) for x, _ in gs], "tirados": tirar_flash(a.video) if a.corrigir else 0}))
    else:
        print(json.dumps(fim(a.video)))


if __name__ == "__main__":
    sys.exit(main())
