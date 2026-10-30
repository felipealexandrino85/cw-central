#!/usr/bin/env python3
"""Quem está falando na imagem? Detector de falante pela boca sincronizada com a voz (Titeu/Zeca).

  python3 falante.py <vídeo horizontal do corte> [--de 0] [--ate 60] [--json saida.json]

Por que existe: o episódio editado muitas vezes mostra os apresentadores REAGINDO enquanto o convidado fala. Recortar
"o meio dos apresentadores" ou "o apresentador da direita" põe a pessoa errada na tela (CW Cast #51, 29/10/2026).

Como mede: em cada PLANO (troca de câmera achada pela imagem), acha os rostos (YuNet), mede quanto a boca de cada um se
mexe quadro a quadro (descontando o balanço da cabeça pelos olhos) e compara com o envelope da voz, em janelas de 2 s, com
atraso de 0 a 80 ms. Quem mexe a boca no compasso das sílabas está falando; rir, balançar a cabeça ou gesticular não acompanha.
Calibrado no CW Cast #51: quem fala tem mediana 0,36–0,48; quem ouve, −0,3 a 0,2.

Devolve JSON:
  {"planos": [{"ini", "fim", "rostos": [{"cx", "cy", "larg", "sync", "janelas"}], "falante": índice|null,
               "estado": "falando"|"fora_de_quadro"|"incerto"}],
   "fora_de_quadro": [[ini, fim], ...]}   trechos com voz em que ninguém na tela está falando
cx/cy = centro do rosto em fração do quadro horizontal (0–1).
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

FPS = 25
FALA = 0.33        # mediana das janelas a partir da qual o rosto ESTÁ falando
OUVE = 0.20        # todos os rostos abaixo disso (e voz no plano) = quem fala está fora da imagem
JANELA = 50        # 2 s
PASSO = 25         # janelas a cada 1 s
PLANO_MIN = 1.0    # planos mais curtos não são julgados (s)


def tamanho(video):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0",
                        str(video)], capture_output=True, text=True)
    return tuple(int(x) for x in r.stdout.strip().split(",")[:2])


def envelope_voz(video, de, ate, n):
    """Energia da voz (300–3400 Hz) por quadro, em dB."""
    import numpy as np
    sr = 16000
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-ss", "{:.3f}".format(de)] + (["-t", "{:.3f}".format(ate - de)] if ate else []) + \
          ["-i", str(video), "-vn", "-ac", "1", "-ar", str(sr), "-af", "highpass=f=300,lowpass=f=3400", "-f", "s16le", "-"]
    a = np.frombuffer(subprocess.run(cmd, capture_output=True).stdout, np.int16).astype(np.float32) / 32768
    p = sr // FPS
    e = np.array([np.sqrt(np.mean(a[i * p:(i + 1) * p] ** 2) + 1e-9) if (i + 1) * p <= len(a) else 1e-9 for i in range(n)])
    return 20 * np.log10(e + 1e-6)


def suave(x, k):
    import numpy as np
    return np.convolve(np.nan_to_num(x), np.ones(k) / k, "same")


def medir(video, de=0.0, ate=None):
    import cv2
    import numpy as np
    W, H = tamanho(video)
    w2, h2 = 960, int(round(H * 960 / W / 2) * 2)
    esc = W / w2
    yunet = cv2.FaceDetectorYN.create(str(Path(__file__).resolve().parent / "face_detection_yunet.onnx"), "", (w2, h2), 0.7, 0.3, 5000)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-ss", "{:.3f}".format(de)] + (["-t", "{:.3f}".format(ate - de)] if ate else []) + \
          ["-i", str(video), "-vf", "fps={}".format(FPS), "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    tam = W * H * 3
    ant_cinza, ant_hist = None, None
    cortes, quadros_rostos = [0], []   # por quadro: [(cx, cy, larg, mov)]
    n = 0
    while True:
        b = proc.stdout.read(tam)
        if len(b) < tam:
            break
        im = np.frombuffer(b, np.uint8).reshape(H, W, 3)
        peq = cv2.resize(im, (w2, h2))
        hsv = cv2.cvtColor(cv2.resize(peq, (160, 90)), cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256])
        hist = cv2.normalize(hist, hist).flatten()
        if ant_hist is not None and cv2.compareHist(ant_hist, hist, cv2.HISTCMP_BHATTACHARYYA) > 0.35 and n - cortes[-1] >= 3:
            cortes.append(n)
        cinza = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY).astype(np.float32)
        _, faces = yunet.detect(peq)
        rs = []
        for r in (faces if faces is not None else []):
            x, y, w, h = (float(v) * esc for v in r[:4])
            if w < W * 0.035:
                continue   # rosto minúsculo no fundo
            mov = np.nan
            if ant_cinza is not None and cortes[-1] != n:
                mrx, mry, mlx, mly = (float(v) * esc for v in r[10:14])   # cantos da boca
                mx, my, mw = (mrx + mlx) / 2, (mry + mly) / 2, abs(mlx - mrx)
                x0, x1 = int(max(0, mx - mw * 0.7)), int(min(W, mx + mw * 0.7))
                y0, y1 = int(max(0, my - h * 0.10)), int(min(H, my + h * 0.25))
                ex0, ex1, ey0, ey1 = int(max(0, x + w * .15)), int(min(W, x + w * .85)), int(max(0, y + h * .2)), int(min(H, y + h * .45))
                if x1 - x0 >= 4 and y1 - y0 >= 4 and ex1 > ex0 and ey1 > ey0:
                    d = np.abs(cinza[y0:y1, x0:x1] - ant_cinza[y0:y1, x0:x1]).mean()
                    e = np.abs(cinza[ey0:ey1, ex0:ex1] - ant_cinza[ey0:ey1, ex0:ex1]).mean()
                    mov = float(d - 0.8 * e)   # a boca mexendo além do que a cabeça toda mexe
            rs.append(((x + w / 2) / W, (y + h / 2) / H, w, mov))
        quadros_rostos.append(rs)
        ant_cinza, ant_hist = cinza, hist
        n += 1
    proc.wait()
    if not n:
        return {"planos": [], "fora_de_quadro": []}
    voz = envelope_voz(video, de, ate, n)
    tem_voz = voz > (np.percentile(voz, 90) - 22)
    limites = cortes + [n]
    planos, fora = [], []
    for a, b in zip(limites[:-1], limites[1:]):
        if (b - a) / FPS < PLANO_MIN:
            continue
        trilhas = []
        for i in range(a, b):
            for cx, cy, lg, mov in quadros_rostos[i]:
                t = next((t for t in trilhas if abs(t["cx"] - cx) < 0.08 and abs(t["cy"] - cy) < 0.15), None)
                if t is None:
                    t = {"cx": cx, "cy": cy, "lg": [], "mov": {}}
                    trilhas.append(t)
                t["mov"][i] = mov
                t["lg"].append(lg)
        trilhas = [t for t in trilhas if len(t["mov"]) >= 0.5 * (b - a)]
        vv = voz[a:b]
        rostos = []
        for t in trilhas:
            m = np.array([t["mov"].get(i, np.nan) for i in range(a, b)], dtype=float)
            ok = ~np.isnan(m)
            jan = []
            for j0 in range(0, max(1, len(m) - PASSO), PASSO):
                j1 = min(len(m), j0 + JANELA)
                okj = ok[j0:j1]
                if okj.sum() < 20 or not tem_voz[a + j0:a + j1].mean() > 0.3:
                    continue
                mj, vj = suave(m[j0:j1], 5)[okj], suave(vv[j0:j1], 5)
                cs = []
                for lag in (0, 1, 2):
                    v2 = np.roll(vj, lag)[okj]
                    if np.std(mj) > 1e-6 and np.std(v2) > 1e-6:
                        cs.append(float(np.corrcoef(mj, v2)[0, 1]))
                if cs:
                    jan.append(max(cs))
            rostos.append({"cx": round(float(t["cx"]), 3), "cy": round(float(t["cy"]), 3), "larg": int(np.median(t["lg"])),
                           "sync": round(float(np.median(jan)), 2) if jan else None, "janelas": len(jan)})
        voz_plano = float(tem_voz[a:b].mean())
        medidos = [r for r in rostos if r["sync"] is not None]
        falante, estado = None, "incerto"
        if medidos:
            k = max(range(len(rostos)), key=lambda j: rostos[j]["sync"] if rostos[j]["sync"] is not None else -9)
            if rostos[k]["sync"] >= FALA:
                falante, estado = k, "falando"
            elif voz_plano >= 0.35 and all(r["sync"] < OUVE for r in medidos):
                estado = "fora_de_quadro"
        elif voz_plano >= 0.35 and not rostos:
            estado = "fora_de_quadro"   # tem voz e não tem rosto nenhum na tela
        ini, fim = round(de + a / FPS, 2), round(de + b / FPS, 2)
        planos.append({"ini": ini, "fim": fim, "voz": round(voz_plano, 2), "rostos": rostos, "falante": falante, "estado": estado})
        if estado == "fora_de_quadro":
            if fora and ini - fora[-1][1] < 0.2:
                fora[-1][1] = fim
            else:
                fora.append([ini, fim])
    return {"planos": planos, "fora_de_quadro": fora}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--de", type=float, default=0.0)
    ap.add_argument("--ate", type=float)
    ap.add_argument("--json")
    a = ap.parse_args()
    txt = json.dumps(medir(a.video, a.de, a.ate), ensure_ascii=False)
    if a.json:
        Path(a.json).write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    sys.exit(main())
