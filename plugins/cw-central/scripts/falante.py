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


def vozes(video):
    """[(t, voz 0|1)] a cada 0,5 s com fala: MFCC (13) média e desvio em janela de 1 s, normalizados no corte, em 2 grupos."""
    import numpy as np
    a = np.frombuffer(subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", str(SR), "-f",
                                      "s16le", "-"], capture_output=True).stdout, np.int16).astype(np.float32) / 32768
    if len(a) < SR * 4:
        return []
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
            ts.append(round(i / 100 + 0.5, 2))   # centro da janela
            X.append(np.concatenate([m[i:i + 100][vo].mean(0), m[i:i + 100][vo].std(0)]))
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


def medir_voz(video):
    """{"ok", "motivo", "pessoa_da_voz", "falas": [[ini, fim, pessoa]], "amostras": [{"t", "rostos": [[cx, cy, pessoa]]}]}.
    ok=False quando não dá para confiar (uma pessoa só, rostos parecidos demais, vozes que não batem com quem aparece)."""
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
    amostras = [{"t": t, "rostos": por_t[t]} for t in sorted(por_t)]
    if tot < 10 or acerto < 0.8 or min(sum(1 for _, v in vz if v == j) for j in (0, 1)) < 0.1 * len(vz):
        # vozes não separam (quase sempre: uma pessoa falando o corte todo e o k-means partindo a mesma voz em dois). Quem conduz
        # é quem o episódio mostra SOZINHO a maior parte do tempo; no plano aberto o recorte fica nele (l19c03: o convidado falava
        # e o plano aberto mostrava o apresentador ouvindo)
        sozinho = [q[0][2] for q in por_t.values() if len(q) == 1]
        if len(sozinho) >= 10:
            pr = max((0, 1), key=sozinho.count)
            if sozinho.count(pr) >= 0.75 * len(sozinho):
                return {"ok": True, "modo": "principal", "acerto": round(acerto, 2), "semelhanca_rostos": round(sim, 2),
                        "falas": [[0.0, round(max(t for t, _ in vz) + 1, 2), pr]], "amostras": amostras,
                        "motivo": "vozes não separam ({:.0%}); recorte em quem conduz (sozinho em {:.0%} dos closes)".format(
                            acerto, sozinho.count(pr) / len(sozinho))}
        return {"ok": False, "motivo": "vozes não batem com quem aparece ({:.0%} em {} amostras)".format(acerto, tot)}
    pv = [0, 1] if reto >= cruz else [1, 0]   # voz -> pessoa
    # falas contínuas por pessoa (trocas curtas são absorvidas)
    falas = []
    for t, v in vz:
        pe = pv[v]
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
    return {"ok": True, "modo": "voz", "acerto": round(acerto, 2), "semelhanca_rostos": round(sim, 2), "pessoa_da_voz": pv,
            "falas": [[round(a, 2), round(b, 2), p] for a, b, p in lim], "amostras": amostras}


def quem_fala_em(med, t0, t1):
    """Pessoa que mais fala em [t0, t1) segundo medir_voz (None se ninguém)."""
    tempo = {}
    for a, b, p in med.get("falas") or []:
        s = min(b, t1) - max(a, t0)
        if s > 0:
            tempo[p] = tempo.get(p, 0) + s
    return max(tempo, key=tempo.get) if tempo else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--de", type=float, default=0.0)
    ap.add_argument("--ate", type=float)
    ap.add_argument("--json")
    ap.add_argument("--voz", action="store_true", help="quem fala pela voz + quem aparece pelo rosto")
    a = ap.parse_args()
    txt = json.dumps(medir_voz(a.video) if a.voz else medir(a.video, a.de, a.ate), ensure_ascii=False)
    if a.json:
        Path(a.json).write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    sys.exit(main())
