#!/usr/bin/env python3
"""Enquadramento por rosto na prévia do corte (Titeu): mede onde está o rosto em cada plano de câmera e corrige o cx.

  python3 rosto.py <plan.json> <prévia.mp4> [--transcript work/<c>/transcript.json] [--aplicar]

Regra da skill: cx/cy só mudam quando a câmera muda; dentro do mesmo plano de câmera o centro é um só. Por isso a medida é
por PLANO (shots seguidos com a mesma câmera e o mesmo cx/cy), em quadros fora do gancho e dos b-rolls.
Devolve JSON: {"ajustes": [...], "sem_rosto": [...], "sem_fala": [...]}. "sem_fala" é só um alerta (boca parada enquanto
há fala) é EXPERIMENTAL: deu falso positivo em prévia real (28/10/2026), então a esteira só registra no log.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

LARG_RECORTE = 0.316   # 1 pessoa: o recorte vertical cobre ~31,6% da largura do bruto (escala 3.160 do DaVinci)
BORDA = 0.25           # a regra é centralizar pelo CORPO (pessoa inclinada fica com o rosto fora do meio, e está certo);
ALVO = 0.33            # só corrige rosto colado na borda (< 25% ou > 75% da largura) e só até 33%/67%: o mínimo


def dur_video(v):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(v)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def quadro(video, t, pasta, n):
    arq = Path(pasta) / "q{}.png".format(n)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", "{:.3f}".format(t), "-i", str(video), "-frames:v", "1",
                    "-vf", "scale=540:-2", str(arq)], capture_output=True)
    return arq if arq.exists() else None


def planos(plan, dur):
    """[(início, fim, índices dos shots)] agrupando shots seguidos com a mesma câmera e o mesmo enquadramento."""
    shots = sorted(enumerate(plan.get("shots") or []), key=lambda x: float(x[1].get("start") or 0))
    grupos = []
    for i, s in shots:
        chave = (s.get("camera"), s.get("cx"), s.get("cy"), bool(s.get("stack")), s.get("dz"))
        if grupos and grupos[-1]["chave"] == chave:
            grupos[-1]["idx"].append(i)
        else:
            grupos.append({"chave": chave, "ini": float(s.get("start") or 0), "idx": [i]})
    for k, g in enumerate(grupos):
        g["fim"] = grupos[k + 1]["ini"] if k + 1 < len(grupos) else dur
    return grupos


def cobertos(plan):
    """Trechos em que a pessoa não está sozinha na tela: gancho (foto em cima) e b-rolls."""
    c = [(0.0, float((plan.get("hook") or {}).get("duration") or 0) + 0.3)]
    for b in plan.get("broll") or []:
        a = float(b.get("start") or 0)
        c.append((a - 0.3, a + float(b.get("dur") or 0) + 0.3))
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("video")
    ap.add_argument("--transcript")
    ap.add_argument("--aplicar", action="store_true")
    a = ap.parse_args()
    import cv2
    import numpy as np
    # YuNet (OpenCV, modelo de 230 KB ao lado deste script) acha rosto de lado e cortado pela borda, que é o caso que importa
    yunet = cv2.FaceDetectorYN.create(str(Path(__file__).resolve().parent / "face_detection_yunet.onnx"), "", (540, 960), 0.6, 0.3, 5000)
    plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    dur = dur_video(a.video)
    palavras = []
    if a.transcript and Path(a.transcript).exists():
        palavras = [(float(w["start"]), float(w["end"])) for w in json.loads(Path(a.transcript).read_text(encoding="utf-8")).get("words") or []]
    fora = cobertos(plan)
    livre = lambda t: not any(x <= t <= y for x, y in fora)
    falando = lambda t: any(s - 0.05 <= t <= e + 0.05 for s, e in palavras)

    def rosto(img):
        yunet.setInputSize((img.shape[1], img.shape[0]))
        _, achados = yunet.detect(img)
        if achados is None or not len(achados):
            return None
        r = max(achados, key=lambda f: f[2] * f[3])   # o maior rosto da tela
        x, y, w, h = (int(max(0, v)) for v in r[:4])
        return x, y, max(1, min(w, img.shape[1] - x)), max(1, min(h, img.shape[0] - y))

    saida = {"ajustes": [], "sem_rosto": [], "sem_fala": []}
    with tempfile.TemporaryDirectory() as tmp:
        n = 0
        for g in planos(plan, dur):
            cam, cx, cy, stack, dz = g["chave"]
            if stack or dz or cx is None or g["fim"] - g["ini"] < 1.2:
                continue   # tela dividida e zoom manual: fora da medida automática
            ts = [g["ini"] + (g["fim"] - g["ini"]) * f for f in (0.2, 0.4, 0.6, 0.8)]
            ts = [t for t in ts if livre(t) and t < dur - 0.4]
            if len(ts) < 2:
                continue
            xs, bocas = [], []
            for t in ts:
                q = quadro(a.video, t, tmp, n)
                n += 1
                img = cv2.imread(str(q)) if q else None
                if img is None:
                    continue
                r = rosto(img)
                if r is None:
                    continue
                x, y, w, h = r
                xs.append((x + w / 2) / img.shape[1])
                if falando(t):   # boca mexe? compara a boca com os olhos em 3 quadros seguidos (o balanço da câmera se anula)
                    seq = [img] + [cv2.imread(str(quadro(a.video, t + d, tmp, "{}_{}".format(n, d)) or "")) for d in (0.12, 0.24)]
                    if all(s is not None and s.shape == img.shape for s in seq):
                        cinza = [cv2.cvtColor(s, cv2.COLOR_BGR2GRAY).astype(np.float32) for s in seq]
                        boca = lambda c: c[y + int(h * 0.65): y + h, x + int(w * 0.2): x + int(w * 0.8)]
                        olho = lambda c: c[y + int(h * 0.2): y + int(h * 0.45), x + int(w * 0.15): x + int(w * 0.85)]
                        mb = np.mean([np.abs(boca(cinza[i + 1]) - boca(cinza[i])).mean() for i in range(2)])
                        mo = np.mean([np.abs(olho(cinza[i + 1]) - olho(cinza[i])).mean() for i in range(2)]) + 0.5
                        bocas.append(mb / mo)
            if not xs:
                saida["sem_rosto"].append("{:.1f}–{:.1f} s".format(g["ini"], g["fim"]))
                continue
            if len(bocas) >= 2 and max(bocas) < 1.15:
                saida["sem_fala"].append("{:.1f}–{:.1f} s".format(g["ini"], g["fim"]))
            if len(xs) < 2:
                continue
            fx = float(np.median(xs))
            if fx < BORDA or fx > 1 - BORDA:
                alvo = ALVO if fx < BORDA else 1 - ALVO
                novo = round(min(0.95, max(0.05, float(cx) + (fx - alvo) * LARG_RECORTE)), 3)
                saida["ajustes"].append({"ini": round(g["ini"], 2), "fim": round(g["fim"], 2), "camera": cam, "cx_antes": cx, "cx_novo": novo,
                                         "rosto_na_tela": round(fx, 2)})
                if a.aplicar:
                    for i in g["idx"]:
                        plan["shots"][i]["cx"] = novo
                    for b in plan.get("_base_shots") or []:   # o movimento.py refaz os shots a partir daqui
                        if g["ini"] - 0.05 <= float(b.get("start") or 0) < g["fim"] and b.get("cx") == cx and b.get("camera") == cam:
                            b["cx"] = novo
    if a.aplicar and saida["ajustes"]:
        Path(a.plan).write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(saida, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
