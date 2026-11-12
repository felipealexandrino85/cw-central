#!/usr/bin/env python3
"""Centro do recorte pelos OMBROS de quem está no recorte (enquadramento novo do Titeu, 12/11/2026).

  python3 centro.py <plan.json> <base.mov> [--aplicar]

Antes o cx de cada câmera era digitado pelo Claude olhando uma folha (prompt CAMERAS) e só o rosto colado na borda era
corrigido (rosto.py). Aqui o cx de cada PLANO (shots seguidos com a mesma câmera e o mesmo cx) é MEDIDO no vídeo horizontal
do corte (base.mov, a mesma escala do cx): pessoa (MediaPipe, OpenCV Zoo, Apache 2.0) → pose → meio dos ombros, mediana
de alguns quadros do plano. A pessoa medida é a que já está no recorte (quem fala foi decidido antes, pela voz); o plano
só muda de lugar, não de pessoa. Referência medida: os cortes aprovados (REF Schisler) têm o meio dos ombros a 0,044 do
centro (mediana) e o rosto fora do meio quando a pessoa olha de lado, e está certo.
Devolve JSON: {"ajustes": [...], "sem_medida": [...], "tempo_s": s}.
"""
import argparse
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

AQUI = Path(__file__).resolve().parent
LARG_RECORTE = 0.316   # recorte vertical de uma pessoa ≈ 31,6% da largura do bruto 16:9
MIN_MUDA = 0.012       # menos que isso (≈ 4% da largura do recorte) não mexe
MAX_MUDA = 0.12        # mais que isso seria trocar de pessoa, e isso não é papel deste passo
PLANO_MIN = 1.0        # planos mais curtos não são medidos (s)
VIS_MIN = 0.5          # visibilidade mínima dos dois ombros


def pasta_modelos():
    for p in (os.environ.get("CW_MODELOS_POSE"), AQUI.parent / "modelos" / "opencv_zoo", Path.home() / ".cw-cortes" / "modelos" / "opencv_zoo"):
        if p and (Path(p) / "pose_estimation_mediapipe__pose_estimation_mediapipe_2023mar.onnx").exists():
            return Path(p)
    return None


def carregar():
    pasta = pasta_modelos()
    if not pasta:
        return None
    sys.path.insert(0, str(pasta))
    pd = importlib.import_module("person_detection_mediapipe__mp_persondet")
    po = importlib.import_module("pose_estimation_mediapipe__mp_pose")
    return (pd.MPPersonDet(str(pasta / "person_detection_mediapipe__person_detection_mediapipe_2023mar.onnx"), 0.3, 0.5, 5000),
            po.MPPose(str(pasta / "pose_estimation_mediapipe__pose_estimation_mediapipe_2023mar.onnx"), 0.5))


def dur_video(v):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(v)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def quadro(video, t, pasta, n):
    arq = Path(pasta) / "c{}.png".format(n)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", "{:.3f}".format(t), "-i", str(video), "-frames:v", "1",
                    "-vf", "scale=1280:-2", str(arq)], capture_output=True)
    return arq if arq.exists() else None


def planos(plan, dur):
    """[{chave, ini, fim, idx}] com shots seguidos de mesma câmera e mesmo enquadramento (a regra: cx só muda com a câmera)."""
    shots = sorted(enumerate(plan.get("shots") or []), key=lambda x: float(x[1].get("start") or 0))
    grupos = []
    for i, s in shots:
        chave = (s.get("camera"), s.get("cx"), s.get("cy"), bool(s.get("stack")))
        if grupos and grupos[-1]["chave"] == chave:
            grupos[-1]["idx"].append(i)
        else:
            grupos.append({"chave": chave, "ini": float(s.get("start") or 0), "idx": [i]})
    for k, g in enumerate(grupos):
        g["fim"] = grupos[k + 1]["ini"] if k + 1 < len(grupos) else dur
    return grupos


def pessoas_no_quadro(pdet, img):
    """MPPersonDet.infer com as saídas da rede na ordem certa: o OpenCV 5 (servidor) devolve [pontuação, caixas] e o
    código do OpenCV Zoo espera [caixas (…, 12), pontuação (…, 1)]."""
    import numpy as np
    blob, pad = pdet._preprocess(img)
    pdet.model.setInput(blob)
    saidas = sorted(pdet.model.forward(pdet.model.getUnconnectedOutLayersNames()), key=lambda o: -o.shape[-1])
    return pdet._postprocess(saidas, np.array([img.shape[1], img.shape[0]]), pad)


def ombros(img, cx, pdet, pose):
    """Meio dos ombros (0–1 na largura) da pessoa cujo nariz está dentro do recorte centrado em cx, a mais perto de cx."""
    H, W = img.shape[:2]
    try:
        pessoas = pessoas_no_quadro(pdet, img)
    except Exception:
        return None
    melhor = None
    for p in pessoas if pessoas is not None else []:
        res = pose.infer(img, p)
        if res is None:
            continue
        lm = res[1]   # (39, 5): x, y, z, visibilidade, presença (pixels)
        nariz, oe, od = lm[0], lm[11], lm[12]
        nx = float(nariz[0]) / W
        if abs(nx - cx) > LARG_RECORTE / 2 or min(oe[3], od[3]) < VIS_MIN:
            continue
        if not all(0 <= q[0] <= W and 0 <= q[1] <= H for q in (oe, od)):
            continue
        mx = float(oe[0] + od[0]) / 2 / W
        if melhor is None or abs(nx - cx) < melhor[0]:
            melhor = (abs(nx - cx), mx)
    return None if melhor is None else melhor[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("video")
    ap.add_argument("--aplicar", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    import cv2
    import numpy as np
    modelos = carregar()
    if not modelos:
        print(json.dumps({"ok": False, "motivo": "modelos de pose não encontrados"}))
        return 0
    pdet, pose = modelos
    plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    dur = dur_video(a.video)
    saida = {"ok": True, "ajustes": [], "sem_medida": []}
    with tempfile.TemporaryDirectory() as tmp:
        n = 0
        for g in planos(plan, dur):
            cam, cx, cy, stack = g["chave"]
            if stack or cx is None or g["fim"] - g["ini"] < PLANO_MIN:
                continue   # tela dividida: duas pessoas, fora deste passo
            cx = float(cx)
            k = 5 if g["fim"] - g["ini"] >= 4 else 3
            ts = [g["ini"] + (g["fim"] - g["ini"]) * (j + 1) / (k + 1) for j in range(k)]
            xs = []
            for t in ts:
                if t >= dur - 0.2:
                    continue
                q = quadro(a.video, t, tmp, n)
                n += 1
                img = cv2.imread(str(q)) if q else None
                if img is None:
                    continue
                m = ombros(img, cx, pdet, pose)
                if m is not None:
                    xs.append(m)
            if len(xs) < 2:
                saida["sem_medida"].append("{:.1f}–{:.1f} s".format(g["ini"], g["fim"]))
                continue
            novo = round(float(np.median(xs)), 3)
            if not MIN_MUDA <= abs(novo - cx) <= MAX_MUDA:
                continue
            saida["ajustes"].append({"ini": round(g["ini"], 2), "fim": round(g["fim"], 2), "camera": cam, "cx_antes": cx, "cx_novo": novo,
                                     "quadros": len(xs)})
            if a.aplicar:
                for i in g["idx"]:
                    plan["shots"][i]["cx"] = novo
                base = sorted(plan.get("_base_shots") or [], key=lambda b: float(b.get("start") or 0))
                for j, b in enumerate(base):   # o movimento.py refaz os shots a partir daqui
                    b0 = float(b.get("start") or 0)
                    b1 = float(base[j + 1].get("start") or 0) if j + 1 < len(base) else dur
                    if b0 < g["fim"] - 0.05 and b1 > g["ini"] + 0.05 and b.get("cx") == g["chave"][1] and b.get("camera") == cam:
                        b["cx"] = novo
    if a.aplicar and saida["ajustes"]:
        Path(a.plan).write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    saida["tempo_s"] = round(time.time() - t0, 1)
    print(json.dumps(saida, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
