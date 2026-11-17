#!/usr/bin/env python3
"""Enquadramento por rosto na prévia do corte (Titeu): mede onde está o rosto em cada plano de câmera e corrige o cx.

  python3 rosto.py <plan.json> <prévia.mp4> [--transcript work/<c>/transcript.json] [--aplicar]

Regra da skill: cx/cy só mudam quando a câmera muda; dentro do mesmo plano de câmera o centro é um só. Por isso a medida é
por PLANO (shots seguidos com a mesma câmera e o mesmo cx/cy), em quadros fora do gancho e dos b-rolls.
Com --base (o horizontal do corte) também acha o recorte VAZIO: o episódio editado trocou de câmera no meio do plano e o cx
ficou onde estava a outra pessoa; o plano é partido na troca e o recorte vai no rosto que está na imagem ("vazios").
Devolve JSON: {"ajustes": [...], "sem_rosto": [...], "sem_fala": [...], "vazios": [...]}. "sem_fala" é só um alerta (boca parada enquanto
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


def vazios(g, base, preview, rosto, livre, dur, tmp, cv2):
    """Trechos do plano em que o recorte ficou SEM ninguém: o episódio editado trocou de câmera no meio do plano e o cx ficou
    na posição da câmera anterior (l66c01, 17/11/2026: só a mão e o tablet). Amostra a cada 0,3 s; onde a prévia não tem rosto
    e o vídeo horizontal tem um rosto fora do recorte, devolve (início na troca de imagem, fim, cx do maior rosto)."""
    cx = float(g["chave"][1])
    ts = [g["ini"] + 0.15 + 0.3 * k for k in range(int((g["fim"] - g["ini"] - 0.15) / 0.3) + 1)]
    marcas = []   # (t, cx do rosto fora do recorte) ou (t, None) com o recorte ok
    for k, t in enumerate(ts):
        if not livre(t) or t >= dur - 0.3:
            continue
        q = quadro(preview, t, tmp, "v{}".format(k))
        img = cv2.imread(str(q)) if q else None
        if img is None or rosto(img) is not None:
            marcas.append((t, None))
            continue
        qb = quadro(base, t, tmp, "b{}".format(k))
        hb = cv2.imread(str(qb)) if qb else None
        rb = rosto(hb) if hb is not None else None
        if rb is None:
            marcas.append((t, None))   # ninguém na imagem inteira: não há o que centralizar
            continue
        fx = (rb[0] + rb[2] / 2) / hb.shape[1]
        marcas.append((t, round(fx, 3) if abs(fx - cx) > LARG_RECORTE / 2 else None))
    spans, i = [], 0
    while i < len(marcas):
        if marcas[i][1] is None:
            i += 1
            continue
        j = i
        while j + 1 < len(marcas) and marcas[j + 1][1] is not None and abs(marcas[j + 1][1] - marcas[i][1]) < 0.1:
            j += 1
        if j > i:   # 2 amostras seguidas (≥ 0,3 s) vazias: não é piscada do detector
            ant = marcas[i - 1][0] if i > 0 else g["ini"]
            ini = troca(base, ant, marcas[i][0], tmp, cv2)
            fim = marcas[j + 1][0] if j + 1 < len(marcas) else g["fim"]
            if fim < g["fim"]:
                fim = troca(base, marcas[j][0], fim, tmp, cv2)
            spans.append((round(ini, 3), round(fim, 3), marcas[i][1]))
        i = j + 1
    return spans


def troca(base, a, b, tmp, cv2):
    """O instante da troca de câmera no vídeo horizontal entre a e b (maior diferença entre quadros seguidos, a 1/15 s)."""
    import numpy as np
    passo = 1 / 15
    ts = [a + passo * k for k in range(int((b - a) / passo) + 2)]
    ims = []
    for k, t in enumerate(ts):
        q = quadro(base, t, tmp, "t{}".format(k))
        im = cv2.imread(str(q), cv2.IMREAD_GRAYSCALE) if q else None
        ims.append(cv2.resize(im, (96, 54)).astype(np.float32) if im is not None else None)
    difs = [(float(np.abs(ims[k + 1] - ims[k]).mean()), ts[k + 1]) for k in range(len(ims) - 1) if ims[k] is not None and ims[k + 1] is not None]
    return max(difs)[1] if difs else b


def dividir(plan, ini, fim, cx_novo, camera):
    """Põe o cx_novo (e a câmera) de ini a fim nos shots e nos _base_shots, partindo o plano nas bordas (o shot depois de fim volta ao velho)."""
    for chave in ("shots", "_base_shots"):
        lista = sorted(plan.get(chave) or [], key=lambda s: float(s.get("start") or 0))
        if not lista:
            continue
        def vigente(t):
            v = [s for s in lista if float(s.get("start") or 0) <= t + 1e-3]
            return dict(v[-1]) if v else dict(lista[0])
        depois = vigente(fim)
        novos = [s for s in lista if not (ini - 1e-3 <= float(s.get("start") or 0) < fim - 1e-3)]
        dentro = [dict(s) for s in lista if ini - 1e-3 <= float(s.get("start") or 0) < fim - 1e-3]
        primeiro = vigente(ini)
        primeiro.pop("on_word", None)
        primeiro.update({"start": round(ini, 3), "cx": cx_novo, "camera": camera})
        novos.append(primeiro)
        for s in dentro:
            if abs(float(s["start"]) - ini) > 1e-3:
                s.update({"cx": cx_novo, "camera": camera})
                novos.append(s)
        if not any(abs(float(s.get("start") or 0) - fim) < 1e-3 for s in novos):
            depois.pop("on_word", None)
            depois["start"] = round(fim, 3)
            novos.append(depois)
        plan[chave] = sorted(novos, key=lambda s: float(s.get("start") or 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("video")
    ap.add_argument("--transcript")
    ap.add_argument("--base", help="o vídeo horizontal do corte: liga a busca de recorte vazio (troca de câmera no meio do plano)")
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

    saida = {"ajustes": [], "sem_rosto": [], "sem_fala": [], "vazios": []}
    with tempfile.TemporaryDirectory() as tmp:
        n = 0
        for g in planos(plan, dur):
            cam, cx, cy, stack, dz = g["chave"]
            if stack or dz or cx is None or g["fim"] - g["ini"] < 1.2:
                continue   # tela dividida e zoom manual: fora da medida automática
            if a.base:
                vs = vazios(g, a.base, a.video, rosto, livre, dur, tmp, cv2)
                if vs:
                    outros = [x for x in plan.get("shots") or [] if not x.get("stack") and x.get("cx") is not None and x.get("cx") != cx]
                    inicios = [float(x.get("start") or 0) for x in plan.get("shots") or []]
                    for i_, f_, c_ in vs:
                        # a mesma pessoa já tem enquadramento noutro plano do corte: usa o dele (e a câmera), não o centro do rosto
                        par = min(outros, key=lambda x: abs(float(x["cx"]) - c_), default=None)
                        usa = par if par is not None and abs(float(par["cx"]) - c_) <= 0.08 else None
                        perto = [t_ for t_ in inicios if abs(t_ - i_) <= 0.1]   # borda num shot que já existe: sem quadro piscando
                        saida["vazios"].append({"ini": min(perto, key=lambda t_: abs(t_ - i_)) if perto else i_, "fim": f_,
                                                "camera": usa.get("camera") if usa else cam, "cx_antes": cx,
                                                "cx_novo": float(usa["cx"]) if usa else c_, "rosto_em": c_})
                    continue   # o rosto da mediana viria misturado das duas câmeras
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
    if a.aplicar:
        for v in saida["vazios"]:   # depois dos ajustes: dividir muda os índices dos shots
            dividir(plan, v["ini"], v["fim"], v["cx_novo"], v["camera"])
    if a.aplicar and (saida["ajustes"] or saida["vazios"]):
        Path(a.plan).write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(saida, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
