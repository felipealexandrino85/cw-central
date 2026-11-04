#!/usr/bin/env python3
"""Motor do estilo Dark-01 · Noir Motion (canal dark de cortes, Instagram e YouTube Shorts).

Recebe um plano (JSON) com o vídeo horizontal do corte, as palavras com tempo e as cenas de motion,
e desenha o vertical 1080x1920 quadro a quadro: título fixo, vídeo do episódio que encolhe/sobe/some,
legenda de 2-3 palavras, nome do convidado, gancho invertido com citação congelada, tipografia em tela
cheia, contador com gráfico, ícones a traço, linha do tempo, antes x depois, zoom de impacto, transição
em faixa vermelha e fechamento com a pergunta do corte. Sem imagem gerada por IA e sem logo da marca.

Uso: python dark01.py plano.json
O plano é escrito pelo Claude na esteira (cenas, gancho, destaques, zooms); limpar_plano() conserta tempos e tira o que não cabe.
"""
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 1080, 1920, 30
AQUI = Path(__file__).resolve().parent
FONTES = Path(os.environ.get("DARK01_FONTES", AQUI / "dark01" / "fonts"))
PRETO, BRANCO, VERMELHO, CINZA = (10, 10, 10), (242, 242, 242), (229, 35, 43), (138, 138, 142)
CQ = W / 100.0   # 1% da largura, a mesma unidade da prévia

PANE = {   # x, y, largura, opacidade, escala, borda
    "normal": (0, 538, 1080, 1.0, 1.0, 0.0),
    "split": (173, 480, 734, 1.0, 1.0, 1.0),
    "hidden": (0, 538, 1080, 0.0, 0.9, 0.0),
}
PANE_DA_CENA = {"tipografia": "hidden", "comparacao": "hidden", "icones": "split", "linha_tempo": "split",
                "contador": "normal", "citacao": "normal"}
SEM_LEGENDA = {"tipografia", "comparacao", "contador", "citacao"}
LEGENDA_ALTA = {"icones", "linha_tempo"}


# ---------------------------------------------------------------- utilidades
def clamp(x, a=0.0, b=1.0):
    return a if x < a else b if x > b else x


def ease_out(p):
    return 1 - (1 - p) ** 3


def ease_io(p):
    return 4 * p ** 3 if p < 0.5 else 1 - (-2 * p + 2) ** 3 / 2


def ease_back(p, c=1.7):
    p -= 1
    return 1 + (c + 1) * p ** 3 + c * p ** 2


_fontes = {}


def fonte(nome, tam):
    arq = {"anton": "Anton-Regular.ttf", "mont": "Montserrat-ExtraBold.ttf", "montb": "Montserrat-Bold.ttf",
           "mono": "JetBrainsMono-Regular.ttf"}[nome]
    k = (arq, int(tam))
    if k not in _fontes:
        _fontes[k] = ImageFont.truetype(str(FONTES / arq), int(tam))
    return _fontes[k]


def put(canvas, img, x, y, alpha=1.0, scale=1.0, anchor="mm"):
    """Cola uma camada RGBA no quadro, com escala e opacidade, recortando o que sair da tela."""
    if img is None or alpha <= 0.01:
        return
    if abs(scale - 1) > 0.004:
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.BILINEAR)
    if alpha < 0.995:
        img = img.copy()
        a = img.getchannel("A").point(lambda v: int(v * alpha))
        img.putalpha(a)
    w, h = img.size
    x0 = round(x - (w / 2 if anchor[0] == "m" else 0 if anchor[0] == "l" else w))
    y0 = round(y - (h / 2 if anchor[1] == "m" else 0 if anchor[1] == "t" else h))
    sx, sy = max(0, -x0), max(0, -y0)
    ex, ey = min(w, W - x0), min(h, H - y0)
    if ex <= sx or ey <= sy:
        return
    canvas.alpha_composite(img, dest=(x0 + sx, y0 + sy), source=(sx, sy, ex, ey))


def slam(u, dur=0.3):
    """Escala e opacidade de uma palavra que 'bate' na tela."""
    if u < 0:
        return 0.0, 1.7
    p = clamp(u / dur)
    return clamp(p * 2.2), 1.7 - 0.7 * ease_back(p)


# ---------------------------------------------------------------- texto
def tokens(texto):
    """'Eu ia fechar no *mês seguinte*' -> [(palavra, destaque)]"""
    out, dest = [], False
    for k, parte in enumerate(texto.split("*")):
        dest = k % 2 == 1
        for j, w in enumerate(parte.split()):
            m = re.match(r"^([.,!?;:…]+)(.*)$", w)
            if m and out and j == 0 and not parte[:1].isspace():   # pontuação colada na palavra anterior ("*16%*.")
                out.append((m.group(1), False, True))
                if m.group(2):
                    out.append((m.group(2), dest, False))
            else:
                out.append((w, dest, False))
    return out


def rich(texto, fonte_, max_w, cor=BRANCO, acento=VERMELHO, modo="cor", alinhar="center", entrelinha=1.04,
         sombra=False, caixa_alta=True):
    """Texto com palavras destacadas, quebrado em linhas. Devolve (camada RGBA, retângulos dos destaques).
    modo 'cor': destaque em vermelho; modo 'barra': texto branco, retângulos para a barra vermelha atrás."""
    toks = [((w.upper() if caixa_alta else w), d, c) for w, d, c in tokens(texto)]
    esp = fonte_.getlength(" ")
    junta = lambda ln: "".join((" " if k and not c else "") + w for k, (w, _, c) in enumerate(ln))
    linhas, atual = [], []
    for w, d, c in toks:
        if atual and not c and fonte_.getlength(junta(atual + [(w, d, c)])) > max_w:
            linhas.append(atual)
            atual = []
        atual.append((w, d, c))
    if atual:
        linhas.append(atual)
    asc, desc = fonte_.getmetrics()
    lh = round(fonte_.size * entrelinha)
    pad = round(fonte_.size * 0.25)
    larg = [fonte_.getlength(junta(ln)) for ln in linhas]
    iw = round(max(larg) + 2 * pad)
    ih = round(lh * (len(linhas) - 1) + asc + desc + 2 * pad)
    img = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    rects = []
    for k, ln in enumerate(linhas):
        x = pad + ((iw - 2 * pad - larg[k]) / 2 if alinhar == "center" else (iw - 2 * pad - larg[k]) if alinhar == "right" else 0)
        y = pad + k * lh
        for j, (w, d, c) in enumerate(ln):
            if c:
                x -= esp
            lw = fonte_.getlength(w)
            if d and modo == "barra":
                rects.append((x - 0.12 * fonte_.size, y + 0.06 * fonte_.size, x + lw + 0.12 * fonte_.size, y + asc + desc * 0.15))
            dr.text((x, y), w, font=fonte_, fill=(acento if d and modo == "cor" else cor) + (255,))
            x += lw + esp
    if sombra:
        a = img.getchannel("A").filter(ImageFilter.GaussianBlur(fonte_.size * 0.12))
        sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
        sh.putalpha(a.point(lambda v: int(v * 0.85)))
        sh.alpha_composite(img)
        img = sh
    return img, rects


_cache = {}


def cached(key, fn):
    if key not in _cache:
        _cache[key] = fn()
    return _cache[key]


# ---------------------------------------------------------------- ícones a traço (grade 24x24)
def _circ(cx, cy, r, a0=0, a1=360, n=48):
    return [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))) for a in np.linspace(a0, a1, n)]


def _quad(p0, c, p1, n=24):
    return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * c[0] + t * t * p1[0], (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * c[1] + t * t * p1[1])
            for t in np.linspace(0, 1, n)]


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


ICONES = {   # cada traço: (pontos, vermelho?)
    "moto": [(_circ(6, 17, 3), 0), (_circ(18, 17, 3), 0), ([(6, 17), (9.5, 11), (15, 11), (18, 17)], 0), ([(9, 17), (15, 17)], 0),
             ([(15, 11), (13.5, 7), (11, 7)], 0), (_rect(2.5, 8.5, 7.5, 12.5), 1)],
    "celular": [(_rect(7, 2.5, 17, 21.5), 0), ([(11, 18.5), (13, 18.5)], 0), ([(9.5, 7), (14.5, 7)], 1), ([(9.5, 10), (14.5, 10)], 1), ([(9.5, 13), (12.5, 13)], 1)],
    "pizza": [(_quad((4, 6), (12, 1.5), (20, 6)) + [(12, 21.5), (4, 6)], 0), (_quad((5.5, 8.5), (12, 5), (18.5, 8.5)), 0),
              (_circ(10, 10.5, 1.3), 1), (_circ(14, 11, 1.3), 1), (_circ(12, 15, 1.3), 1)],
    "dinheiro": [(_circ(12, 12, 9.5), 0), ([(15, 8.6), (13.6, 7.6), (10.6, 7.6), (9, 9), (9.4, 10.8), (14.6, 12.8), (15.2, 14.8), (13.6, 16.4), (10.4, 16.4), (8.9, 15.4)], 1),
                 ([(12, 5.5), (12, 7.6)], 1), ([(12, 16.4), (12, 18.5)], 1)],
    "loja": [([(3, 9), (5, 4), (19, 4), (21, 9), (3, 9)], 0), ([(4.5, 9), (4.5, 20), (19.5, 20), (19.5, 9)], 0), ([(10, 20), (10, 14), (14, 14), (14, 20)], 1)],
    "pessoas": [(_circ(9, 8, 3.2), 0), (_quad((3, 20), (9, 9), (15, 20)), 0), (_circ(17, 9, 2.5), 1), (_quad((14.5, 13.5), (19.5, 12), (21.5, 19)), 1)],
    "relogio": [(_circ(12, 12, 9), 0), ([(12, 6.5), (12, 12), (15.5, 14.5)], 1)],
    "repetir": [(_circ(12, 12, 8, -60, 230), 0), ([(17.5, 3.5), (16, 5.1), (18.2, 6.6)], 1), (_circ(12, 12, 2.2), 1)],
    "estrela": [([(12, 2.5), (14.6, 8.8), (21.3, 9.3), (16.2, 13.6), (17.8, 20.2), (12, 16.6), (6.2, 20.2), (7.8, 13.6), (2.7, 9.3), (9.4, 8.8), (12, 2.5)], 0)],
    "grafico": [([(3, 3), (3, 21), (21, 21)], 0), ([(6, 17), (10, 12), (13, 14), (19, 6)], 1), ([(15.5, 6), (19, 6), (19, 9.5)], 1)],
    "queda": [([(3, 3), (3, 21), (21, 21)], 0), ([(6, 7), (10, 12), (13, 10), (19, 18)], 1), ([(15.5, 18), (19, 18), (19, 14.5)], 1)],
    "chat": [([(3, 5), (21, 5), (21, 16), (10, 16), (5.5, 20), (6.5, 16), (3, 16), (3, 5)], 0), ([(7, 9), (17, 9)], 1), ([(7, 12), (13, 12)], 1)],
    "carrinho": [([(2.5, 4), (5.5, 4), (8, 15), (18.5, 15), (20.5, 7.5), (6.6, 7.5)], 0), (_circ(9, 19, 1.6), 1), (_circ(17, 19, 1.6), 1)],
    "porcentagem": [([(18, 5), (6, 19)], 0), (_circ(7.5, 7.5, 2.6), 1), (_circ(16.5, 16.5, 2.6), 1)],
}


def _parcial(pts, p):
    if p >= 1:
        return pts
    seg = [math.dist(pts[k], pts[k + 1]) for k in range(len(pts) - 1)]
    alvo, out = sum(seg) * p, [pts[0]]
    for k, s in enumerate(seg):
        if alvo <= s:
            f = alvo / s if s else 0
            out.append((pts[k][0] + (pts[k + 1][0] - pts[k][0]) * f, pts[k][1] + (pts[k + 1][1] - pts[k][1]) * f))
            return out
        alvo -= s
        out.append(pts[k + 1])
    return out


def icone(nome, tam, p):
    """Ícone desenhado até a fração p do traço (os traços andam juntos), com antisserrilhado 3x."""
    ss = 3
    T = tam * ss
    img = Image.new("RGBA", (T, T), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    k = T / 24.0
    lw = max(2, round(1.35 * k))
    for pts, red in ICONES.get(nome, ICONES["estrela"]):
        pp = [(x * k, y * k) for x, y in _parcial(pts, ease_io(clamp(p)))]
        if len(pp) < 2:
            continue
        cor = (VERMELHO if red else BRANCO) + (255,)
        dr.line(pp, fill=cor, width=lw, joint="curve")
        for x, y in (pp[0], pp[-1]):
            dr.ellipse((x - lw / 2, y - lw / 2, x + lw / 2, y + lw / 2), fill=cor)
    return img.resize((tam, tam), Image.LANCZOS)


# ---------------------------------------------------------------- legenda
_UN = {"zero": 0, "um": 1, "uma": 1, "dois": 2, "duas": 2, "três": 3, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6, "sete": 7, "oito": 8,
       "nove": 9, "dez": 10, "onze": 11, "doze": 12, "treze": 13, "quatorze": 14, "catorze": 14, "quinze": 15, "dezesseis": 16,
       "dezessete": 17, "dezoito": 18, "dezenove": 19, "vinte": 20, "trinta": 30, "quarenta": 40, "cinquenta": 50, "sessenta": 60,
       "setenta": 70, "oitenta": 80, "noventa": 90, "cem": 100, "cento": 100, "duzentos": 200, "trezentos": 300, "quatrocentos": 400,
       "quinhentos": 500, "seiscentos": 600, "setecentos": 700, "oitocentos": 800, "novecentos": 900}


def numeros(palavras):
    """Números falados por extenso viram dígitos ("trinta e quatro por cento" -> "34%", "vinte reais" -> "R$ 20", "dez dias" -> "10 dias").
    Só junta o que forma um número só (centena + dezena + unidade, ligados por "e"); vírgula separa ("três, quatro anos")."""
    lim = lambda w: re.sub(r"[^\wçãéêíóôú]", "", w["w"].lower())
    pont = lambda w: bool(re.search(r"[.,!?;:]$", w["w"]))
    out, k = [], 0
    while k < len(palavras):
        w0 = lim(palavras[k])
        if w0 not in _UN or (w0 == "cento" and k and lim(palavras[k - 1]) == "por"):
            out.append(palavras[k])
            k += 1
            continue
        total, ultimo, j = _UN[w0], _UN[w0], k + 1
        while (j + 1 < len(palavras) and not pont(palavras[j - 1]) and lim(palavras[j]) == "e" and lim(palavras[j + 1]) in _UN
               and _UN[lim(palavras[j + 1])] < ultimo and ultimo % (100 if _UN[lim(palavras[j + 1])] >= 10 else 10) == 0
               and lim(palavras[j + 1]) != "cento"):
            ultimo = _UN[lim(palavras[j + 1])]
            total += ultimo
            j += 2
        if w0 == "cem" and j == k + 1 and j + 1 < len(palavras) and lim(palavras[j]) == "e":
            pass
        prox = lim(palavras[j]) if j < len(palavras) and not pont(palavras[j - 1]) else ""
        fim = palavras[j - 1]
        if prox == "por" and j + 1 < len(palavras) and lim(palavras[j + 1]) == "cento":
            txt, fim, j = "{}%".format(total), palavras[j + 1], j + 2
        elif prox == "reais":
            txt, fim, j = "R$ {}".format(total), palavras[j], j + 1
        elif j > k + 1 or (total >= 2 and w0 not in ("um", "uma")):
            txt = str(total)
        else:
            out.append(palavras[k])
            k += 1
            continue
        p_ = re.search(r"[.,!?;:]+$", fim["w"])
        out.append({"w": txt + (p_.group(0) if p_ else ""), "i": palavras[k]["i"], "f": fim["f"]})
        k = j
    return out


FUNCAO = {"o", "a", "os", "as", "de", "do", "da", "dos", "das", "no", "na", "nos", "nas", "um", "uma", "e", "que", "pra", "pro",
          "com", "em", "por", "se", "ao", "à", "seu", "sua", "é", "mais", "muito", "eu", "você", "ele", "ela"}


def blocos_legenda(palavras, destaques):
    dest = {re.sub(r"[^\w%]", "", d.lower()) for d in destaques}
    out, cur = [], []
    for k, w in enumerate(palavras):
        cur.append(w)
        prox = palavras[k + 1] if k + 1 < len(palavras) else None
        txt = " ".join(x["w"] for x in cur)
        fecha = (len(cur) >= 3 or len(txt) >= 15 or re.search(r"[.,!?;:]$", w["w"])
                 or (prox and prox["i"] - w["f"] > 0.3) or prox is None)
        solta = re.sub(r"[^\w]", "", w["w"].lower()) in FUNCAO and not re.search(r"[.,!?;:]$", w["w"])
        if fecha and solta and prox and len(cur) < 4 and len(txt) < 20 and prox["i"] - w["f"] <= 0.3:
            fecha = False
        if fecha:
            out.append(cur)
            cur = []
    blocos = []
    for k, b in enumerate(out):
        fim = out[k + 1][0]["i"] if k + 1 < len(out) else b[-1]["f"] + 0.4
        if fim - b[-1]["f"] > 0.7:
            fim = b[-1]["f"] + 0.35
        partes = []
        for w in b:
            limpo = re.sub(r"[.,!?;:…\"“”]+$", "", w["w"]).strip()
            chave = re.sub(r"[^\w%]", "", limpo.lower())
            partes.append("*{}*".format(limpo) if chave in dest or re.search(r"\d", limpo) else limpo)
        blocos.append((b[0]["i"], fim, " ".join(partes)))
    return blocos


# ---------------------------------------------------------------- cenas
def cena_tipografia(cv, c, u):
    linhas = c["linhas"]
    y = 557 + (1190 - 557) / 2
    alturas = []
    imgs = []
    for k, ln in enumerate(linhas):
        grande = ln.startswith("*")
        apagada = ln.startswith("~")
        txt = ln.strip("*~")
        tam = 280 if grande else 162
        f = fonte("anton", tam)
        while f.getlength(txt.upper()) > 0.9 * W and tam > 60:
            tam -= 8
            f = fonte("anton", tam)
        im, _ = cached(("tipo", ln, tam), lambda: rich(txt, f, 2 * W, cor=CINZA if apagada else BRANCO, entrelinha=0.9))
        imgs.append((im, grande, f))
        alturas.append(round(tam * 0.98))
    total = sum(alturas)
    drift = -16 * clamp(u / max(c["dur"], 0.1))
    yy = y - total / 2 + drift
    tempos = c.get("tempos") or [0.1 + 0.45 * k for k in range(len(imgs))]
    for k, (im, grande, f) in enumerate(imgs):
        d = tempos[k]
        a, s = slam(u - d)
        cy = yy + alturas[k] / 2
        put(cv, im, W / 2, cy, a, s)
        if grande and u > d + 0.35:
            lw = (im.width - 0.5 * f.size) * ease_out(clamp((u - d - 0.35) / 0.4))
            bar = Image.new("RGBA", (max(1, round(lw)), round(2 * CQ)), VERMELHO + (255,))
            put(cv, bar, W / 2 - (im.width - 0.5 * f.size) / 2, cy + alturas[k] * 0.47, 1, 1, "lm")
        yy += alturas[k]


def cena_contador(cv, c, u):
    if c.get("prefixo"):
        im, _ = cached(("pref", c["prefixo"]), lambda: rich(c["prefixo"], fonte("mont", 65), W))
        put(cv, im, W / 2, 640, clamp((u - 0.1) / 0.2))
    p = ease_out(clamp((u - 0.3) / 0.9))
    v = c["valor"] * p
    casas = c.get("casas", 0)
    num = "{:,.{}f}".format(v, casas).replace(",", "X").replace(".", ",").replace("X", ".")
    txt = num + c.get("sufixo", "")
    f = fonte("anton", 230 if len(txt) <= 6 else 190)
    im = Image.new("RGBA", (W, 300), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((W / 2, 150), txt, font=f, fill=VERMELHO + (255,), anchor="mm")
    put(cv, im, W / 2, 790, clamp(u / 0.15), 1.0 + 0.06 * (1 - clamp((u - 1.2) / 0.15)) * (u > 1.2))
    if c.get("sub"):
        im, _ = cached(("sub", c["sub"]), lambda: rich(c["sub"], fonte("montb", 44), 0.86 * W))
        put(cv, im, W / 2, 965, clamp((u - 1.2) / 0.3))
    g = c.get("grafico")
    if g:
        vals, rot = g["valores"], g.get("rotulos", [""] * len(g["valores"]))
        x0, x1, base, alt = 151, 929, 1330, 165
        n = len(vals)
        gap = 43
        bw = min(170, (x1 - x0 - gap * (n - 1)) / n)
        x0 = W / 2 - (bw * n + gap * (n - 1)) / 2
        mx = max(vals) or 1
        dr = ImageDraw.Draw(cv)
        dr.rectangle((x0 - 30, base, x0 + bw * n + gap * (n - 1) + 30, base + 4), fill=CINZA + (255,))
        for k, val in enumerate(vals):
            q = ease_out(clamp((u - 1.3 - 0.15 * k) / 0.45))
            h = alt * val / mx * q
            cor = VERMELHO if k == g.get("destaque", n - 1) else BRANCO
            if h > 1:
                dr.rectangle((x0 + k * (bw + gap), base - h, x0 + k * (bw + gap) + bw, base), fill=cor + (255,))
            if rot[k]:
                im, _ = cached(("rot", rot[k]), lambda: rich(rot[k], fonte("mono", 30), 400, cor=CINZA, caixa_alta=False))
                put(cv, im, x0 + k * (bw + gap) + bw / 2, base + 34, clamp((u - 1.3) / 0.3))
        if g.get("legenda"):
            im, _ = cached(("leg", g["legenda"]), lambda: rich(g["legenda"], fonte("mono", 30), W, cor=CINZA, caixa_alta=False))
            put(cv, im, W / 2, base + 72, clamp((u - 2.0) / 0.3))


def cena_icones(cv, c, u):
    itens = c["itens"][:3]
    n = len(itens)
    tam = 238
    cxs = [W / 2] if n == 1 else [W * (0.3 + 0.4 * k) for k in range(2)] if n == 2 else [W * (0.19 + 0.31 * k) for k in range(3)]
    for k, item in enumerate(itens):
        nome, rotulo = item[0], item[1]
        d = item[2] if len(item) > 2 else 0.5 + 0.6 * k
        p = clamp((u - d) / 0.7)
        if p <= 0:
            continue
        im = cached(("ic", nome, tam), lambda: icone(nome, tam, 1)) if p >= 1 else icone(nome, tam, p)
        put(cv, im, cxs[k], 1035, 1)
        lab, _ = cached(("icl", rotulo, k == n - 1), lambda: rich(rotulo, fonte("anton", 65), 330, cor=VERMELHO if k == n - 1 else BRANCO))
        put(cv, lab, cxs[k], 1200, clamp((u - d - 0.4) / 0.3))


def cena_linha_tempo(cv, c, u):
    x0, x1, y = 86, 994, 1150
    dr = ImageDraw.Draw(cv)
    q = ease_io(clamp((u - 0.2) / 0.9))
    dr.rectangle((x0, y - 3, x0 + (x1 - x0) * q, y + 3), fill=CINZA + (255,))
    marcos = c["marcos"]
    n = len(marcos)
    for k, m in enumerate(marcos):
        cx = x0 + (x1 - x0) * (0.12 + 0.76 * (k / (n - 1) if n > 1 else 0.5))
        em = m.get("em", 0.9 + 1.0 * k)
        a = 0.18 + 0.82 * clamp((u - em) / 0.3) if u > 0.6 else 0
        red = m.get("destaque")
        cor = VERMELHO if red else BRANCO
        dot = cached(("dot", red), lambda: _bola(40, cor))
        put(cv, dot, cx, y, a)
        ano, _ = cached(("ano", m["topo"], red), lambda: rich(m["topo"], fonte("anton", 86), 340, cor=cor))
        put(cv, ano, cx, y - 30, a, 1, "mb")
        tx, _ = cached(("tx", m["texto"], red), lambda: rich(m["texto"], fonte("mont", 37), 300, cor=cor))
        put(cv, tx, cx, y + 30, a, 1, "mt")


def _bola(d, cor):
    im = Image.new("RGBA", (d * 3, d * 3), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    dr.ellipse((0, 0, d * 3 - 1, d * 3 - 1), fill=PRETO + (255,))
    dr.ellipse((d * 0.75, d * 0.75, d * 2.25, d * 2.25), fill=cor + (255,))
    return im.resize((round(d * 1.6), round(d * 1.6)), Image.LANCZOS)


def cena_comparacao(cv, c, u):
    top, bot = 557, 1380
    q = ease_io(clamp((u - 0.1) / 0.6))
    ImageDraw.Draw(cv).rectangle((W / 2 - 4, top, W / 2 + 4, top + (bot - top) * q), fill=VERMELHO + (255,))
    linhas = c["linhas"]
    n = len(linhas)
    lh = min(170, (bot - top - 200) / max(n, 1))
    yh = top + 90
    for k, (txt, cor, d) in enumerate(((c["esq"], CINZA, 0.3), (c["dir"], BRANCO, 0.45))):
        im, _ = cached(("vsh", txt), lambda: rich(txt, fonte("anton", 110 if len(txt) <= 8 else 80), 440, cor=cor))
        a, s = slam(u - d)
        put(cv, im, W / 2 + (-1 if k == 0 else 1) * 250, yh, a, s)
    for j, (e, dd) in enumerate(linhas):
        y = yh + 150 + j * lh
        t0 = 0.8 + 0.8 * j
        for k, txt in enumerate((e, dd)):
            tt = t0 + 0.4 * k
            p = ease_out(clamp((u - tt) / 0.35))
            if p <= 0:
                continue
            im, _ = cached(("vsl", txt, k), lambda: rich(txt, fonte("mont", 48), 430, cor=(169, 169, 173) if k == 0 else BRANCO,
                                                        alinhar="right" if k == 0 else "left"))
            dx = (1 - p) * 86 * (-1 if k == 0 else 1)
            if k == 0:
                put(cv, im, W / 2 - 40 + dx, y, p, 1, "rm")
            else:
                put(cv, im, W / 2 + 40 + dx, y, p, 1, "lm")


def cena_citacao(cv, c, u):
    aspas = cached(("aspas",), lambda: _aspas())
    a, s = slam(u - 0.15, 0.35)
    put(cv, aspas, 54, 432, a, s, "lt")
    im, _ = cached(("cit", c["texto"]), lambda: rich(c["texto"], fonte("anton", 97), 0.86 * W))
    a, s = slam(u - 0.45, 0.35)
    put(cv, im, W / 2, 1200, a, s, "mt")


def _aspas():
    im = Image.new("RGBA", (500, 520), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((0, -40), "“", font=fonte("anton", 520), fill=BRANCO + (255,))
    return im


CENAS = {"tipografia": cena_tipografia, "contador": cena_contador, "icones": cena_icones, "linha_tempo": cena_linha_tempo,
         "comparacao": cena_comparacao, "citacao": cena_citacao}


def blocos_tokens(texto, f, max_w, entrelinha=1.04):
    """Cada palavra como camada própria, já posicionada (centro relativo ao centro do bloco)."""
    toks = [(w.upper(), d, c) for w, d, c in tokens(texto)]
    esp = f.getlength(" ")
    linhas, atual = [], []
    for tk in toks:
        larg = sum(f.getlength(w) for w, _, _ in atual + [tk]) + esp * len(atual)
        if atual and not tk[2] and larg > max_w:
            linhas.append(atual)
            atual = []
        atual.append(tk)
    if atual:
        linhas.append(atual)
    lh = f.size * entrelinha
    asc, desc = f.getmetrics()
    out = []
    for k, ln in enumerate(linhas):
        larg = sum(f.getlength(w) for w, _, _ in ln) + esp * sum(1 for j, (_, _, c) in enumerate(ln) if j and not c)
        x = -larg / 2
        cy = (k - (len(linhas) - 1) / 2) * lh
        for j, (w, d, c) in enumerate(ln):
            if j and not c:
                x += esp
            lw = f.getlength(w)
            im = Image.new("RGBA", (round(lw + 20), asc + desc + 20), (0, 0, 0, 0))
            ImageDraw.Draw(im).text((10, 10), w, font=f, fill=(VERMELHO if d else BRANCO) + (255,))
            out.append((im, x + lw / 2, cy))
            x += lw
    return out


def envelope(wav):
    sr = 8000
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav), "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"], capture_output=True, check=True)
    a = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    passo = sr // 100
    n = len(a) // passo
    rms = np.sqrt((a[: n * passo].reshape(n, passo) ** 2).mean(axis=1))
    ref = np.percentile(rms[rms > 0.002], 92) if (rms > 0.002).any() else 1
    return np.clip(rms / ref, 0, 1.15)


def onda(cv, env, T, alpha=1.0, cy=1552, n=40):
    """Onda de áudio que pulsa do centro para fora, com a voz."""
    if alpha <= 0.02:
        return
    dr = ImageDraw.Draw(cv)
    bw, gap = 10, 8
    x0 = W / 2 - (n * bw + (n - 1) * gap) / 2
    for k in range(n):
        d = abs(k - (n - 1) / 2)
        idx = int((T - d * 0.03) * 100)
        v = env[idx] if 0 <= idx < len(env) else 0
        h = max(3, 34 * v * (1 - d / n * 0.55))
        cor = VERMELHO if d < 4 else (200, 200, 204)
        a = round(255 * alpha * (1 if d < 4 else 0.75))
        x = x0 + k * (bw + gap)
        dr.rectangle((x, cy - h, x + bw, cy + h), fill=cor + (a,))


def barra_baixo(cv, t, dur, parte, alpha=1.0):
    if alpha <= 0.02:
        return
    dr = ImageDraw.Draw(cv)
    x0, x1, y = 86, 994, 1660
    a = round(255 * alpha)
    dr.rectangle((x0, y, x1, y + 3), fill=(58, 58, 62, a))
    dr.rectangle((x0, y, x0 + (x1 - x0) * clamp(t / dur), y + 3), fill=VERMELHO + (a,))
    f = fonte("mono", 26)
    rotulo = parte.upper()
    while f.getlength(rotulo) > (x1 - x0) - 110 and len(rotulo) > 8:
        rotulo = rotulo[:-2].rstrip() + "…" if not rotulo.endswith("…") else rotulo[:-2].rstrip() + "…"
    dr.text((x0, y - 14), rotulo, font=f, fill=CINZA + (a,), anchor="ls")
    resta = max(0, dur - t)
    dr.text((x1, y - 14), "{}:{:02d}".format(int(resta // 60), int(resta % 60)), font=f, fill=CINZA + (a,), anchor="rs")


# ---------------------------------------------------------------- vídeo de fundo
def duotone(img):
    a = np.asarray(img.convert("L"), dtype=np.float32) / 255.0
    a = np.clip((a - 0.5) * 1.6 + 0.55, 0, 1)
    r = np.clip(a * 1.35, 0, 1) * 255
    g = np.clip(a * 1.35 - 0.75, 0, 1) * 200 + a * 35
    b = np.clip(a * 1.35 - 0.75, 0, 1) * 200 + a * 43
    return Image.fromarray(np.dstack([r, g, b]).astype(np.uint8), "RGB")


def fundo():
    bg = Image.new("RGBA", (W, H), PRETO + (255,))
    dr = ImageDraw.Draw(bg)
    passo = round(8 * CQ)
    for x in range(0, W, passo):
        dr.line((x, 0, x, H), fill=(22, 22, 23, 255))
    for y in range(0, H, passo):
        dr.line((0, y, W, y), fill=(22, 22, 23, 255))
    return bg


def quadro_em(video, t, larg=1080):
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", "{:.3f}".format(max(0, t)), "-i", str(video), "-frames:v", "1",
                        "-vf", "scale={}:-2,eq=saturation=0.85".format(larg), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True, check=True)
    h = len(r.stdout) // (larg * 3)
    return Image.frombytes("RGB", (larg, h), r.stdout)


# ---------------------------------------------------------------- linha do tempo do corte
def estado_pane(cenas, t):
    """(x, y, largura, opacidade, escala, borda) no instante t do vídeo, com transição de 0,45 s entre estados."""
    alvo, ant, ini = "normal", "normal", -10
    marcas = []
    for c in cenas:
        marcas.append((c["de"], PANE_DA_CENA.get(c["tipo"], "normal")))
        marcas.append((c["ate"], "normal"))
    marcas.sort(key=lambda m: m[0])
    for tm, est in marcas:
        if tm <= t:
            ant, alvo, ini = alvo, est, tm
    p = ease_io(clamp((t - ini) / 0.45))
    a, b = PANE[ant], PANE[alvo]
    return tuple(a[k] + (b[k] - a[k]) * p for k in range(6))


def zoom_em(zooms, t):
    for z in zooms:
        u = t - z
        if 0 <= u < 2.6:
            return 1 + 0.08 * (clamp(u / 0.13) if u < 2.3 else 1 - ease_io(clamp((u - 2.3) / 0.3)))
    return 1.0


def desenha_pane(cv, frame, est, zoom=1.0, dim=0.0, duo=False):
    x, y, w, alpha, esc, borda = est
    if alpha <= 0.01:
        return
    fr = frame
    if zoom > 1.001:
        cw, ch = fr.width / zoom, fr.height / zoom
        fr = fr.crop(((fr.width - cw) / 2, (fr.height - ch) / 2, (fr.width + cw) / 2, (fr.height + ch) / 2))
    ww = round(w * esc)
    hh = round(ww * 9 / 16)
    fr = fr.resize((ww, hh), Image.BILINEAR)
    if duo:
        fr = duotone(fr)
    if dim > 0:
        fr = Image.blend(fr, Image.new("RGB", fr.size, (0, 0, 0)), dim)
    xx = round(x + (w - ww) / 2)
    yy = round(y + (w * 9 / 16 - hh) / 2)
    if borda > 0.01:
        dr = ImageDraw.Draw(cv)
        b = 6
        dr.rectangle((xx - b, yy - b, xx + ww + b - 1, yy + hh + b - 1), fill=BRANCO + (round(255 * borda * alpha),))
    if alpha >= 0.995:
        cv.paste(fr, (xx, yy))
    else:
        cv.paste(fr, (xx, yy), Image.new("L", fr.size, round(255 * alpha)))


def faixa(cv, u):
    """Transição: faixa vermelha diagonal atravessando a tela (u de 0 a 1)."""
    if not 0 <= u <= 1:
        return
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(lay)
    cx = -1.3 * W + 3.6 * W * ease_io(u)
    inc = 0.36 * H
    for (a, b, cor) in ((-0.32, -0.02, VERMELHO), (-0.02, 0.03, BRANCO), (0.03, 0.42, VERMELHO)):
        x0, x1 = cx + a * W, cx + b * W
        dr.polygon([(x0 + inc, 0), (x1 + inc, 0), (x1 - inc, H), (x0 - inc, H)], fill=cor + (255,))
    cv.alpha_composite(lay)


# ---------------------------------------------------------------- áudio
def sfx(pasta):
    pasta.mkdir(parents=True, exist_ok=True)
    def gera(nome, filtro, dur):
        out = pasta / (nome + ".wav")
        if not out.exists():
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-t", str(dur), "-i", filtro, "-ar", "48000", "-ac", "2", str(out)], check=True)
        return out
    return {
        "whoosh": gera("whoosh", "anoisesrc=c=pink:a=0.6:d=0.42,highpass=f=300,lowpass=f=5000,afade=t=in:d=0.2:curve=qsin,afade=t=out:st=0.2:d=0.22:curve=qsin", 0.42),
        "impacto": gera("impacto", "sine=f=52:d=0.55,volume=1.8,afade=t=out:d=0.55:curve=exp", 0.55),
        "tick": gera("tick", "sine=f=1900:d=0.035,afade=t=out:d=0.035", 0.035),
        "rebobinar": gera("rebobinar", "aevalsrc=0.3*sin(2*PI*(700+3200*t)*t)+0.12*(random(0)-0.5):s=48000:d=0.5,highpass=f=300,tremolo=f=34:d=0.7,afade=t=in:d=0.04,afade=t=out:st=0.36:d=0.14", 0.5),
        "estatica": gera("estatica", "anoisesrc=c=white:a=0.35:d=0.4,highpass=f=1200,tremolo=f=28:d=0.9", 0.4),
    }


BANCO = Path(os.environ.get("DARK01_SFX_BANCO", AQUI / "dark01" / "sfx"))
TRILHAS = AQUI / "dark01" / "trilhas"


def som(nome, s):
    """Arquivo, ganho de normalização e pico de um som: do banco (ex.: 'whoosh-03') ou sintetizado ('tick', 'rebobinar')."""
    if nome in s:
        return s[nome], 0.0, 0.0
    idx = cached(("banco",), lambda: {x["id"]: x for x in json.loads((BANCO / "index.json").read_text(encoding="utf-8"))["sons"]})
    x = idx[nome]
    return BANCO / x["arquivo"], float(x.get("ganho_db", 0)), float(x.get("pico", 0))


def monta_audio(plano, video, dur_main, off, fim_dur, eventos, saida, tmp):
    s = sfx(tmp / "sfx")
    g = plano.get("gancho")
    partes = []
    filtros = []
    ins = ["-i", str(video)]
    if g:
        filtros.append("[0:a]atrim={:.3f}:{:.3f},asetpts=PTS-STARTPTS,afade=t=in:d=0.03,afade=t=out:st={:.3f}:d=0.06[g]".format(
            g["de"], g["ate"], g["ate"] - g["de"] - 0.06))
        filtros.append("anullsrc=r=48000:cl=stereo,atrim=0:{:.3f}[gap]".format(off - (g["ate"] - g["de"])))
        partes += ["[g]", "[gap]"]
    filtros.append("[0:a]atrim=0:{:.3f},asetpts=PTS-STARTPTS,afade=t=out:st={:.3f}:d=0.08[m]".format(dur_main, dur_main - 0.08))
    filtros.append("anullsrc=r=48000:cl=stereo,atrim=0:{:.3f}[fim]".format(fim_dur))
    partes += ["[m]", "[fim]"]
    filtros.append("{}concat=n={}:v=0:a=1,aformat=sample_rates=48000:channel_layouts=stereo,loudnorm=I=-14:TP=-1.5:LRA=11,asplit=2[voz][chave]".format("".join(partes), len(partes)))
    mix = ["[voz]"]
    total = off + dur_main + fim_dur
    n_in = 1
    trilha = plano.get("trilha")
    if trilha:   # trilha baixa, abaixa sozinha quando há voz, para no rebobinar e some no fechamento
        arq_trilha = Path(trilha).expanduser()
        if not arq_trilha.is_absolute() and not arq_trilha.exists():
            arq_trilha = TRILHAS / arq_trilha
        ins += ["-stream_loop", "-1", "-i", str(arq_trilha)]
        gd = (g["ate"] - g["de"]) if g else 0
        filtros.append("[{}:a]atrim=0:{:.3f},asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,volume={}dB,"
                       "volume=enable='between(t,{:.3f},{:.3f})':volume=0,afade=t=in:d=0.4,afade=t=out:st={:.3f}:d=1.8[tr0]".format(
                           n_in, total, plano.get("trilha_db", -13), gd, off, total - 1.9))
        filtros.append("[tr0][chave]sidechaincompress=threshold=0.04:ratio=5:attack=25:release=350[tr]")
        mix.append("[tr]")
        n_in += 1
    else:
        filtros.append("[chave]anullsink")
    for k, (t, nome, db) in enumerate(eventos):
        arq, ganho, pico = som(nome, s)
        t = max(0.0, t - pico)
        ins += ["-i", str(arq)]
        filtros.append("[{}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={:.1f}dB,adelay={}|{}[e{}]".format(
            n_in, db + ganho, round(t * 1000), round(t * 1000), k))
        mix.append("[e{}]".format(k))
        n_in += 1
    filtros.append("{}amix=inputs={}:normalize=0:duration=first,alimiter=limit=0.95[a]".format("".join(mix), len(mix)))
    subprocess.run(["ffmpeg", "-v", "error", "-y"] + ins + ["-filter_complex", ";".join(filtros), "-map", "[a]", "-c:a", "pcm_s16le", str(saida)], check=True)


# ---------------------------------------------------------------- plano
def limpar_plano(plano, dur):
    """O plano vem do Claude: tempos dentro do corte, cenas sem sobrepor (mín. 2,5 s, máx. 12 s), tipos e ícones conhecidos."""
    plano = dict(plano)
    cenas, fim_ant = [], 0.6
    for c in sorted([c for c in plano.get("cenas") or [] if isinstance(c, dict)], key=lambda c: float(c.get("de") or 0)):
        try:
            de, ate = float(c["de"]), float(c["ate"])
        except (KeyError, TypeError, ValueError):
            continue
        de, ate = max(de, fim_ant + 0.3), min(ate, de + 12.0, dur - 0.4)
        if c.get("tipo") not in CENAS or ate - de < 2.5:
            continue
        c = dict(c, de=round(de, 2), ate=round(ate, 2))
        if c["tipo"] == "icones":
            c["itens"] = [[i[0] if i[0] in ICONES else "estrela"] + list(i[1:]) for i in c.get("itens") or [] if isinstance(i, (list, tuple)) and len(i) >= 2][:3]
            if not c["itens"]:
                continue
        if c["tipo"] == "tipografia" and not c.get("linhas"):
            continue
        if c["tipo"] == "contador":
            try:
                c["valor"] = float(c["valor"])
            except (KeyError, TypeError, ValueError):
                continue
            g = c.get("grafico")
            if g and (not isinstance(g.get("valores"), list) or len(g["valores"]) < 2):
                c.pop("grafico")
        if c["tipo"] == "comparacao" and not (c.get("linhas") and c.get("esq") and c.get("dir")):
            continue
        if c["tipo"] == "linha_tempo" and len(c.get("marcos") or []) < 2:
            continue
        if c["tipo"] == "citacao" and not c.get("texto"):
            continue
        cenas.append(c)
        fim_ant = ate
    plano["cenas"] = cenas
    zs = []
    for z in sorted(float(z) for z in plano.get("zooms") or [] if isinstance(z, (int, float))):
        if 0.5 < z < dur - 3 and (not zs or z - zs[-1] > 6):
            zs.append(z)
    plano["zooms"] = zs
    g = plano.get("gancho")
    if g:
        try:
            de, ate = float(g["de"]), float(g["ate"])
            plano["gancho"] = {"de": max(0.0, de), "ate": min(dur, max(de + 2.0, min(ate, de + 7.0)))} if ate > de else None
        except (KeyError, TypeError, ValueError):
            plano["gancho"] = None
    return plano


# ---------------------------------------------------------------- render
def render(plano_path):
    plano = json.loads(Path(plano_path).read_text(encoding="utf-8"))
    raiz = Path(plano_path).resolve().parent
    video = (raiz / plano["video"]).resolve()
    saida = (raiz / plano["saida"]).resolve()
    saida.parent.mkdir(parents=True, exist_ok=True)
    pb = json.loads((raiz / plano["palavras"]).read_text(encoding="utf-8"))
    pb = pb.get("words", pb) if isinstance(pb, dict) else pb
    palavras_brutas = [{"w": w.get("w") or w.get("word") or "", "i": float(w.get("i", w.get("start", 0))), "f": float(w.get("f", w.get("end", 0)))}
                       for w in pb if (w.get("w") or w.get("word") or "").strip()]
    palavras = numeros(palavras_brutas)
    dur_main = float(plano.get("duracao") or subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)], capture_output=True, text=True).stdout)
    plano = limpar_plano(plano, dur_main)
    cenas = [dict(c, dur=c["ate"] - c["de"]) for c in plano.get("cenas", [])]
    zooms = plano.get("zooms", [])
    g = plano.get("gancho")
    gdur = (g["ate"] - g["de"]) if g else 0
    est_dur = 0.5 if g else 0
    off = gdur + est_dur
    fim_dur = 2.2
    total = off + dur_main + fim_dur
    nframes = int(round(total * FPS))
    legendas = [b for b in blocos_legenda(palavras, plano.get("destaques", [])) if b[0] < dur_main]

    bg = fundo()
    titulo, barras = rich(plano["titulo"], fonte("anton", 99), 0.86 * W, modo="barra", entrelinha=1.04)
    arroba, _ = rich(plano.get("arroba", "@seu.canal"), fonte("mono", 32), W, cor=CINZA, caixa_alta=False)
    nome = None
    if plano.get("convidado"):
        f = fonte("mono", 35)
        txt = plano["convidado"].upper()
        nome = Image.new("RGBA", (round(f.getlength(txt) + 70), 74), (10, 10, 10, 200))
        dn = ImageDraw.Draw(nome)
        dn.rectangle((0, 0, 8, 73), fill=VERMELHO + (255,))
        dn.text((38, 37), txt, font=f, fill=(214, 214, 216, 255), anchor="lm")
    gblocos = []
    if g:   # encaixa o gancho nas palavras: começa no início da primeira e termina no fim da última, sem cortar a fala
        dentro = [k for k, w in enumerate(palavras_brutas) if g["de"] - 0.15 <= w["i"] < g["ate"]]
        if dentro:
            k0, k1 = dentro[0], dentro[-1]
            ini = palavras_brutas[k0]["i"] - 0.12
            if k0:
                ini = max(ini, palavras_brutas[k0 - 1]["f"] + 0.02)
            fim = palavras_brutas[k1]["f"] + 0.25
            if k1 + 1 < len(palavras_brutas):
                fim = min(fim, palavras_brutas[k1 + 1]["i"] - 0.02)
            g = dict(g, de=round(max(0.0, ini), 3), ate=round(max(fim, palavras_brutas[k1]["f"] + 0.05), 3))
            plano["gancho"] = g
            gdur = g["ate"] - g["de"]
            off = gdur + est_dur
            total = off + dur_main + fim_dur
            nframes = int(round(total * FPS))
        gw = [dict(w, i=w["i"] - g["de"], f=w["f"] - g["de"]) for w in palavras if g["de"] - 0.05 <= w["i"] < g["ate"]]
        gblocos = blocos_legenda(gw, plano.get("destaques", []))
    if g:
        rebob = [quadro_em(video, x) for x in np.linspace(g["de"], 0.4, 14)]
    frame_fim = quadro_em(video, dur_main - 0.1)
    rng = np.random.default_rng(7)

    # sons: whoosh nas transições, impacto nas palavras que batem, tick no contador, estática no gancho
    eventos = []
    wh = iter(["whoosh-0{}".format(k) for k in (3, 5, 8, 2, 9, 4, 6, 7, 1)] * 4)
    if g:
        eventos += [(0.02, "impacto-02", -9), (gdur - 0.05, "glitch-02", -10), (gdur, "rebobinar", -12), (off - 0.02, "impacto-05", -12)]
    for c in cenas:
        c0 = off + c["de"]
        eventos.append((c0, next(wh), -12))
        if c["tipo"] == "tipografia":
            ts = c.get("tempos") or [0.1 + 0.45 * k for k in range(len(c["linhas"]))]
            eventos += [(c0 + d + 0.12, "impacto-0{}".format(3 + k % 3), -10 if k == len(ts) - 1 else -14) for k, d in enumerate(ts)]
        if c["tipo"] == "contador":
            eventos += [(c0 + 0.3, "riser-03", -16)] + [(c0 + 0.3 + 0.075 * k, "tick", -22) for k in range(12)]
            eventos += [(c0 + 1.2, "ding-02", -11), (c0 + 1.2, "impacto-01", -13)]
            if c.get("grafico"):
                eventos += [(c0 + 1.3 + 0.15 * k, "pop-0{}".format(1 + k % 5), -15) for k in range(len(c["grafico"]["valores"]))]
        if c["tipo"] == "icones":
            eventos += [(c0 + (it[2] if len(it) > 2 else 0.5 + 0.6 * k), "pop-0{}".format(1 + k % 5), -13) for k, it in enumerate(c["itens"][:3])]
        if c["tipo"] == "comparacao":
            eventos += [(c0 + 0.32, "impacto-06", -12)] + [(c0 + 0.8 + 0.8 * j + 0.4 * k, "pop-0{}".format(1 + (2 * j + k) % 5), -15)
                                                          for j in range(len(c["linhas"])) for k in range(2)]
        if c["tipo"] == "linha_tempo":
            eventos += [(c0 + m.get("em", 0.9 + k), "pop-0{}".format(1 + k % 5), -14) for k, m in enumerate(c["marcos"])]
    eventos += [(off + z, next(wh), -14) for z in zooms]
    eventos += [(off + dur_main, next(wh), -12), (off + dur_main + 0.35, "impacto-07", -8), (off + dur_main + 0.75, "impacto-04", -12)]
    eventos = [e for e in eventos if 0 <= e[0] < total]

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        audio = tmp / "audio.wav"
        monta_audio(plano, video, dur_main, off, fim_dur, eventos, audio, Path(os.environ.get("DARK01_SFX_CACHE", tmp)))
        env = envelope(audio)
        gdec = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", "{:.3f}".format(g["de"]), "-i", str(video), "-t", "{:.3f}".format(gdur),
                                 "-vf", "fps={},scale=1080:608,eq=saturation=0.85".format(FPS), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                stdout=subprocess.PIPE) if g else None
        gult = None
        fbytes = 1080 * 608 * 3
        dec = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(video), "-t", "{:.3f}".format(dur_main),
                                "-vf", "fps={},scale=1080:608,eq=saturation=0.85".format(FPS), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                               stdout=subprocess.PIPE)
        enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "{}x{}".format(W, H), "-r", str(FPS),
                                "-i", "-", "-i", str(audio), "-map", "0:v", "-map", "1:a",
                                "-vf", "noise=alls=6:allf=t,vignette=angle=PI/5",
                                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                                "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(saida)],
                               stdin=subprocess.PIPE)
        fbytes = 1080 * 608 * 3
        ultimo = None
        for n in range(nframes):
            T = n / FPS
            cv = bg.copy()
            mostra_titulo = True
            if T < gdur:   # gancho: o trecho mais forte rodando, câmera aproximando e a frase batendo palavra por palavra
                buf = gdec.stdout.read(fbytes)
                if len(buf) == fbytes:
                    gult = Image.frombytes("RGB", (1080, 608), buf)
                if gult is not None:
                    desenha_pane(cv, gult, PANE["normal"], zoom=1.0 + 0.12 * ease_io(clamp(T / gdur)))
                blk = next((b for b in gblocos if b[0] <= T < b[1]), None)
                if blk:
                    im, _ = cached(("gleg", blk[2]), lambda: rich(blk[2], fonte("anton", 112), 0.9 * W, sombra=True, entrelinha=1.0))
                    a, sc = slam(T - blk[0], 0.2)
                    put(cv, im, W / 2, 1255, a, sc)
                if T < 0.1:   # flash branco no primeiro quadro
                    cv.alpha_composite(Image.new("RGBA", (W, H), (255, 255, 255, round(230 * (1 - T / 0.1)))))
                onda(cv, env, T)
            elif T < off:   # rebobinar: as imagens voltam rápido até o começo da fala
                u = (T - gdur) / est_dur
                fr = rebob[min(len(rebob) - 1, int(u * len(rebob)))]
                desenha_pane(cv, fr, PANE["normal"], zoom=1.12 - 0.12 * u)
                lay = Image.new("RGBA", (W, 608), (0, 0, 0, 0))
                dl = ImageDraw.Draw(lay)
                for yy in range(int(rng.integers(0, 6)), 608, 6):
                    dl.line((0, yy, W, yy), fill=(0, 0, 0, 90))
                faixa_y = int(rng.integers(0, 560))
                dl.rectangle((0, faixa_y, W, faixa_y + 26), fill=(255, 255, 255, 60))
                cv.alpha_composite(lay, dest=(0, 538))
                im, _ = cached(("rew",), lambda: rich("<< minutos antes", fonte("mono", 46), W, caixa_alta=True))
                put(cv, im, 40, 538 + 50, 1 if int(T * 8) % 2 == 0 else 0.55, 1, "lm")
                onda(cv, env, T, 0.5)
            elif T < off + dur_main:
                t = T - off
                buf = dec.stdout.read(fbytes)
                if len(buf) == fbytes:
                    ultimo = Image.frombytes("RGB", (1080, 608), buf)
                ativa = next((c for c in cenas if c["de"] <= t < c["ate"]), None)
                est = estado_pane(cenas, t)
                dim = 0.62 * clamp((t - ativa["de"]) / 0.4) if ativa and ativa["tipo"] == "contador" else 0
                if ativa and ativa["tipo"] == "contador" and t > ativa["ate"] - 0.4:
                    dim = 0.62 * clamp((ativa["ate"] - t) / 0.4)
                duo = bool(ativa and ativa["tipo"] == "citacao")
                fr = ultimo
                if duo:
                    fr = cached(("cit_fr", ativa["de"]), lambda: quadro_em(video, ativa.get("quadro", ativa["de"])))
                if fr is not None:
                    desenha_pane(cv, fr, est, zoom_em(zooms, t), dim, duo)
                if ativa:
                    CENAS[ativa["tipo"]](cv, ativa, t - ativa["de"])
                # legenda
                if not (ativa and ativa["tipo"] in SEM_LEGENDA):
                    blk = next((b for b in legendas if b[0] <= t < b[1]), None)
                    if blk:
                        im, _ = cached(("leg", blk[2], bool(ativa and ativa["tipo"] in LEGENDA_ALTA)), lambda: rich(blk[2], fonte("mont", 74 if ativa and ativa["tipo"] in LEGENDA_ALTA else 80), 0.9 * W, sombra=True, entrelinha=1.06))
                        u = t - blk[0]
                        y = 1418 if ativa and ativa["tipo"] in LEGENDA_ALTA else 1245
                        put(cv, im, W / 2, y, clamp(0.3 + u / 0.16 * 0.7), 0.88 + 0.12 * ease_back(clamp(u / 0.16)))
                # nome do convidado, no canto de baixo do vídeo
                if nome is not None and est[3] > 0.5:
                    u = t - plano.get("nome_em", 3.0)
                    if 0 <= u < 3.75:
                        px, py, pw = est[0], est[1], est[2]
                        im = nome if u >= 0.45 else nome.crop((0, 0, max(1, round(nome.width * ease_out(u / 0.45))), nome.height))
                        put(cv, im, px + 24, py + pw * 9 / 16 - 24, clamp((3.75 - u) / 0.3) * est[3], 1, "lb")
                # onda de áudio e barra de progresso (somem quando o gráfico ou a comparação ocupam a parte de baixo)
                onda(cv, env, T)
                barra_baixo(cv, t, dur_main, plano.get("rotulo") or plano["titulo"].replace("*", ""))
            else:   # fechamento: último quadro congelado, escurecido, com a pergunta do corte
                u = T - off - dur_main
                fr = cached(("fim_duo",), lambda: duotone(frame_fim))
                desenha_pane(cv, fr, PANE["normal"], dim=0.55 * clamp(u / 0.3))
                # sem "parte 2 / no perfil" (Felipe, 04/11/2026): o fechamento é a pergunta do corte, grande no centro;
                # texto/sub só aparecem se o plano pedir de propósito
                fim = plano.get("fim", {})
                texto, sub, perg = fim.get("texto") or "", fim.get("sub") or "", fim.get("pergunta") or ""
                if texto:
                    im, _ = cached(("p2", texto), lambda: rich(texto, fonte("anton", 270), W, entrelinha=0.9))
                    a, s = slam(u - 0.35, 0.32)
                    put(cv, im, W / 2, 700, a, s)
                if sub:
                    im, _ = cached(("np", sub), lambda: rich(sub, fonte("anton", 130), W, cor=VERMELHO, entrelinha=0.9))
                    a, s = slam(u - 0.75, 0.32)
                    put(cv, im, W / 2, 900, a, s)
                if perg:
                    grande = not (texto or sub)
                    im, _ = cached(("perg", perg, grande), lambda: rich(perg, fonte("mont", 92 if grande else 65), 0.86 * W, sombra=True))
                    if grande:
                        a, s = slam(u - 0.3, 0.32)
                        put(cv, im, W / 2, 0.47 * H, a, s)
                    else:
                        put(cv, im, W / 2, 1255, clamp((u - 1.2) / 0.3))
            if mostra_titulo:
                put(cv, arroba, W / 2, 0.055 * H)
                pt = ease_out(clamp(T / 0.45))
                tl = titulo
                if pt < 1:
                    tl = titulo.crop((0, round(titulo.height * (1 - pt)), titulo.width, titulo.height))
                tx = (W - titulo.width) / 2
                ty = 0.105 * H + (1 - pt) * 43
                q = ease_io(clamp((T - 0.25) / 0.55))
                dr = ImageDraw.Draw(cv)
                for (x0, y0, x1, y1) in barras:
                    if q > 0:
                        dr.rectangle((tx + x0, ty + y0, tx + x0 + (x1 - x0) * q, ty + y1), fill=VERMELHO + (255,))
                put(cv, tl, tx, ty + titulo.height - tl.height, 1, 1, "lt")
            # transições: faixa vermelha em cada início de cena e no fechamento
            for tc in [off + c["de"] for c in cenas] + [off + dur_main]:
                faixa(cv, (T - (tc - 0.16)) / 0.32)
            enc.stdin.write(cv.convert("RGB").tobytes())
            if n % 150 == 0:
                print("quadro {}/{}".format(n, nframes), flush=True)
        enc.stdin.close()
        enc.wait()
        dec.stdout.close()
        dec.wait()
        if gdec:
            gdec.stdout.close()
            gdec.wait()
    if enc.returncode:
        raise SystemExit("ffmpeg falhou ao gravar " + str(saida))
    print("ok", saida)


if __name__ == "__main__":
    render(sys.argv[1])
