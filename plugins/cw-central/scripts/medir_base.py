"""Medição de partida do enquadramento (Fase 0): lê SÓ o vídeo vertical pronto e mede, por bloco (plano entre dois cortes de
imagem), rosto, ombros, respiro, centro do crop e quem aparece. Não mexe na esteira.

  python medir_base.py <video.mov> [<video2> ...] --saida medidas.json [--folhas pasta/]

Métricas por corte (fato medido no arquivo; nada aqui diz quem está FALANDO, isso precisa de gabarito humano):
- blocos: planos detectados pela diferença de histograma HSV entre quadros (limiar LIMIAR_CORTE);
- cabeca_cortada: topo estimado da cabeça (topo da caixa do rosto − MARGEM_CABECA × altura do rosto) acima do quadro, ou rosto encostado
  na borda lateral;
- ombros_cortados: algum ombro (pose) fora do quadro ou invisível num bloco de uma pessoa;
- erro_centro: |ponto médio dos ombros − 0,5| em fração da largura (o quadro É o crop);
- trocas_sujeito_min: troca da pessoa principal (identidade SFace) entre blocos seguidos, por minuto;
- pretos / congelados: blackdetect e freezedetect do ffmpeg;
- tempo de cada módulo.
Modelos: YuNet e SFace (OpenCV Zoo, ao lado deste script), pessoa + pose MediaPipe do OpenCV Zoo (Apache 2.0) em
~/.cw-cortes/modelos/opencv_zoo (ou CW_MODELOS_POSE)."""
import argparse, json, os, re, subprocess, sys, time
from pathlib import Path

import cv2
import numpy as np

AQUI = Path(__file__).resolve().parent
POSE_DIR = Path(os.environ.get("CW_MODELOS_POSE", Path.home() / ".cw-cortes" / "modelos" / "opencv_zoo"))
LIMIAR_CORTE = 0.35      # distância de Bhattacharyya entre histogramas HSV de quadros seguidos = troca de plano
BLOCO_MIN = 0.4          # blocos mais curtos (resíduo, flash) não são medidos, só contados
LADO_ROSTO = 0.005       # rosto a menos disso da borda lateral = cortado
MARGEM_CABECA = 0.5      # topo da cabeça = topo da caixa do YuNet − isto × altura do rosto (inicial; calibrar)
MESMA_PESSOA = 0.363     # limiar de cosseno do SFace (o recomendado pelo OpenCV Zoo)


def _pose():
    sys.path.insert(0, str(POSE_DIR))
    import importlib
    pd = importlib.import_module("person_detection_mediapipe__mp_persondet")
    po = importlib.import_module("pose_estimation_mediapipe__mp_pose")
    return (pd.MPPersonDet(str(POSE_DIR / "person_detection_mediapipe__person_detection_mediapipe_2023mar.onnx"), 0.3, 0.5, 5000),
            po.MPPose(str(POSE_DIR / "pose_estimation_mediapipe__pose_estimation_mediapipe_2023mar.onnx"), 0.5))


def blocos(video, tempos):
    """[(ini, fim)] em segundos: planos entre trocas de imagem."""
    t0 = time.time()
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 29.97
    cap.release()
    proc = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-vf", "scale=90:160", "-pix_fmt", "bgr24",
                             "-f", "rawvideo", "-"], stdout=subprocess.PIPE)   # decodifica e reduz no ffmpeg: ~5x mais rápido que o cv2
    ant, cortes, i = None, [0.0], 0
    while True:
        buf = proc.stdout.read(90 * 160 * 3)
        if len(buf) < 90 * 160 * 3:
            break
        p = np.frombuffer(buf, np.uint8).reshape(160, 90, 3)
        h = cv2.calcHist([cv2.cvtColor(p, cv2.COLOR_BGR2HSV)], [0, 1], None, [16, 8], [0, 180, 0, 256])
        cv2.normalize(h, h)
        if ant is not None and cv2.compareHist(ant, h, cv2.HISTCMP_BHATTACHARYYA) > LIMIAR_CORTE:
            cortes.append(i / fps)
        ant, i = h, i + 1
    proc.wait()
    dur = i / fps
    tempos["blocos"] = round(time.time() - t0, 2)
    return [(a, b) for a, b in zip(cortes, cortes[1:] + [dur]) if b > a], dur, fps


def quadro(video, t):
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, q = cap.read()
    cap.release()
    return q if ok else None


def medir_bloco(q, yn, sf, pdet, pose):
    H, W = q.shape[:2]
    esc = 960 / H
    peq = cv2.resize(q, (int(W * esc), 960))
    yn.setInputSize((peq.shape[1], peq.shape[0]))
    _, fs = yn.detect(peq)
    fs = [] if fs is None else [f for f in fs if f[2] * f[3] > (0.004 * peq.shape[0] * peq.shape[1])]
    r = {"rostos": len(fs)}
    if not fs:
        return r, None
    f = max(fs, key=lambda f: f[2] * f[3])
    x, y, w, h = [float(v) / esc for v in f[:4]]
    topo_cabeca = y - MARGEM_CABECA * h
    r.update(rosto_cx=round((x + w / 2) / W, 3), topo_cabeca=round(topo_cabeca / H, 3),
             cabeca_cortada=bool(topo_cabeca < 0 or x < LADO_ROSTO * W or x + w > (1 - LADO_ROSTO) * W),
             rosto_alt=round(h / H, 3))
    emb = sf.feature(sf.alignCrop(peq, f))
    try:
        pessoas = pdet.infer(q)
    except Exception:
        pessoas = None
    if pessoas is not None and len(pessoas):
        # a pessoa cujo box contém o rosto principal
        cx, cy = x + w / 2, y + h / 2
        melhor = None
        for p in pessoas:
            b = p[0:4]
            if b[0] <= cx <= b[2] and b[1] <= cy <= b[3] + h:
                melhor = p
                break
        if melhor is None:
            melhor = pessoas[0]
        res = pose.infer(q, melhor)
        if res is not None:
            lm = res[1]   # (39, 5): x, y, z, visibilidade, presença (pixels)
            oe, od = lm[11], lm[12]
            vis = min(oe[3], od[3])
            r["ombro_vis"] = round(float(vis), 2)
            fora = any(not (0 <= p[0] <= W and 0 <= p[1] <= H) for p in (oe, od))
            if vis >= 0.5 and not fora:
                mx = (oe[0] + od[0]) / 2 / W
                r.update(ombros_cx=round(float(mx), 3), erro_centro=round(abs(float(mx) - 0.5), 3),
                         larg_sobre_ombros=round(W / max(1.0, abs(float(oe[0] - od[0]))), 2),
                         rosto_x_ombros=round(abs(r["rosto_cx"] - float(mx)), 3))
            r["ombros_cortados"] = bool(fora or vis < 0.5)
    return r, emb


def ffmpeg_det(video, tempos):
    t0 = time.time()
    p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(video), "-vf",
                        "blackdetect=d=0.03:pix_th=0.08,freezedetect=n=0.003:d=0.5", "-an", "-f", "null", "-"],
                       capture_output=True, text=True)
    pretos = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", p.stderr)]
    ini = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", p.stderr)]
    fim = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", p.stderr)]
    tempos["pretos_congelados"] = round(time.time() - t0, 2)
    return pretos, list(zip(ini, fim + [None] * (len(ini) - len(fim))))


def medir(video, yn, sf, pdet, pose, folhas=None):
    tempos = {}
    bl, dur, fps = blocos(video, tempos)
    t0 = time.time()
    medidos, ident, embs = [], [], []
    for a, b in bl:
        if b - a < BLOCO_MIN:
            continue
        q = quadro(video, (a + b) / 2)
        if q is None:
            continue
        m, emb = medir_bloco(q, yn, sf, pdet, pose)
        m.update(ini=round(a, 2), fim=round(b, 2))
        pid = None
        if emb is not None:
            for k, e in enumerate(embs):
                if sf.match(emb, e, cv2.FaceRecognizerSF_FR_COSINE) >= MESMA_PESSOA:
                    pid = k
                    break
            if pid is None:
                embs.append(emb)
                pid = len(embs) - 1
        m["pessoa"] = pid
        medidos.append(m)
        if folhas is not None:
            p = cv2.resize(q, (270, 480))
            cv2.putText(p, "{:.1f}s p{}".format((a + b) / 2, pid), (6, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            ident.append(p)
    tempos["rosto_pose_identidade"] = round(time.time() - t0, 2)
    pretos, congelados = ffmpeg_det(video, tempos)
    tempo_de = {}
    for m in medidos:
        if m["pessoa"] is not None:
            tempo_de[m["pessoa"]] = tempo_de.get(m["pessoa"], 0) + m["fim"] - m["ini"]
    fixas = {k for k, t in tempo_de.items() if t >= max(4.0, 0.08 * dur)}   # quem aparece pouco (b-roll, card) não é sujeito
    seq = [m["pessoa"] for m in medidos if m["pessoa"] in fixas]
    trocas = sum(1 for x, y in zip(seq, seq[1:]) if x != y)
    com_rosto = [m for m in medidos if m["rostos"]]
    um = [m for m in com_rosto if m["rostos"] == 1]
    erros = [m["erro_centro"] for m in com_rosto if "erro_centro" in m]
    res = {
        "video": str(video), "duracao": round(dur, 2), "fps": round(fps, 3),
        "blocos": len(bl), "blocos_curtos_<0.4s": sum(1 for a, b in bl if b - a < BLOCO_MIN), "blocos_medidos": len(medidos),
        "blocos_com_rosto": len(com_rosto), "blocos_1_rosto": len(um), "blocos_2+_rostos": len(com_rosto) - len(um),
        "cabeca_cortada": sum(1 for m in com_rosto if m.get("cabeca_cortada")),
        "ombros_cortados_closes": sum(1 for m in um if m.get("ombros_cortados")),
        "ombros_medidos_closes": sum(1 for m in um if "ombros_cortados" in m),
        "erro_centro_mediano": round(float(np.median(erros)), 3) if erros else None,
        "erro_centro_<=0.03": round(sum(1 for e in erros if e <= 0.03) / len(erros), 3) if erros else None,
        "rosto_vs_ombros_>0.03": sum(1 for m in com_rosto if m.get("rosto_x_ombros", 0) > 0.03),
        "topo_cabeca_mediano": round(float(np.median([m["topo_cabeca"] for m in com_rosto])), 3) if com_rosto else None,
        "larg_sobre_ombros_mediana": round(float(np.median([m["larg_sobre_ombros"] for m in um if "larg_sobre_ombros" in m])), 2)
        if any("larg_sobre_ombros" in m for m in um) else None,
        "pessoas_distintas": len(embs), "pessoas_fixas": len(fixas), "trocas_sujeito_min": round(trocas / (dur / 60), 1) if dur else None,
        "pretos": pretos, "congelados": congelados, "tempos_s": tempos,
        "por_bloco": medidos,
    }
    res["tempo_por_min"] = round(sum(tempos.values()) / (dur / 60), 1) if dur else None
    if folhas is not None and ident:
        Path(folhas).mkdir(parents=True, exist_ok=True)
        linhas = [np.hstack(ident[i:i + 8] + [np.zeros_like(ident[0])] * (8 - len(ident[i:i + 8]))) for i in range(0, len(ident), 8)]
        cv2.imwrite(str(Path(folhas) / (Path(video).stem[:60] + ".jpg")), np.vstack(linhas), [cv2.IMWRITE_JPEG_QUALITY, 70])
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("videos", nargs="+")
    ap.add_argument("--saida", required=True)
    ap.add_argument("--folhas")
    a = ap.parse_args()
    yn = cv2.FaceDetectorYN.create(str(AQUI / "face_detection_yunet.onnx"), "", (540, 960), 0.7, 0.3, 5000)
    sf = cv2.FaceRecognizerSF.create(str(AQUI / "face_recognition_sface.onnx"), "")
    pdet, pose = _pose()
    saida = Path(a.saida)
    todos = json.loads(saida.read_text()) if saida.exists() else {}
    for v in a.videos:
        if v in todos:
            continue
        try:
            todos[v] = medir(v, yn, sf, pdet, pose, a.folhas)
        except Exception as e:
            todos[v] = {"erro": "{}: {}".format(type(e).__name__, e)}
        saida.write_text(json.dumps(todos, ensure_ascii=False, indent=1))
        r = todos[v]
        print(Path(v).name[:50], {k: r.get(k) for k in ("duracao", "blocos", "cabeca_cortada", "ombros_cortados_closes",
                                                         "erro_centro_mediano", "trocas_sujeito_min", "tempo_por_min", "erro")}, flush=True)


if __name__ == "__main__":
    main()
