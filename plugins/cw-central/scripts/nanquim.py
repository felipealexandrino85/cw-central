#!/usr/bin/env python3
"""Motor do estilo Nanquim · Reflexivo P&B (cortes emocionais: quedas, viradas, conselhos para donos de restaurante).

Molde "Superpoder" (referências em Downloads/Estilo, decupadas quadro a quadro em 05/11/2026):
  - 1080x1920, a imagem do episódio numa faixa 4:3 (1080x810, y = 555 a 1365) com preto puro em cima e embaixo;
  - P&B de contraste alto, recorte que acompanha o rosto de quem fala (suave dentro do plano, reenquadre em cada emenda);
  - abertura em fenda: a faixa nasce como uma fenda de 50 px no meio e abre linear até a faixa cheia em 0,9 s;
  - legenda em blocos de 1 a 4 linhas montados palavra por palavra (pequeno / médio / palavra-chave grande com desfoque),
    na metade de baixo da faixa, alinhamento alternando entre os blocos;
  - fim: a imagem segura ~0,6 s depois da última palavra, corte seco para o preto com o CTA em máquina de escrever,
    a trilha continua e some até o silêncio (card de 3 s);
  - trilha licenciada (catálogo em nanquim/trilhas.json, baixada sob demanda), ~12 dB abaixo da voz, com o ponto alto
    encaixado na virada do corte. Master −14 LUFS, pico ≤ −1 dBTP.

Uso: python nanquim.py plano.json
O plano é montado pela esteira (scripts/esteira.py, produzir_nanquim); o Claude só escolhe a palavra-chave de cada frase,
as frases médias, a virada e o clima da trilha. Sem b-roll, sem foto, sem SFX, sem logo.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 1080, 1920, 30
FW, FH = 1080, 810                  # faixa 4:3
FY = (H - FH) // 2                  # 555
AQUI = Path(__file__).resolve().parent
FONTE = Path(os.environ.get("NANQUIM_FONTE", AQUI / "dark01" / "fonts" / "Montserrat-ExtraBold.ttf"))
CATALOGO = AQUI / "nanquim" / "trilhas.json"
CACHE = Path(os.environ.get("NANQUIM_CACHE", Path.home() / ".cache" / "nanquim"))

PEQ, MED, GRA = 52, 74, 106         # altura de maiúscula ≈ 38 / 54 / 77 px (Montserrat: maiúscula ≈ 0,72 da fonte)
TRACK = -0.025                      # letras quase encostadas
ZOOM_MAX = 1.75                     # no plano aberto do CW Cast a cabeça é pequena: até 1,75× (recorte de 823 px de largura do 1080p)
ABRE = 27                           # quadros da abertura em fenda
SEGURA = 0.6                        # respiro depois da última palavra
CARD = 3.0                          # tela preta com o CTA e a trilha sumindo
CTA = ["ME SIGA PARA MAIS", "CONTEÚDOS COMO ESTE"]
CTA_KICKER = "GOSTOU DESSA HISTÓRIA?"
FUNCAO = {"o", "a", "os", "as", "um", "uma", "de", "do", "da", "dos", "das", "no", "na", "nos", "nas", "e", "que", "pra", "pro",
          "em", "eu", "ele", "ela", "se", "me", "te", "lhe", "por", "com", "sem", "mas", "ou", "é", "tá", "isso", "esse", "essa"}


def clamp(x, a=0.0, b=1.0):
    return a if x < a else b if x > b else x


def norm(t):
    t = unicodedata.normalize("NFD", (t or "").lower())
    return re.sub(r"[^\w%]", "", "".join(c for c in t if unicodedata.category(c) != "Mn"))


def limpa(w):
    """Palavra como vai para a legenda: sem pontuação de fim (a interrogação fica), caixa alta."""
    w = re.sub(r"[.,;:…\"“”!]+$", "", (w or "").strip())
    return re.sub(r"^[\"“”¿¡]+", "", w).upper()


# ---------------------------------------------------------------- frases (a esteira usa a mesma divisão no prompt)
def palavras_do(arq):
    pb = json.loads(Path(arq).read_text(encoding="utf-8"))
    pb = pb.get("words", pb) if isinstance(pb, dict) else pb
    out = [{"w": w.get("w") or w.get("word") or "", "i": float(w["start"] if "start" in w else w.get("i", 0)),
            "f": float(w["end"] if "end" in w else w.get("f", 0))} for w in pb if (w.get("w") or w.get("word") or "").strip()]
    for k in range(1, len(out)):   # mantém a ordem da fala (ordenar pelo tempo trocava palavras coladas: "VOU INVESTIR")
        out[k]["i"] = max(out[k]["i"], out[k - 1]["i"])
        out[k]["f"] = max(out[k]["f"], out[k]["i"])
    return out


def frases(ws):
    """Divide a fala em blocos de legenda (uma ideia curta cada): fecha em ponto, em vírgula depois de 3+ palavras, em pausa
    de 0,28 s ou com 8 palavras (sem deixar palavra de ligação sozinha no fim). Devolve listas de índices de ws."""
    out, cur = [], []
    for k, w in enumerate(ws):
        cur.append(k)
        prox = ws[k + 1] if k + 1 < len(ws) else None
        txt = w["w"].strip()
        fecha = (prox is None or re.search(r"[.?!…]$", txt) or (re.search(r"[,;:]$", txt) and len(cur) >= 3)
                 or prox["i"] - w["f"] > 0.28 or len(cur) >= 8)
        so_tamanho = fecha and prox is not None and len(cur) >= 8 and not re.search(r"[.?!…,;:]$", txt) and prox["i"] - w["f"] <= 0.28
        if so_tamanho:
            dep = ws[k + 2] if k + 2 < len(ws) else None
            if len(cur) < 10 and (re.search(r"[.?!…,;:]$", prox["w"].strip()) or dep is None or dep["i"] - prox["f"] > 0.28):
                fecha = False   # a palavra seguinte fecha a frase: não deixa "CASA" sozinha no bloco de baixo
            elif norm(txt) in FUNCAO:
                fecha = False   # "... SEM SENTIR A" | "OBRIGAÇÃO": a ligação vai junto para o próximo bloco
        if fecha and prox is not None and len(cur) == 1 and norm(txt) in FUNCAO and prox["i"] - w["f"] <= 0.28:
            fecha = False
        if fecha:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def texto_frases(ws):
    """Para o prompt: "3 | 12.4 s | NOS DIAS ATUAIS ISSO É QUASE UM SUPERPODER"."""
    return "\n".join("{} | {:.1f} s | {}".format(n, ws[b[0]]["i"], " ".join(limpa(ws[k]["w"]) for k in b)) for n, b in enumerate(frases(ws)))


# ---------------------------------------------------------------- tipografia
_fontes = {}


def fonte(tam):
    tam = int(round(tam))
    if tam not in _fontes:
        _fontes[tam] = ImageFont.truetype(str(FONTE), tam)
    return _fontes[tam]


def largura(txt, f):
    return f.getlength(txt) + TRACK * f.size * max(0, len(txt) - 1)


def sprite(txt, f, cor=(255, 255, 255)):
    """Uma palavra (ou expressão) com tracking apertado e sombra escura bem suave. Devolve (RGBA, dx, dy) — dx/dy = onde fica
    a origem do texto dentro da imagem (a sombra pede margem)."""
    m = round(f.size * 0.35)
    asc, desc = f.getmetrics()
    iw, ih = round(largura(txt, f)) + 2 * m, asc + desc + 2 * m
    txt_l = Image.new("L", (iw, ih), 0)
    d = ImageDraw.Draw(txt_l)
    for k, ch in enumerate(txt):
        d.text((m + f.getlength(txt[:k]) + TRACK * f.size * k, m), ch, font=f, fill=255)
    sombra = txt_l.filter(ImageFilter.GaussianBlur(f.size * 0.16)).point(lambda v: int(v * 0.75))
    img = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
    img.putalpha(sombra)
    cor_img = Image.new("RGBA", (iw, ih), cor + (255,))
    cor_img.putalpha(txt_l)
    img.alpha_composite(cor_img)
    return img, m, m


def altura_maiuscula(tam):
    b = fonte(tam).getbbox("H")
    return b[3] - b[1]


def topo_maiuscula(tam):
    """Distância da origem do texto (topo do ascendente) até o topo da maiúscula."""
    return fonte(tam).getbbox("H")[1]


def quebra(palavras, f, max_w, max_n):
    """Linhas de até max_n palavras que cabem em max_w."""
    linhas, cur = [], []
    for p in palavras:
        if cur and (len(cur) >= max_n or largura(" ".join(x["t"] for x in cur + [p]), f) > max_w):
            linhas.append(cur)
            cur = []
        cur.append(p)
    if cur:
        linhas.append(cur)
    return linhas


def montar_bloco(ws, idx, grande, medio, alinhar, max_w):
    """Layout fixo de um bloco: lista de (palavra, tempo, tamanho, x, y_base) em coordenadas da faixa, posição relativa."""
    ps = [{"t": limpa(ws[k]["w"]), "i": ws[k]["i"], "k": k} for k in idx]
    ps = [p for p in ps if p["t"]]
    if not ps:
        return []
    g0 = g1 = None
    if grande:
        alvo = [norm(x) for x in grande.split() if norm(x)]
        for a in range(len(ps)):
            if [norm(p["t"]) for p in ps[a:a + len(alvo)]] == alvo:
                g0, g1 = a, a + len(alvo)
                break
        if g0 is not None and g0 > 0 and g1 - g0 == 1 and norm(ps[g0 - 1]["t"]) in {"a", "o", "as", "os"} and g0 - 1 > 0:
            g0 -= 1   # "A OBRIGAÇÃO" fica junto na linha grande
    linhas = []   # (tamanho, [palavras])
    if g0 is not None:
        for ln in quebra(ps[:g0], fonte(PEQ), max_w, 4):
            linhas.append((PEQ, ln))
        tam = GRA
        txt_g = " ".join(p["t"] for p in ps[g0:g1])
        while tam > PEQ and largura(txt_g, fonte(tam)) > max_w:
            tam -= 4
        linhas.append((tam, ps[g0:g1]))
        for ln in quebra(ps[g1:], fonte(PEQ), max_w, 4):
            linhas.append((PEQ, ln))
    else:
        tam = MED if medio else PEQ
        for ln in quebra(ps, fonte(tam), max_w, 3 if medio else 4):
            linhas.append((tam, ln))
    out, y = [], 0.0
    for n, (tam, ln) in enumerate(linhas):
        f = fonte(tam)
        if n:   # uma linha embaixo da outra: maiúscula da anterior + folga para o acento da próxima (Felipe, 05/11: encostavam)
            ant = linhas[n - 1][0]
            y += altura_maiuscula(ant) + 0.17 * ant + 0.22 * tam
        txt = " ".join(p["t"] for p in ln)
        lw = largura(txt, f)
        x0 = 0 if alinhar == "left" else -lw if alinhar == "right" else -lw / 2
        x = x0
        for p in ln:
            out.append({"t": p["t"], "i": p["i"], "tam": tam, "x": x, "y": y, "grande": tam > MED})
            x += largura(p["t"] + " ", f)
    return out


# ---------------------------------------------------------------- rosto e recorte
def detectar(video, dur):
    """Rostos a cada 2 quadros (YuNet, 640 px) e trocas de plano por histograma, em um passe só."""
    import cv2
    yn = cv2.FaceDetectorYN.create(str(AQUI / "face_detection_yunet.onnx"), "", (640, 360), 0.7, 0.3, 5000)
    proc = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-t", "{:.3f}".format(dur),
                             "-vf", "fps={},scale=640:360".format(FPS), "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)
    rostos, cortes, ant = [], [0], None
    n = 0
    while True:
        b = proc.stdout.read(640 * 360 * 3)
        if len(b) < 640 * 360 * 3:
            break
        im = np.frombuffer(b, np.uint8).reshape(360, 640, 3)
        hsv = cv2.cvtColor(cv2.resize(im, (160, 90)), cv2.COLOR_BGR2HSV)
        hist = cv2.normalize(cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256]), None).flatten()
        if ant is not None and cv2.compareHist(ant, hist, cv2.HISTCMP_BHATTACHARYYA) > 0.35 and n - cortes[-1] >= 3:
            cortes.append(n)
        ant = hist
        rs = []
        if n % 2 == 0:
            _, fs = yn.detect(im)
            for r in (fs if fs is not None else []):
                x, y, w, h = (float(v) for v in r[:4])
                if w >= 640 * 0.03:
                    rs.append(((x + w / 2) / 640, (y + h / 2) / 360, h / 360))
            rostos.append(rs)
        else:
            rostos.append(None)   # quadro não medido
        n += 1
    proc.wait()
    return rostos, cortes, n


def suaviza(x, sigma):
    if len(x) < 3 or sigma <= 0:
        return x
    r = int(3 * sigma)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    pad = np.pad(x, r, mode="edge")
    return np.convolve(pad, k, "valid")


def trilha_recorte(video, dur, emendas, falante=None):
    """Janela de recorte (x, y, largura, altura em px do vídeo 1920x1080) para cada quadro.
    Dentro de um trecho o recorte fica PARADO, no lugar mediano do rosto de quem fala; em cada emenda/troca de câmera muda
    de posição e escala (100% ↔ ~108%) para disfarçar o pulo, como na referência."""
    rostos, cortes, n = detectar(video, dur)
    limites = sorted(set(cortes + [int(round(e * FPS)) for e in emendas if 0 < e * FPS < n] + [n]))
    jan = np.zeros((n, 4))
    for s, (a, b) in enumerate(zip(limites[:-1], limites[1:])):
        if b <= a:
            continue
        alvo_cx = None
        if falante:   # quem fala no meio do segmento (falante.py), quando a medida existe
            t = (a + b) / 2 / FPS
            p = next((p for p in falante.get("planos") or [] if p["ini"] <= t < p["fim"]), None)
            if p and p.get("falante") is not None:
                alvo_cx = p["rostos"][p["falante"]]["cx"]
        amostras = [(i, r) for i in range(a, b) if rostos[i] for r in rostos[i]]
        if not amostras:
            ch = 1080.0
            jan[a:b] = [(1920 - ch * 4 / 3) / 2, 0, ch * 4 / 3, ch]
            continue
        if alvo_cx is None:   # o rosto maior do segmento
            alvo_cx = max(amostras, key=lambda x: x[1][2])[1][0]
        cx, cy, fh = np.full(b - a, np.nan), np.full(b - a, np.nan), np.full(b - a, np.nan)
        ult = alvo_cx
        for i in range(a, b):
            if rostos[i]:
                r = min(rostos[i], key=lambda r: abs(r[0] - ult))
                if abs(r[0] - ult) < 0.12:
                    cx[i - a], cy[i - a], fh[i - a] = r
                    ult = r[0]
        ok = ~np.isnan(cx)
        if not ok.any():
            ch = 1080.0
            jan[a:b] = [(1920 - ch * 4 / 3) / 2, 0, ch * 4 / 3, ch]
            continue
        idx = np.arange(b - a)
        cx = np.interp(idx, idx[ok], cx[ok])
        cy = np.interp(idx, idx[ok], cy[ok])
        cx[:], cy[:] = float(np.median(cx)), float(np.median(cy))   # câmera PARADA no trecho (Felipe, 05/11: sem acompanhar o rosto)
        # cabeça ≈ 1,45 × o rosto do detector; na referência a cabeça ocupa ~40% da altura da faixa
        z = clamp(1080.0 / (float(np.median(fh[ok])) * 1080 * 1.45 / 0.40), 1.0, ZOOM_MAX)
        if s % 2:   # trecho sim, trecho não: ~8% mais fechado (ou mais aberto, se já está no limite)
            z = z * 1.08 if z * 1.08 <= ZOOM_MAX else z / 1.08
        ch = 1080.0 / z
        cw = ch * 4 / 3
        pos = (0.5, 0.56, 0.46)[s % 3]   # o rosto passeia um pouco de lado entre os trechos
        x = np.clip(cx * 1920 - pos * cw, 0, 1920 - cw)
        y = np.clip(cy * 1080 - 0.36 * ch, 0, 1080 - ch)
        jan[a:b, 0], jan[a:b, 1], jan[a:b, 2], jan[a:b, 3] = x, y, cw, ch
    return jan, n


# ---------------------------------------------------------------- cor
def lut_pb():
    x = np.arange(256) / 255.0
    y = np.clip((x - 0.5) * 1.22 + 0.5 - 0.02, 0, 1)
    y = np.where(y < 0.07, y * 0.4, y)            # pretos profundos
    y = 1 - (1 - y) ** 1.08                        # realce sem estourar
    return (np.clip(y, 0, 0.97) * 255).astype(np.uint8)


# ---------------------------------------------------------------- trilha
def catalogo():
    try:
        return json.loads(CATALOGO.read_text(encoding="utf-8"))["trilhas"]
    except (OSError, ValueError, KeyError):
        return []


def arquivo_trilha(tr):
    """Caminho local da trilha (baixa e guarda no cache na primeira vez)."""
    p = Path(str(tr.get("arquivo") or ""))
    if p.is_absolute() and p.exists():
        return p
    CACHE.mkdir(parents=True, exist_ok=True)
    dest = CACHE / "{}.mp3".format(tr["id"])
    if not dest.exists() or dest.stat().st_size < 50000:
        req = urllib.request.Request(tr["url"], headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest.with_suffix(".tmp"), "wb") as f:
            f.write(r.read())
        dest.with_suffix(".tmp").rename(dest)
    return dest


def pico_da_trilha(arq):
    """Segundo da trilha em que ela mais cresce (entrada das cordas / do piano cheio): energia dos 3 s seguintes contra os 4 s
    anteriores, em janelas de 0,25 s."""
    sr = 8000
    a = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", str(arq), "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                                     capture_output=True).stdout, np.int16).astype(np.float32) / 32768
    p = sr // 4
    e = np.array([np.sqrt(np.mean(a[i:i + p] ** 2) + 1e-9) for i in range(0, len(a) - p, p)])
    db = 20 * np.log10(e + 1e-6)
    dur = len(db) / 4
    melhor, t_m = -99, 0.0
    for k in range(24, max(25, len(db) - 80)):   # entre 6 s e 20 s antes do fim
        v = db[k:k + 12].mean() - db[k - 16:k].mean()
        if v > melhor:
            melhor, t_m = v, k / 4
    return t_m, dur


def monta_audio(video, voz_dur, total, trilha, virada, saida, tmp):
    """Voz do corte + trilha 12 dB abaixo desde o quadro 0, pico na virada, some até o silêncio no fim. −14 LUFS, −1 dBTP."""
    ins = ["-i", str(video)]
    fc = ["[0:a]atrim=0:{:.3f},asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,"
          "loudnorm=I=-15:TP=-2:LRA=9,apad=whole_dur={:.3f}[voz]".format(voz_dur, total)]
    mix = "[voz]"
    if trilha:
        arq = arquivo_trilha(trilha)
        pico, dur_tr = pico_da_trilha(arq)
        ini = float(trilha.get("ini")) if trilha.get("ini") is not None else max(0.0, pico - float(virada or 0))
        if ini + total > dur_tr:   # trilha curta para o corte: começa mais cedo (o pico chega antes da virada)
            ini = max(0.0, dur_tr - total - 0.5)
        trilha["ini_usado"] = round(ini, 2)
        ins += (["-stream_loop", "-1"] if dur_tr < total else []) + ["-ss", "{:.3f}".format(ini), "-i", str(arq)]
        fc.append("[1:a]atrim=0:{:.3f},asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,loudnorm=I=-27:TP=-8:LRA=11,"
                  "afade=t=in:d=0.04,afade=t=out:st={:.3f}:d={:.3f}[mus]".format(total, total - CARD, CARD))
        fc.append("[voz][mus]amix=inputs=2:normalize=0:duration=first[mx]")
        mix = "[mx]"
    meio = tmp / "mix.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y"] + ins + ["-filter_complex", ";".join(fc), "-map", mix, "-t", "{:.3f}".format(total),
                    "-c:a", "pcm_s24le", str(meio)], check=True)
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", str(meio), "-af", "loudnorm=I=-14:TP=-1:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = json.loads(r.stderr[r.stderr.rfind("{"):r.stderr.rfind("}") + 1])
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(meio), "-af",
                    "loudnorm=I=-14:TP=-1.2:LRA=11:measured_I={input_i}:measured_TP={input_tp}:measured_LRA={input_lra}:"
                    "measured_thresh={input_thresh}:offset={target_offset}:linear=true,aresample=48000".format(**m),
                    "-c:a", "pcm_s16le", str(saida)], check=True)


# ---------------------------------------------------------------- card final
def fundo_card(ultimo, janela, lut):
    """Tela cheia 9:16 com o último quadro do corte (P&B), bem desfocado e escurecido, centrado em quem fala."""
    if ultimo is None:
        return Image.new("RGB", (W, H), (0, 0, 0))
    x, y, cw, ch = janela
    larg = 1080 * 9 / 16
    x0 = clamp(x + cw / 2 - larg / 2, 0, 1920 - larg)
    im = Image.fromarray(lut[ultimo]).crop((int(x0), 0, int(x0 + larg), 1080)).resize((W, H), Image.BILINEAR)
    im = im.filter(ImageFilter.GaussianBlur(34)).point(lambda v: int(v * 0.38))
    return Image.merge("RGB", (im, im, im))


def _ease_back(p, c=1.7):
    p -= 1
    return 1 + (c + 1) * p ** 3 + c * p ** 2


def texto_digitado(cv, linha, f, cx, y, u, t0, t1, cor=255):
    """Uma linha centrada em cx, letra a letra entre t0 e t1 (cada letra de cinza para a cor em 2 quadros)."""
    d = ImageDraw.Draw(cv)
    lw = largura(linha, f)
    x = cx - lw / 2
    for k, ch in enumerate(linha):
        t_l = t0 + (t1 - t0) * k / max(1, len(linha) - 1)
        if u < t_l or ch == " ":
            continue
        q = clamp((u - t_l) / (2 / FPS))
        v = int(110 + (cor - 110) * q)
        d.text((x + f.getlength(linha[:k]) + TRACK * f.size * k, y), ch, font=f, fill=(v, v, v, 255))


def desenha_card(fundo, u):
    cv = Image.blend(Image.new("RGB", (W, H), (0, 0, 0)), fundo, clamp(u / 0.3)).convert("RGBA")
    cx = W / 2
    # pergunta pequena e cinza, depois o convite digitado, depois o botão
    fk = fonte(38)
    if u > 0.05:
        k_img, dx, dy = sprite(CTA_KICKER, fk, (190, 190, 194))
        k_img.putalpha(k_img.getchannel("A").point(lambda v: int(v * clamp((u - 0.05) / 0.25))))
        cv.alpha_composite(k_img, dest=(round(cx - largura(CTA_KICKER, fk) / 2 - dx), round(H * 0.385 - dy)))
    fc = fonte(66)
    for li, linha in enumerate(CTA):
        texto_digitado(cv, linha, fc, cx, H * 0.425 + li * 76, u, 0.3 + 0.45 * li, 0.7 + 0.45 * li)
    # botão "+ SEGUIR": entra com um pulo, é "clicado" (afunda, onda) e vira "SEGUINDO"
    t_bot, t_clique = 1.3, 2.0
    if u >= t_bot:
        bw, bh = 400, 104
        p = clamp((u - t_bot) / 0.28)
        esc = _ease_back(p) if p < 1 else 1.0
        if t_clique <= u < t_clique + 0.16:
            esc *= 1 - 0.07 * np.sin(np.pi * (u - t_clique) / 0.16)
        seguindo = u >= t_clique + 0.08
        bot = Image.new("RGBA", (bw + 8, bh + 8), (0, 0, 0, 0))
        db = ImageDraw.Draw(bot)
        if seguindo:
            db.rounded_rectangle((4, 4, bw + 3, bh + 3), radius=bh // 2, fill=(0, 0, 0, 90), outline=(255, 255, 255, 255), width=4)
            ft = fonte(40)
            rot = "SEGUINDO"
            tw = largura(rot, ft) + 50
            x0 = (bw + 8 - tw) / 2
            db.line([(x0, bh / 2 + 4), (x0 + 12, bh / 2 + 16), (x0 + 32, bh / 2 - 8)], fill=(255, 255, 255, 255), width=6, joint="curve")
            for k, ch in enumerate(rot):
                db.text((x0 + 50 + ft.getlength(rot[:k]) + TRACK * 40 * k, bh / 2 + 4), ch, font=ft, fill=(255, 255, 255, 255), anchor="lm")
        else:
            db.rounded_rectangle((4, 4, bw + 3, bh + 3), radius=bh // 2, fill=(255, 255, 255, 255))
            ft = fonte(42)
            rot = "+ SEGUIR"
            tw = largura(rot, ft)
            x0 = (bw + 8 - tw) / 2
            for k, ch in enumerate(rot):
                db.text((x0 + ft.getlength(rot[:k]) + TRACK * 42 * k, bh / 2 + 4), ch, font=ft, fill=(12, 12, 12, 255), anchor="lm")
        if esc != 1.0:
            bot = bot.resize((max(1, round(bot.width * esc)), max(1, round(bot.height * esc))), Image.BILINEAR)
        by = H * 0.56
        cv.alpha_composite(bot, dest=(round(cx - bot.width / 2), round(by + (bh + 8) / 2 - bot.height / 2)))
        if t_clique <= u < t_clique + 0.5:   # onda do toque
            q = (u - t_clique) / 0.5
            onda = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            r = 30 + 150 * q
            yy = by + (bh + 8) / 2
            ImageDraw.Draw(onda).ellipse((cx + 60 - r, yy - r, cx + 60 + r, yy + r), outline=(255, 255, 255, int(170 * (1 - q))), width=4)
            cv.alpha_composite(onda)
    return cv.convert("RGB")


# ---------------------------------------------------------------- render
def render(plano_path):
    plano = json.loads(Path(plano_path).read_text(encoding="utf-8"))
    raiz = Path(plano_path).resolve().parent
    video = (raiz / plano["video"]).resolve()
    saida = (raiz / plano["saida"]).resolve()
    saida.parent.mkdir(parents=True, exist_ok=True)
    ws = palavras_do(raiz / plano["palavras"])
    if not ws:
        raise SystemExit("plano sem palavras")
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                       capture_output=True, text=True)
    dur_video = float(r.stdout.strip() or 0)
    voz_dur = float(plano.get("duracao") or dur_video)
    fim_img = min(ws[-1]["f"] + SEGURA, dur_video - 0.04) if dur_video > ws[-1]["f"] + 0.2 else ws[-1]["f"] + SEGURA
    total = fim_img + CARD
    n_img = int(round(fim_img * FPS))
    nframes = int(round(total * FPS))

    # legenda: um bloco por frase; palavra-chave grande e frases médias vêm do plano (o Claude escolhe pelo número da frase)
    escolhas = {int(b["n"]): b for b in plano.get("blocos") or [] if isinstance(b, dict) and str(b.get("n", "")).lstrip("-").isdigit()}
    fr = frases(ws)
    blocos = []
    for n, idx in enumerate(fr):
        e = escolhas.get(n, {})
        blocos.append({"de": ws[idx[0]]["i"] - 0.04, "idx": idx, "grande": (e.get("grande") or "").strip(), "medio": bool(e.get("medio"))})
    for k, b in enumerate(blocos):
        b["ate"] = blocos[k + 1]["de"] if k + 1 < len(blocos) else fim_img + 1 / FPS

    falante = None
    if plano.get("falante"):
        try:
            falante = json.loads((raiz / plano["falante"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            falante = None
    jan, n_lidos = trilha_recorte(video, fim_img, plano.get("emendas") or [], falante)
    lut = lut_pb()

    # alinhamento: alterna entre os blocos (o recorte já põe o rosto entre 46% e 56%; a legenda fica embaixo, no peito)
    ordem = ["center", "left", "right", "center", "right", "left"]
    for k, b in enumerate(blocos):
        b["alinhar"] = ordem[k % len(ordem)]
        max_w = FW * (0.80 if b["alinhar"] == "center" else 0.70)
        b["lay"] = montar_bloco(ws, b["idx"], b["grande"], b["medio"], b["alinhar"], max_w)
        if b["lay"]:
            alt = max(p["y"] + altura_maiuscula(p["tam"]) for p in b["lay"])
            # o bloco começa sempre na mesma altura (58% da faixa, no peito) e cresce para baixo, linha a linha;
            # só sobe se não couber até 93%
            b["y0"] = min(FH * 0.58, FH * 0.93 - alt)
            b["x0"] = {"left": FW * 0.13, "right": FW * 0.87, "center": FW / 2}[b["alinhar"]]

    _spr = {}

    def spr(t, tam):
        k = (t, tam)
        if k not in _spr:
            _spr[k] = sprite(t, fonte(tam))
        return _spr[k]

    card = {}

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        audio = tmp / "audio.wav"
        trilha = plano.get("trilha")
        monta_audio(video, voz_dur, total, trilha, plano.get("virada"), audio, tmp)
        if trilha:
            plano["trilha"] = trilha
            Path(plano_path).write_text(json.dumps(plano, ensure_ascii=False, indent=1), encoding="utf-8")
        dec = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-t", "{:.3f}".format(fim_img),
                                "-vf", "fps={}".format(FPS), "-f", "rawvideo", "-pix_fmt", "gray", "-"], stdout=subprocess.PIPE)
        enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "{}x{}".format(W, H), "-r", str(FPS),
                                "-i", "-", "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest", "-movflags", "+faststart", str(saida)],
                               stdin=subprocess.PIPE)
        sw, sh = 1920, 1080
        ultimo = None
        for n in range(nframes):
            T = n / FPS
            quadro = np.zeros((H, W, 3), np.uint8)
            if n < n_img:
                buf = dec.stdout.read(sw * sh)
                if len(buf) == sw * sh:
                    ultimo = np.frombuffer(buf, np.uint8).reshape(sh, sw)
                if ultimo is not None:
                    x, y, cw, ch = jan[min(n, len(jan) - 1)]
                    rec = Image.fromarray(lut[ultimo]).crop((int(x), int(y), int(x + cw), int(y + ch))).resize((FW, FH), Image.BILINEAR)
                    faixa = Image.merge("RGB", (rec, rec, rec)).convert("RGBA")
                else:
                    faixa = Image.new("RGBA", (FW, FH), (0, 0, 0, 255))
                # abertura em fenda: máscara que revela, de 50 px até a faixa cheia, linear (quadros 0-1 pretos)
                if n < 2 + ABRE:
                    if n < 2:
                        faixa = Image.new("RGBA", (FW, FH), (0, 0, 0, 255))
                    else:
                        h_ab = round(50 + (FH - 50) * (n - 2) / ABRE)
                        m = Image.new("L", (FW, FH), 0)
                        ImageDraw.Draw(m).rectangle((0, (FH - h_ab) // 2, FW, (FH + h_ab) // 2), fill=255)
                        preto = Image.new("RGBA", (FW, FH), (0, 0, 0, 255))
                        preto.paste(faixa, (0, 0), m)
                        faixa = preto
                b = next((b for b in blocos if b["de"] <= T < b["ate"] and b["lay"]), None)
                if b:
                    for p in b["lay"]:
                        u = T - (p["i"] - 0.03)
                        if u < 0:
                            continue
                        im, dx, dy = spr(p["t"], p["tam"])
                        px, py = b["x0"] + p["x"] - dx, b["y0"] + p["y"] - dy - topo_maiuscula(p["tam"])
                        if p["grande"]:   # desfoque que se resolve em ~4 quadros, com escala 105% → 100%
                            q = clamp(u / (4 / FPS))
                            if q < 1:
                                esc = 1.05 - 0.05 * q
                                im2 = im.resize((round(im.width * esc), round(im.height * esc)), Image.BILINEAR)
                                im2 = im2.filter(ImageFilter.GaussianBlur(9 * (1 - q)))
                                a = im2.getchannel("A").point(lambda v, q=q: int(v * clamp(0.35 + q)))
                                im2.putalpha(a)
                                px -= (im2.width - im.width) / 2
                                py -= (im2.height - im.height) / 2
                                im = im2
                        else:   # entra seca, com fade de 2 quadros
                            q = clamp(u / (2 / FPS) + 0.5)
                            if q < 1:
                                im = im.copy()
                                im.putalpha(im.getchannel("A").point(lambda v, q=q: int(v * q)))
                        faixa.alpha_composite(im, dest=(max(0, round(px)), max(0, round(py)))) if px >= 0 and py >= 0 else \
                            faixa.alpha_composite(im.crop((max(0, -round(px)), max(0, -round(py)), im.width, im.height)),
                                                  dest=(max(0, round(px)), max(0, round(py))))
                quadro[FY:FY + FH] = np.asarray(faixa.convert("RGB"))
            else:   # card: o último quadro desfocado atrás, o convite em máquina de escrever e o botão "Seguir" sendo clicado
                if "fundo" not in card:
                    card["fundo"] = fundo_card(ultimo, jan[min(n_img, len(jan)) - 1], lut)
                quadro = np.asarray(desenha_card(card["fundo"], T - fim_img))
            enc.stdin.write(quadro.tobytes())
            if n % 150 == 0:
                print("quadro {}/{}".format(n, nframes), flush=True)
        enc.stdin.close()
        enc.wait()
        dec.stdout.close()
        dec.wait()
    if enc.returncode:
        raise SystemExit("ffmpeg falhou ao gravar " + str(saida))
    print("ok", saida)


if __name__ == "__main__":
    render(sys.argv[1])
