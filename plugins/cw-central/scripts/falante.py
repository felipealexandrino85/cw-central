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
  {"v": 2, "planos": [{"ini", "fim", "rostos": [{"cx", "cy", "larg", "sync", "janelas", "serie"}], "falante": índice|null,
               "estado": "falando"|"fora_de_quadro"|"incerto"}],
   "fora_de_quadro": [[ini, fim], ...]}   trechos com voz em que ninguém na tela está falando
cx/cy = centro do rosto em fração do quadro horizontal (0–1). serie = [[segundo do meio da janela, sync], ...] (janela de 2 s a
cada 1 s): quem fala JANELA A JANELA, para o recorte trocar de pessoa quando a fala passa de um para o outro no mesmo plano.
"""
import argparse
import json
import re
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
            jan, serie = [], []
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
                    serie.append([round(de + (a + (j0 + j1) / 2) / FPS, 2), round(max(cs), 2)])
            rostos.append({"cx": round(float(t["cx"]), 3), "cy": round(float(t["cy"]), 3), "larg": int(np.median(t["lg"])),
                           "sync": round(float(np.median(jan)), 2) if jan else None, "janelas": len(jan), "serie": serie})
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
    return {"v": 2, "planos": planos, "fora_de_quadro": fora}


# ------------------------------------------------------------------ quem fala pela VOZ + quem aparece pelo ROSTO (02/11/2026)
# A boca falha quando o microfone tapa o rosto (convidado do João Barcelos, lote 19: "fora de quadro" em 30 dos 36 s em que ele
# falava) e no plano aberto o rosto é pequeno demais. Aqui: as duas vozes do corte separadas pelo timbre (MFCC + k-means), as duas
# pessoas pelo rosto (SFace, mesmo pacote do YuNet), e cada voz ligada à pessoa que mais aparece sozinha enquanto ela fala
# (o episódio editado mostra quem fala na maior parte do tempo). Calibrado no l19c01: rosto certo em 147/148 amostras.
SR = 16000
PASSO_VOZ = 0.5    # uma janela de voz (1 s) a cada 0,5 s
VOZ_MIN = 1.2      # troca de quem fala mais curta que isso não move o recorte (s)


def _kmeans(X, k=2, it=40):
    import numpy as np
    best = None
    for s in range(8):
        r = np.random.default_rng(s)
        C = X[r.choice(len(X), k, replace=False)]
        for _ in range(it):
            lab = ((X[:, None] - C[None]) ** 2).sum(2).argmin(1)
            C = np.array([X[lab == j].mean(0) if (lab == j).any() else C[j] for j in range(k)])
        inn = float(((X - C[lab]) ** 2).sum())
        if best is None or inn < best[0]:
            best = (inn, lab, C)
    return best[1], best[2]


def _audio(video, de=None, dur=None):
    import numpy as np
    cmd = ["ffmpeg", "-nostdin", "-v", "error"] + (["-ss", "{:.2f}".format(de)] if de else []) + ["-i", str(video)] \
        + (["-t", "{:.2f}".format(dur)] if dur else []) + ["-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True).stdout, np.int16).astype(np.float32) / 32768


def _janelas_voz(a, t0=0.0):
    """(ts, X): uma janela de 1 s a cada 0,5 s com fala; X = média e desvio do MFCC (13), normalizado no trecho (tira o canal)."""
    import numpy as np
    if len(a) < SR * 4:
        return [], []
    n, hop = 400, 160
    fr = np.lib.stride_tricks.sliding_window_view(a, n)[::hop] * np.hamming(n)
    sp = np.abs(np.fft.rfft(fr, 512)) ** 2
    mel, imel = (lambda f: 2595 * np.log10(1 + f / 700)), (lambda m: 700 * (10 ** (m / 2595) - 1))
    b = np.floor(513 * imel(np.linspace(mel(80), mel(7600), 28)) / SR).astype(int)
    fb = np.zeros((26, 257))
    for i in range(26):
        fb[i, b[i]:b[i + 1]] = (np.arange(b[i], b[i + 1]) - b[i]) / max(1, b[i + 1] - b[i])
        fb[i, b[i + 1]:b[i + 2]] = (b[i + 2] - np.arange(b[i + 1], b[i + 2])) / max(1, b[i + 2] - b[i + 1])
    m = np.log(sp @ fb.T + 1e-10) @ np.cos(np.pi / 26 * (np.arange(26) + 0.5)[None, :] * np.arange(1, 14)[:, None]).T
    m = (m - m.mean(0)) / (m.std(0) + 1e-6)
    en = np.log(sp.sum(1) + 1e-10)
    lim = np.percentile(en, 40)
    ts, X = [], []
    for i in range(0, len(m) - 100, int(PASSO_VOZ * 100)):
        vo = en[i:i + 100] > lim
        if vo.mean() >= 0.5:
            ts.append(round(t0 + i / 100 + 0.5, 2))   # centro da janela
            X.append(np.concatenate([m[i:i + 100][vo].mean(0), m[i:i + 100][vo].std(0)]))
    return ts, X


def vozes(video):
    """[(t, voz 0|1)] a cada 0,5 s com fala: MFCC (13) média e desvio em janela de 1 s, normalizados no corte, em 2 grupos."""
    import numpy as np
    ts, X = _janelas_voz(_audio(video))
    if len(X) < 10:
        return []
    lab, _ = _kmeans(np.array(X))
    return list(zip(ts, (int(x) for x in lab)))


def rostos_id(video, fps=2):
    """[(t, cx, cy, pessoa 0|1)] e a semelhança entre as 2 pessoas: rostos (YuNet) com identidade pelo SFace."""
    import cv2
    import numpy as np
    W, H = tamanho(video)
    w2, h2 = 1280, int(round(H * 1280 / W / 2) * 2)
    aqui = Path(__file__).resolve().parent
    yn = cv2.FaceDetectorYN.create(str(aqui / "face_detection_yunet.onnx"), "", (w2, h2), 0.7, 0.3, 5000)
    sf = cv2.FaceRecognizerSF.create(str(aqui / "face_recognition_sface.onnx"), "")
    p = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-vf", "fps={},scale={}:{}".format(fps, w2, h2),
                          "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)
    achados, k = [], 0
    while True:
        b = p.stdout.read(w2 * h2 * 3)
        if len(b) < w2 * h2 * 3:
            break
        im = np.frombuffer(b, np.uint8).reshape(h2, w2, 3)
        _, fs = yn.detect(im)
        for f in (fs if fs is not None else []):
            if f[2] < w2 * 0.035:
                continue   # rosto minúsculo no fundo
            e = sf.feature(sf.alignCrop(im, f)).flatten()
            achados.append((round(k / fps, 2), float((f[0] + f[2] / 2) / w2), float((f[1] + f[3] / 2) / h2), e / (np.linalg.norm(e) + 1e-9)))
        k += 1
    p.wait()
    if len(achados) < 6:
        return [], 1.0
    lab, C = _kmeans(np.array([x[3] for x in achados]))
    sim = float(C[0] @ C[1] / (np.linalg.norm(C[0]) * np.linalg.norm(C[1]) + 1e-9))
    return [(t, cx, cy, int(l)) for (t, cx, cy, _), l in zip(achados, lab)], sim


# ---------------------------------------------------------------------------------------------- mapa do episódio
# Felipe, 09/11/2026 ("alguns enquadramentos continuam errados"): separar as vozes DENTRO de cada corte (~60 s, sempre 2 pessoas)
# falhava quando o corte tinha pouco close de alguém ou 3 pessoas na mesa (host + 2 convidados, lote 31), e aí a boca (fraca no
# plano aberto) decidia. O mapa aprende UMA vez, no episódio inteiro: quem é quem pelo rosto (SFace) e pela voz (MFCC), e liga
# cada voz ao rosto que aparece sozinho enquanto ela fala. Cada corte só classifica a voz e os rostos contra o mapa.
CLOSE = 0.09        # largura mínima do rosto (fração da imagem) para contar como close de uma pessoa
MAPA_ROSTO = 0.40   # semelhança SFace para ser a mesma pessoa (o OpenCV recomenda 0,363)


def _rostos_keyframes(video, cada=1.0, lado=1280):
    """[(t, cx, cy, emb)] dos quadros-chave (no máximo um a cada `cada` s): rápido mesmo no episódio de 1 h em 4K."""
    import cv2
    import numpy as np
    import tempfile
    W, H = tamanho(video)
    w2, h2 = lado, int(round(H * lado / W / 2) * 2)
    aqui = Path(__file__).resolve().parent
    yn = cv2.FaceDetectorYN.create(str(aqui / "face_detection_yunet.onnx"), "", (w2, h2), 0.7, 0.3, 5000)
    sf = cv2.FaceRecognizerSF.create(str(aqui / "face_recognition_sface.onnx"), "")
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(["ffmpeg", "-nostdin", "-skip_frame", "nokey", "-i", str(video), "-vf",
                              "select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{})',scale={}:{},showinfo".format(cada, w2, h2),
                              "-vsync", "vfr", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE, stderr=err)
        quadros = []
        while True:
            b = p.stdout.read(w2 * h2 * 3)
            if len(b) < w2 * h2 * 3:
                break
            im = np.frombuffer(b, np.uint8).reshape(h2, w2, 3)
            _, fs = yn.detect(im)
            q = []
            for f in (fs if fs is not None else []):
                if f[2] < w2 * 0.035:
                    continue
                e = sf.feature(sf.alignCrop(im, f)).flatten()
                q.append((float((f[0] + f[2] / 2) / w2), float((f[1] + f[3] / 2) / h2), e / (np.linalg.norm(e) + 1e-9), float(f[2] / w2)))
            quadros.append(q)
        p.wait()
        err.seek(0)
        tempos = [float(x) for x in re.findall(r"pts_time:\s*([\d.]+)", err.read().decode("utf-8", "replace"))]
    return [(round(t, 2), q) for t, q in zip(tempos, quadros)]


def _agrupar_rostos(embs):
    """Rótulo de cada rosto (mesma pessoa = semelhança ≥ MAPA_ROSTO com o centro do grupo) e os centros."""
    import numpy as np
    C, cont, lab = [], [], []
    for e in embs:
        if C:
            sims = np.array(C) @ e
            j = int(sims.argmax())
            if sims[j] >= MAPA_ROSTO:
                c = C[j] * cont[j] + e
                C[j] = c / (np.linalg.norm(c) + 1e-9)
                cont[j] += 1
                lab.append(j)
                continue
        C.append(e.copy())
        cont.append(1)
        lab.append(len(C) - 1)
    return lab, C, cont


VOZ_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
           "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx")   # CAM++ (3D-Speaker, Apache-2.0), 28 MB
_VOZ = {}


def _modelo_voz():
    """Impressão de voz (CAM++) pelo OpenCV DNN, o mesmo do YuNet/SFace: nada a instalar. Baixa uma vez para ~/.cache."""
    if "net" not in _VOZ:
        import cv2
        import urllib.request
        arq = Path.home() / ".cache" / "cw-falante" / "campplus.onnx"
        if not arq.exists():
            arq.parent.mkdir(parents=True, exist_ok=True)
            tmp = arq.with_suffix(".baixando")
            urllib.request.urlretrieve(VOZ_URL, tmp)
            tmp.rename(arq)
        _VOZ["net"] = cv2.dnn.readNetFromONNX(str(arq))
    return _VOZ["net"]


def _fbank(x):
    """80 filtros mel (estilo Kaldi: 25 ms, passo 10 ms, janela povey), menos a média: a entrada do CAM++."""
    import numpy as np
    n, hop = 400, 160
    fr = np.lib.stride_tricks.sliding_window_view(x, n)[::hop].astype(np.float64)
    fr = fr - fr.mean(1, keepdims=True)
    fr = np.concatenate([fr[:, :1] * 0.03, fr[:, 1:] - 0.97 * fr[:, :-1]], 1)
    fr = fr * ((0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / (n - 1))) ** 0.85)
    sp = np.abs(np.fft.rfft(fr, 512)) ** 2
    mel = lambda f: 1127 * np.log(1 + f / 700)
    mp = np.linspace(mel(20), mel(8000), 82)
    fm = mel(np.arange(257) * 16000 / 512)
    fb = np.stack([np.clip(np.minimum((fm - mp[i]) / (mp[i + 1] - mp[i]), (mp[i + 2] - fm) / (mp[i + 2] - mp[i + 1])), 0, None)
                   for i in range(80)])
    f = np.log(np.maximum(sp @ fb.T, 1e-10))
    return (f - f.mean(0)).astype(np.float32)


def _impressoes(a, ts, t0=0.0, meia=0.75):
    """Impressão de voz (192, norma 1) de 1,5 s em volta de cada instante de ts (em s; a começa em t0)."""
    import numpy as np
    net = _modelo_voz()
    out = []
    for t in ts:
        i0 = max(0, int((t - t0 - meia) * SR))
        seg = a[i0:i0 + int(2 * meia * SR)]
        if len(seg) < SR // 2:
            seg = np.pad(seg, (0, SR // 2 - len(seg)))
        net.setInput(_fbank(seg)[None])
        e = net.forward().flatten()
        out.append(e / (np.linalg.norm(e) + 1e-9))
    return np.array(out)


def mapa_episodio(video):
    """{"ok", "motivo", "pessoas": [{"id", "rosto": [128], "voz": [192] ou None, "cx": lado médio no plano aberto, "peso"}],
    "acerto"}. A voz de cada pessoa é aprendida nos closes dela (o episódio editado mostra quem fala quase sempre) e conferida
    em metade dos closes que não entrou no aprendizado."""
    import numpy as np
    qs = _rostos_keyframes(video)
    embs = [x[2] for _, q in qs for x in q]
    if len(embs) < 30:
        return {"ok": False, "motivo": "poucos rostos no episódio ({})".format(len(embs))}
    lab, C, cont = _agrupar_rostos(embs)
    total = len(embs)
    gente = [j for j in sorted(range(len(C)), key=lambda j: -cont[j]) if cont[j] >= max(10, 0.04 * total)][:5]
    if len(gente) < 2:
        return {"ok": False, "motivo": "uma pessoa só no episódio"}
    ident = {g: k for k, g in enumerate(gente)}
    por_t, i = [], 0   # [(t, [(cx, pessoa|-1)])]
    for t, q in qs:
        por_t.append((t, [(x[0], ident.get(lab[i + k], -1), x[3]) for k, x in enumerate(q)]))
        i += len(q)
    P = len(gente)
    # close de verdade: um rosto só E grande (no plano aberto de 3 pessoas o YuNet às vezes acha um rosto só, pequeno; CW Cast #48)
    solo = [(t, q[0][1]) for t, q in por_t if len(q) == 1 and q[0][1] >= 0 and q[0][2] >= CLOSE]
    a = _audio(video)
    ts, _ = _janelas_voz(a)   # só os instantes com fala
    ts = np.array(ts)
    amostra, ys, grupo = [], [], []
    for n, (t, pe) in enumerate(solo):
        for j in np.where(np.abs(ts - t) <= 1.0)[0]:
            amostra.append(float(ts[j]))
            ys.append(pe)
            grupo.append((n // 10) % 2)   # blocos de 10 closes: metade aprende, metade confere
    if len(amostra) < 60:
        return {"ok": False, "motivo": "pouca fala nos closes ({} janelas)".format(len(amostra))}
    E = _impressoes(a, amostra)
    ys, grupo = np.array(ys), np.array(grupo)

    def centros(sel):
        cs = []
        for pe in range(P):
            Xp = E[sel & (ys == pe)]
            if len(Xp) < 15:
                cs.append(None)   # quase não fala nos closes: sem voz no mapa
                continue
            c = Xp.mean(0)
            d = Xp @ (c / np.linalg.norm(c))
            c = Xp[d >= np.percentile(d, 30)].mean(0)   # tira os 30% mais longe (close de quem ouve, risada, voz cruzada)
            cs.append(c / np.linalg.norm(c))
        n_ = [int((sel & (ys == pe)).sum()) for pe in range(P)]
        for x in sorted(range(P), key=lambda pe: n_[pe]):   # voz quase igual à de outra pessoa: era close de quem só reage
            if cs[x] is not None and any(y != x and cs[y] is not None and n_[y] >= n_[x] and float(cs[x] @ cs[y]) > 0.8
                                         for y in range(P)):
                cs[x] = None   # (CW Cast #48: a "voz" do 3º rosto era 0,94 igual à do convidado)
        return cs

    acertos = []
    for g in (0, 1):
        cs = centros(grupo == g)
        ok = [pe for pe in range(P) if cs[pe] is not None]
        te = (grupo != g) & np.isin(ys, ok)
        if len(ok) < 2 or te.sum() < 20:
            continue
        pred = np.array(ok)[(E[te] @ np.array([cs[pe] for pe in ok]).T).argmax(1)]
        acertos.append(float(np.mean([(pred[ys[te] == pe] == pe).mean() for pe in ok])))   # média por pessoa (não só o host)
    cs = centros(np.ones(len(ys), bool))
    falam = [pe for pe in range(P) if cs[pe] is not None]
    acerto = min(acertos) if acertos else 0
    if len(falam) < 2 or acerto < 0.70:
        return {"ok": False, "motivo": "vozes não separam no episódio (acerto {:.0%}, {} pessoa(s) com voz)".format(acerto, len(falam))}
    pessoas = []
    for k, g in enumerate(gente):
        cxs = sorted(cx for _, q in por_t if len(q) >= 2 for cx, pe, _ in q if pe == k)
        pessoas.append({"id": k, "rosto": [round(float(x), 5) for x in C[g]],
                        "voz": [round(float(x), 5) for x in cs[k]] if cs[k] is not None else None,
                        "cx": round(cxs[len(cxs) // 2], 3) if cxs else None, "peso": round(cont[g] / total, 3)})
    return {"ok": True, "acerto": round(acerto, 2), "amostras": len(amostra), "pessoas": pessoas}


def _medir_com_mapa(video, mapa, confia=False):
    """Como medir_voz, mas a voz e os rostos do corte são classificados contra o mapa do episódio (ids estáveis no episódio)."""
    import cv2
    import numpy as np
    pe = mapa["pessoas"]
    R = np.array([p["rosto"] for p in pe])
    com_voz = [p["id"] for p in pe if p.get("voz")]
    V = np.array([pe[k]["voz"] for k in com_voz])
    a = _audio(video)
    ts, _ = _janelas_voz(a)
    if len(ts) < 6:
        return None
    vp = [com_voz[int(j)] for j in (_impressoes(a, ts) @ V.T).argmax(1)]
    W, H = tamanho(video)
    w2, h2 = 1280, int(round(H * 1280 / W / 2) * 2)
    aqui = Path(__file__).resolve().parent
    yn = cv2.FaceDetectorYN.create(str(aqui / "face_detection_yunet.onnx"), "", (w2, h2), 0.7, 0.3, 5000)
    sf = cv2.FaceRecognizerSF.create(str(aqui / "face_recognition_sface.onnx"), "")
    pr = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-vf", "fps=2,scale={}:{}".format(w2, h2),
                           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)
    por_t, k = {}, 0
    while True:
        b = pr.stdout.read(w2 * h2 * 3)
        if len(b) < w2 * h2 * 3:
            break
        im = np.frombuffer(b, np.uint8).reshape(h2, w2, 3)
        _, fs = yn.detect(im)
        rs = []
        for f in (fs if fs is not None else []):
            if f[2] < w2 * 0.035:
                continue
            e = sf.feature(sf.alignCrop(im, f)).flatten()
            e = e / (np.linalg.norm(e) + 1e-9)
            sims = R @ e
            j = int(sims.argmax())
            rs.append([round(float((f[0] + f[2] / 2) / w2), 3), round(float((f[1] + f[3] / 2) / h2), 3), j if sims[j] >= 0.30 else -1,
                       round(float(f[2] / w2), 3)])
        if rs:
            por_t[round(k / 2, 2)] = rs
        k += 1
    pr.wait()
    if not por_t:
        return None
    certo = tot = 0
    for t, p in zip(ts, vp):
        q = por_t.get(round(round(t * 2) / 2, 2)) or []
        if len(q) == 1 and q[0][2] >= 0 and q[0][3] >= CLOSE:
            tot += 1
            certo += q[0][2] == p
    acerto = certo / tot if tot else None
    if not confia and tot >= 10 and acerto < 0.6:   # o mapa não serve para este corte (outra gravação, outra mesa): mede só no corte
        return None
    return {"ts": ts, "vp": vp, "por_t": por_t, "acerto": acerto}


def medir_voz(video, mapa=None, confia_mapa=False):
    """{"ok", "motivo", "pessoa_da_voz", "falas": [[ini, fim, pessoa]], "amostras": [{"t", "rostos": [[cx, cy, pessoa]]}]}.
    ok=False quando não dá para confiar (uma pessoa só, rostos parecidos demais, vozes que não batem com quem aparece).
    Com o mapa do episódio (mapa_episodio) a voz e o rosto vêm dele (modo "mapa"); sem mapa ou se ele não bater, mede no corte."""
    if mapa and mapa.get("ok"):
        try:
            mm = _medir_com_mapa(video, mapa, confia_mapa)
        except Exception as e:   # modelo de voz indisponível (download, OpenCV antigo): mede só no corte, como antes
            print("mapa do episódio não usado:", e, file=sys.stderr)
            mm = None   # confia_mapa: conferência do vídeo final (lá o erro É o que se mede)
        if mm:
            return _falas(list(zip(mm["ts"], mm["vp"])), None, mm["por_t"], "mapa",
                          {"acerto": round(mm["acerto"], 2) if mm["acerto"] is not None else None, "pessoas": len(mapa["pessoas"])})
    vz = vozes(video)
    rs, sim = rostos_id(video)
    if not vz or not rs:
        return {"ok": False, "motivo": "sem voz ou sem rosto"}
    if sim > 0.6 or min(sum(1 for r in rs if r[3] == j) for j in (0, 1)) < 0.08 * len(rs):
        return {"ok": False, "motivo": "uma pessoa só na tela (ou rostos parecidos demais: {:.2f})".format(sim)}
    por_t = {}
    for t, cx, cy, pe in rs:
        por_t.setdefault(t, []).append([round(cx, 3), round(cy, 3), pe])
    # voz × pessoa nos quadros com UMA pessoa na tela
    cont = [[0, 0], [0, 0]]
    for t, v in vz:
        q = por_t.get(round(round(t * 2) / 2, 2)) or []
        if len(q) == 1:
            cont[v][q[0][2]] += 1
    reto, cruz = cont[0][0] + cont[1][1], cont[0][1] + cont[1][0]
    tot = reto + cruz
    acerto = max(reto, cruz) / tot if tot else 0
    if tot < 10 or acerto < 0.8 or min(sum(1 for _, v in vz if v == j) for j in (0, 1)) < 0.1 * len(vz):
        # vozes não separam: não chuta. O modo "principal" (quem aparece sozinho mais tempo fala o corte todo) errava o plano
        # aberto da abertura (l31c02, 06/11/2026: o apresentador falava e o recorte ficou no convidado); quem decide é a boca
        return {"ok": False, "motivo": "vozes não batem com quem aparece ({:.0%} em {} amostras)".format(acerto, tot)}
    pv = [0, 1] if reto >= cruz else [1, 0]   # voz -> pessoa
    return _falas(vz, pv, por_t, "voz", {"acerto": round(acerto, 2), "semelhanca_rostos": round(sim, 2), "pessoa_da_voz": pv})


def _falas(vz, pv, por_t, modo, extra):
    """Falas contínuas por pessoa a partir de [(t, voz)] (pv: voz -> pessoa; None = a voz já é a pessoa)."""
    falas = []
    for t, v in vz:
        pe = v if pv is None else pv[v]
        if falas and falas[-1][2] == pe and t - falas[-1][1] <= 1.0:
            falas[-1][1] = t + PASSO_VOZ / 2
        else:
            falas.append([t - PASSO_VOZ / 2, t + PASSO_VOZ / 2, pe])
    lim = []
    for f in falas:
        if lim and (f[1] - f[0] < VOZ_MIN) and lim[-1][2] != f[2]:
            lim[-1][1] = f[1]   # troca curta demais: fica com quem já falava
        elif lim and lim[-1][2] == f[2]:
            lim[-1][1] = f[1]
        else:
            lim.append(list(f))
    return dict(extra, ok=True, modo=modo, falas=[[round(a, 2), round(b, 2), p] for a, b, p in lim],
                amostras=[{"t": t, "rostos": por_t[t]} for t in sorted(por_t)])


def quem_fala_em(med, t0, t1):
    """Pessoa que mais fala em [t0, t1) segundo medir_voz (None se ninguém)."""
    tempo = {}
    for a, b, p in med.get("falas") or []:
        s = min(b, t1) - max(a, t0)
        if s > 0:
            tempo[p] = tempo.get(p, 0) + s
    return max(tempo, key=tempo.get) if tempo else None


def rosto_do_lado(cxs, lado):
    """Índice do rosto daquele lado da imagem (cxs = centro x de cada rosto, 0–1), para o lado pedido pelo editor
    (esquerda/direita/meio). None se não dá para dizer (um rosto só; "meio" com menos de 3)."""
    if len(cxs) < 2:
        return None
    ordem = sorted(range(len(cxs)), key=lambda j: cxs[j])
    if lado == "esquerda":
        return ordem[0]
    if lado == "direita":
        return ordem[-1]
    return ordem[len(ordem) // 2] if len(cxs) >= 3 else None


def trechos_do_lado(lados, ini, fim):
    """Partes [a, b, lado] do plano [ini, fim) cobertas pelo lado pedido ({"t0", "t1", "lado"}; t0 None = o corte todo;
    t1 vazio ou igual a t0 = o plano de câmera daquele momento)."""
    out = []
    for l in lados or []:
        t0, t1 = l.get("t0"), l.get("t1")
        if t0 is None:
            a, b = ini, fim
        elif t1 is None or t1 <= t0:
            if not ini <= t0 < fim:
                continue
            a, b = ini, fim
        else:
            a, b = max(ini, t0), min(fim, t1)
        if b - a > 0.2:
            out.append([a, b, l["lado"]])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--de", type=float, default=0.0)
    ap.add_argument("--ate", type=float)
    ap.add_argument("--json")
    ap.add_argument("--voz", action="store_true", help="quem fala pela voz + quem aparece pelo rosto")
    ap.add_argument("--mapa", help="mapa do episódio (json): com --voz classifica contra ele; com --criar-mapa grava nele")
    ap.add_argument("--criar-mapa", action="store_true", help="aprende quem é quem no episódio inteiro (video = bruto)")
    ap.add_argument("--confia-mapa", action="store_true", help="com --voz --mapa: não troca para a medida no corte se o mapa discordar")
    a = ap.parse_args()
    if a.criar_mapa:
        try:
            res = mapa_episodio(a.video)
        except Exception as e:   # grava a falha: a esteira não tenta de novo a cada corte
            res = {"ok": False, "motivo": "mapa falhou: {}".format(str(e)[:200])}
        if a.mapa:
            Path(a.mapa).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({k: v for k, v in res.items() if k != "pessoas"} | {"pessoas": [{k: v for k, v in p.items() if k not in ("rosto", "voz")}
                                                                                     for p in res.get("pessoas") or []]}, ensure_ascii=False))
        return 0
    mapa = None
    if a.voz and a.mapa:
        try:
            mapa = json.loads(Path(a.mapa).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            mapa = None
    txt = json.dumps(medir_voz(a.video, mapa, a.confia_mapa) if a.voz else medir(a.video, a.de, a.ate), ensure_ascii=False)
    if a.json:
        Path(a.json).write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    sys.exit(main())
