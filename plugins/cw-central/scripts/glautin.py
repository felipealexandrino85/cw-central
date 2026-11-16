#!/usr/bin/env python3
"""Motor do estilo Glautin · Editorial (cortes do Glauton Santos, CEO da Cardápio Web, para o perfil pessoal dele).

Modelo "A · Editorial" escolhido pelo Felipe em 16/11/2026 (maquete: claude.ai/artifact/Q6dthSjRoKEj1TStdZ6RXt) + cartão de
número do "B". Versão 2 no mesmo dia (Felipe, lote 59: "a legenda ficou muito rápida e a edição muito simplificada, mais motion
animado"):
  - o vídeo é o corte do Titeu já enquadrado no Glauton, renderizado SEM legenda, headline, logo e CTA;
  - aproximação lenta da câmera no corte inteiro + zooms rápidos e suaves nas frases de impacto;
  - título em serifa no alto nos primeiros segundos (linha entre *asteriscos* em itálico dourado);
  - legenda em blocos de frase (até 2 linhas, letra maior) que entram inteiros; a palavra acende quando é dita (karaokê suave);
    a palavra-chave do bloco em itálico serifado com um fio dourado que se desenha embaixo;
  - uma peça de motion a cada 6–8 s: citação (vídeo desfoca, frase grande com aspas douradas), palavra (1–3 palavras grandes
    reveladas por máscara), número (cartão de vidro; conta só número grande ou percentual), lista (itens entrando um a um);
  - nome e cargo discretos uma vez.
Sem vinheta, sem CTA, sem logo, sem SFX.

Uso: python glautin.py plano.json
plano = {"video", "saida", "palavras": [{"w","t","e"}], "titulo": {"linhas", "de", "ate"}, "chaves": [...],
         "nome": {"texto", "cargo", "de"}, "cenas": [{"tipo": "citacao"|"palavra"|"numero"|"lista"|"zoom", "de", "ate", ...}]}
  citacao: "texto" · palavra: "texto" · numero: "valor", "prefixo", "sufixo", "texto" · lista: "itens": [[texto, s do corte]]
  ("numeros" da versão 1 ainda é aceito e vira cena de número.)
"""
import json
import math
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
AQUI = Path(__file__).resolve().parent
FONTES = Path(os.environ.get("GLAUTIN_FONTES", AQUI / "glautin" / "fonts"))
TINTA = (247, 244, 238)
OURO = (232, 201, 141)
MARGEM = 96
LEG_Y = 1150          # topo da legenda (o rosto fica entre 0,15 e 0,62 da altura no enquadramento do Titeu)
LEG_W = 920
TIT_Y = 236           # abaixo da barra do Instagram
NOME_Y = 1452
ZOOM = (1.0, 1.04)
APAGADA = 0.42        # opacidade da palavra do bloco que ainda não foi dita


def clamp(x, a=0.0, b=1.0):
    return a if x < a else b if x > b else x


def suave(x):
    x = clamp(x)
    return 1 - (1 - x) ** 3


def vai_e_volta(x):
    x = clamp(x)
    return 2 * x * x if x < .5 else 1 - (-2 * x + 2) ** 2 / 2


def norm(t):
    t = unicodedata.normalize("NFD", t.lower())
    return re.sub(r"[^a-z0-9%]", "", "".join(c for c in t if unicodedata.category(c) != "Mn"))


_F = {}


def fonte(nome, tam, peso=None):
    k = (nome, tam, peso)
    if k not in _F:
        f = ImageFont.truetype(str(FONTES / nome), tam)
        if peso:
            try:
                f.set_variation_by_axes([peso])
            except Exception:
                pass
        _F[k] = f
    return _F[k]


SANS = lambda tam, peso=600: fonte("Figtree[wght].ttf", tam, peso)
SERIF = lambda tam: fonte("InstrumentSerif-Regular.ttf", tam)
ITAL = lambda tam: fonte("InstrumentSerif-Italic.ttf", tam)


def sprite(txt, f, cor=TINTA, sombra=True):
    """Texto num RGBA justo, com uma sombra macia (nada de contorno grosso: o modelo é sóbrio)."""
    x0, y0, x1, y1 = f.getbbox(txt)
    pad = 28
    im = Image.new("RGBA", (max(1, x1 - x0) + 2 * pad, y1 + 2 * pad), (0, 0, 0, 0))
    if sombra:
        sm = Image.new("L", im.size, 0)
        ImageDraw.Draw(sm).text((pad - x0, pad + 3), txt, font=f, fill=160)
        im.putalpha(sm.filter(ImageFilter.GaussianBlur(9)))
    ImageDraw.Draw(im).text((pad - x0, pad), txt, font=f, fill=cor + (255,))
    return im, pad, x0


def com_alpha(im, a):
    if a >= 0.999:
        return im
    out = im.copy()
    out.putalpha(out.getchannel("A").point(lambda v: int(v * a)))
    return out


def quebra(txt, f, larg):
    linhas, cur = [], ""
    for w in txt.split():
        prova = (cur + " " + w).strip()
        if cur and f.getlength(prova) > larg:
            linhas.append(cur)
            cur = w
        else:
            cur = prova
    return linhas + ([cur] if cur else [])


# ------------------------------------------------------------------------------------------- legenda em blocos de frase
FIM = re.compile(r"[.?!…]['\")\]]*$")


def blocos(ws, max_chars=50, pausa=0.6, min_dur=1.4):
    """Frases (ponto ou pausa) em blocos de tamanho parecido, até 2 linhas; bloco curto demais junta com o vizinho."""
    frases, cur = [], []
    for k, w in enumerate(ws):
        cur.append(w)
        prox = ws[k + 1] if k + 1 < len(ws) else None
        if prox is None or FIM.search(w["w"]) or float(prox["t"]) - float(w["e"]) > pausa:
            frases.append(cur)
            cur = []
    out = []
    for fr in frases:
        acc = np.cumsum([len(w["w"]) + 1 for w in fr])
        n = max(1, math.ceil(acc[-1] / max_chars))
        cortes = sorted({int(np.argmin(np.abs(acc[:-1] - acc[-1] * j / n))) + 1 for j in range(1, n)}) if len(fr) > 1 else []
        for x, y in zip([0] + cortes, cortes + [len(fr)]):
            if y > x:
                out.append(fr[x:y])
    junto = []   # bloco de 1–2 palavras ou de menos de min_dur: entra no anterior (se couber) ou no seguinte
    for b in out:
        dur = float(b[-1]["e"]) - float(b[0]["t"])
        curto = len(b) <= 2 or dur < min_dur
        if curto and junto and sum(len(w["w"]) + 1 for w in junto[-1] + b) <= max_chars + 8 and float(b[0]["t"]) - float(junto[-1][-1]["e"]) < 1.0:
            junto[-1] = junto[-1] + b
        else:
            junto.append(b)
    final = []
    for b in junto:
        if final and (len(final[-1]) <= 2) and sum(len(w["w"]) + 1 for w in final[-1] + b) <= max_chars + 8:
            final[-1] = final[-1] + b
        else:
            final.append(b)
    res = []
    for k, b in enumerate(final):
        de = float(b[0]["t"]) - 0.12
        if res:   # um bloco de cada vez: o anterior sai quando este entra
            res[-1]["ate"] = min(res[-1]["ate"], de)
            de = max(de, res[-1]["ate"])
        res.append({"ws": b, "de": de, "ate": max(float(b[-1]["e"]) + 0.9, de + 0.5)})
    return res


def montar_bloco(bl, chaves):
    f, fk = SANS(64), ITAL(84)
    marcada = None
    for k, w in enumerate(bl["ws"]):
        if norm(w["w"]) and norm(w["w"]) in chaves:
            marcada = k
            break
    itens = []
    for k, w in enumerate(bl["ws"]):
        chave = k == marcada
        ff = fk if chave else f
        im, pad, _ = sprite(w["w"], ff, OURO if chave else TINTA)
        itens.append({"w": w, "im": im, "pad": pad, "larg": ff.getlength(w["w"]), "chave": chave, "asc": ff.getmetrics()[0]})
    esp = f.getlength(" ")
    linhas, cur, lw = [], [], 0
    for it in itens:
        add = it["larg"] + (esp if cur else 0)
        if cur and lw + add > LEG_W:
            linhas.append((cur, lw))
            cur, lw = [], 0
            add = it["larg"]
        cur.append(it)
        lw += add
    if cur:
        linhas.append((cur, lw))
    if len(linhas) > 2:
        resto = [it for ln, _ in linhas[1:] for it in ln]
        linhas = [linhas[0], (resto, sum(i["larg"] for i in resto) + esp * (len(resto) - 1))]
    base0 = LEG_Y + 70
    for n, (ln, lw) in enumerate(linhas):
        x = (W - lw) / 2
        for it in ln:
            it["x"], it["y"], it["base"] = x - it["pad"], base0 + n * 88 - it["asc"] - it["pad"], base0 + n * 88
            x += it["larg"] + esp
    bl["itens"] = itens
    return bl


def desenha_bloco(cv, bl, t):
    entra = suave((t - bl["de"]) / 0.35)
    sai = clamp((bl["ate"] - t) / 0.3)
    a0 = entra * sai
    if a0 <= 0:
        return
    dy = int(14 * (1 - entra))
    for it in bl["itens"]:
        dita = suave((t - float(it["w"]["t"]) + 0.04) / 0.18)
        a = a0 * (APAGADA + (1 - APAGADA) * dita)
        cv.alpha_composite(com_alpha(it["im"], a), (int(it["x"]), int(it["y"] + dy)))
        if it["chave"]:
            q = vai_e_volta((t - float(it["w"]["t"]) - 0.1) / 0.8)
            if q > 0:
                x0 = it["x"] + it["pad"]
                ImageDraw.Draw(cv).rectangle([x0, it["base"] + dy + 14, x0 + it["larg"] * q, it["base"] + dy + 17], fill=OURO + (int(255 * a0),))


# ------------------------------------------------------------------------------------------- título e nome
def montar_titulo(tit):
    linhas = []
    for ln in tit.get("linhas") or []:
        ital = ln.strip().startswith("*") and ln.strip().endswith("*")
        txt = ln.strip().strip("*").strip()
        if txt:
            im, pad, _ = sprite(txt, ITAL(92) if ital else SERIF(92), OURO if ital else TINTA)
            linhas.append((im, pad))
    return linhas


def revela(cv, im, x, y, p, sai=1.0):
    """Máscara de baixo para cima: o texto sobe de dentro de uma linha invisível (o movimento da maquete, mais marcado)."""
    if p <= 0 or sai <= 0:
        return
    w, h = im.size
    dy = int(h * 0.55 * (1 - p))
    corte = im.crop((0, 0, w, h - dy)) if dy > 0 else im
    cv.alpha_composite(com_alpha(corte, min(1.0, p * 1.4) * sai), (int(x), int(y + dy)))


def desenha_titulo(cv, linhas, tit, t):
    de, ate = float(tit.get("de", 0.2)), float(tit.get("ate", 3.6))
    sai = clamp((ate - t) / 0.5)
    for n, (im, pad) in enumerate(linhas):
        revela(cv, im, MARGEM - pad, TIT_Y + n * 100 - pad, suave((t - de - 0.22 * n) / 0.8), sai)
    q = vai_e_volta((t - de - 0.5) / 0.9)
    if q > 0 and sai > 0 and linhas:
        y = TIT_Y + len(linhas) * 100 + 24
        ImageDraw.Draw(cv).rectangle([MARGEM, y, MARGEM + 120 * q, y + 3], fill=OURO + (int(230 * sai),))


def desenha_nome(cv, nome, t, cache):
    de, ate = float(nome.get("de", 0)), float(nome.get("ate", 0))
    p, sai = suave((t - de) / 0.8), clamp((ate - t) / 0.6)
    if p <= 0 or sai <= 0:
        return
    if "n" not in cache:
        cache["n"] = sprite(nome.get("texto") or "Glauton Santos", SERIF(58), TINTA)
        cache["c"] = sprite((nome.get("cargo") or "CEO · Cardápio Web").upper(), SANS(26, 650), TINTA)
    im, pad, _ = cache["n"]
    revela(cv, im, MARGEM - pad, NOME_Y - pad, p, sai)
    q = vai_e_volta((t - de - 0.15) / 1.0)
    ImageDraw.Draw(cv).rectangle([MARGEM, NOME_Y + 74, MARGEM + 260 * q, NOME_Y + 76], fill=OURO + (int(220 * sai),))
    im, pad, _ = cache["c"]
    cv.alpha_composite(com_alpha(im, 0.88 * suave((t - de - 0.3) / 0.6) * sai), (MARGEM - pad, NOME_Y + 96 - pad))


# ------------------------------------------------------------------------------------------- peças de motion
CARD = (96, 1120, 984, 1440)   # na faixa da legenda (no alto, o cartão tapava o rosto no l59c01); a legenda sai enquanto ele está


def vidro(cv, quadro, caixa, a, raio=40):
    v = quadro.crop(caixa).filter(ImageFilter.GaussianBlur(24)).convert("RGBA")
    v.alpha_composite(Image.new("RGBA", v.size, (20, 16, 26, 70)))
    v.alpha_composite(Image.new("RGBA", v.size, (255, 255, 255, 26)))
    m = Image.new("L", v.size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, v.size[0] - 1, v.size[1] - 1], raio, fill=int(255 * a))
    v.putalpha(m)
    cv.alpha_composite(v, caixa[:2])
    ImageDraw.Draw(cv).rounded_rectangle(caixa, raio, outline=(255, 255, 255, int(70 * a)), width=2)


def numero_txt(n, v):
    val = float(n.get("valor") or 0)
    dec = 0 if float(val).is_integer() else 1
    s = "{:,.{}f}".format(v, dec).replace(",", "X").replace(".", ",").replace("X", ".")
    return "{}{}{}".format(n.get("prefixo") or "", s, n.get("sufixo") or "")


def desenha_numero(cv, quadro, n, t, cache):
    de, ate = float(n["de"]), float(n["ate"])
    p, sai = suave((t - de) / 0.7), clamp((ate - t) / 0.45)
    a = p * sai
    if a <= 0:
        return
    x0, y0, x1, y1 = CARD
    dy = int(30 * (1 - p))
    vidro(cv, quadro, (x0, y0 + dy, x1, y1 + dy), a)
    val = float(n.get("valor") or 0)
    conta = abs(val) >= 20 or "%" in (n.get("sufixo") or "") or not float(val).is_integer()
    if conta:
        c = vai_e_volta((t - de - 0.25) / 1.3)
        txt, esc = numero_txt(n, val * c if c < 1 else val), 1.0
    else:   # número pequeno (10 gestores): contar de 0 fica estranho; entra inteiro com uma leve ampliação
        txt, esc = numero_txt(n, val), 0.86 + 0.14 * suave((t - de - 0.1) / 0.6)
    im, pad, _ = sprite(txt, SANS(176, 640), TINTA, sombra=False)
    if esc < 0.999:
        im = im.resize((max(1, int(im.width * esc)), max(1, int(im.height * esc))), Image.BICUBIC)
    cv.alpha_composite(com_alpha(im, a), (x0 + 52 - int(pad * esc), y0 + dy + 30 - int(pad * esc)))
    k = ("t", id(n))
    if k not in cache:
        linhas = quebra(n.get("texto") or "", SANS(40, 500), x1 - x0 - 110)[:2]
        cache[k] = [sprite(l, SANS(40, 500), TINTA, sombra=False) for l in linhas]
    for j, (im, pad, _) in enumerate(cache[k]):
        cv.alpha_composite(com_alpha(im, 0.92 * a * suave((t - de - 0.35 - 0.12 * j) / 0.5)), (x0 + 56 - pad, y0 + dy + 236 + 46 * j - pad))
    q = vai_e_volta((t - de - 0.5) / 0.9)
    ImageDraw.Draw(cv).rectangle([x0 + 56, y0 + dy + 222, x0 + 56 + 90 * q, y0 + dy + 225], fill=OURO + (int(230 * a),))


def desenha_palavra(cv, c, t, cache):
    de, ate = float(c["de"]), float(c["ate"])
    p, sai = suave((t - de) / 0.6), clamp((ate - t) / 0.4)
    if p <= 0 or sai <= 0:
        return
    k = ("p", id(c))
    if k not in cache:
        linhas = quebra(c.get("texto") or "", ITAL(124), W - 2 * MARGEM)[:2]
        cache[k] = [sprite(l, ITAL(124), OURO if j == len(linhas) - 1 else TINTA) for j, l in enumerate(linhas)]
    for j, (im, pad, _) in enumerate(cache[k]):
        revela(cv, im, MARGEM - pad, TIT_Y + j * 124 - pad, suave((t - de - 0.15 * j) / 0.6), sai)
    q = vai_e_volta((t - de - 0.35) / 0.9)
    y = TIT_Y + len(cache[k]) * 124 + 26
    ImageDraw.Draw(cv).rectangle([MARGEM, y, MARGEM + 160 * q, y + 4], fill=OURO + (int(230 * sai),))


def desenha_lista(cv, quadro, c, t, cache):
    de, ate = float(c["de"]), float(c["ate"])
    p, sai = suave((t - de) / 0.6), clamp((ate - t) / 0.45)
    a = p * sai
    if a <= 0:
        return
    itens = c.get("itens") or []
    alto = 70 + 92 * len(itens)
    x0, y0, x1 = 96, 1130, 984
    dy = int(26 * (1 - p))
    vidro(cv, quadro, (x0, y0 + dy, x1, y0 + dy + alto), a)
    k = ("l", id(c))
    if k not in cache:
        cache[k] = [sprite(str(txt), SANS(46, 560), TINTA, sombra=False) for txt, _ in itens]
    d = ImageDraw.Draw(cv)
    for j, ((txt, em), (im, pad, _)) in enumerate(zip(itens, cache[k])):
        pj = suave((t - float(em) + 0.15) / 0.45)
        if pj <= 0:
            continue
        yy = y0 + dy + 46 + 92 * j
        d.rectangle([x0 + 50, yy + 28, x0 + 50 + 28 * pj, yy + 31], fill=OURO + (int(255 * a),))
        cv.alpha_composite(com_alpha(im, a * pj), (int(x0 + 100 - pad + 18 * (1 - pj)), yy - pad))


def desenha_citacao(cv, quadro, c, t, cache):
    de, ate = float(c["de"]), float(c["ate"])
    p, sai = suave((t - de) / 0.45), clamp((ate - t) / 0.4)
    a = p * sai
    if a <= 0:
        return
    if a > 0.02:
        borrado = quadro.filter(ImageFilter.GaussianBlur(16 * a)).convert("RGBA")
        borrado.alpha_composite(Image.new("RGBA", (W, H), (10, 8, 14, int(150 * a))))
        borrado.putalpha(int(255 * a))
        cv.alpha_composite(borrado)
    k = ("c", id(c))
    if k not in cache:
        linhas = quebra(c.get("texto") or "", SERIF(96), W - 2 * MARGEM - 20)[:5]
        cache[k] = [sprite(l, SERIF(96), TINTA) for l in linhas]
    ls = cache[k]
    y0 = int(H * 0.42 - len(ls) * 105 / 2)
    aspa, apad, _ = cache.setdefault("aspa", sprite("“", ITAL(260), OURO, sombra=False))
    revela(cv, aspa, MARGEM - apad - 6, y0 - 190 - apad, suave((t - de - 0.1) / 0.5), sai)
    for j, (im, pad, _) in enumerate(ls):
        revela(cv, im, MARGEM - pad, y0 + j * 105 - pad, suave((t - de - 0.25 - 0.12 * j) / 0.6), sai)


def zoom_em(t, cenas):
    z = 1.0
    for c in cenas:
        if c["tipo"] == "zoom" and c["de"] <= t < c["ate"] + 0.6:
            entra = vai_e_volta((t - c["de"]) / 0.5)
            sai = vai_e_volta((c["ate"] + 0.6 - t) / 0.6)
            z = max(z, 1 + 0.07 * min(entra, sai))
    return z


def degrade():
    """Escurece de leve o alto e a faixa da legenda (leitura sem caixa atrás do texto)."""
    a = np.zeros((H, 1), np.float32)
    y = np.arange(H, dtype=np.float32)
    a[:, 0] = np.clip((640 - y) / 640, 0, 1) ** 1.5 * 0.5 + np.clip((y - 1000) / 700, 0, 1) ** 1.3 * 0.6
    im = np.zeros((H, W, 4), np.uint8)
    im[..., 3] = (np.repeat(a, W, axis=1) * 255).astype(np.uint8)
    return Image.fromarray(im)


# ------------------------------------------------------------------------------------------- plano
DUR = {"citacao": (2.4, 4.5), "palavra": (1.4, 2.6), "numero": (2.6, 4.5), "lista": (3.0, 8.0), "zoom": (0.6, 2.0)}


def limpar_plano(p, dur):
    """Tempos dentro do vídeo, título curto no começo, peças sem se sobrepor (o zoom pode cair sobre a fala normal), nome uma vez."""
    tit = p.get("titulo") or {}
    tit["linhas"] = [l for l in (tit.get("linhas") or []) if str(l).strip()][:2]
    tit["de"] = clamp(float(tit.get("de", 0.2)), 0, 1.0)
    tit["ate"] = clamp(float(tit.get("ate", 3.6)), tit["de"] + 2.0, min(5.0, dur))
    p["titulo"] = tit
    cenas = list(p.get("cenas") or [])
    for n in p.get("numeros") or []:   # versão 1
        cenas.append(dict(n, tipo="numero"))
    limpas, fim_ant = [], tit["ate"] + 0.3
    zooms = []
    for c in sorted(cenas, key=lambda c: float(c.get("de", 0) or 0)):
        tipo = c.get("tipo")
        if tipo not in DUR:
            continue
        try:
            de, ate = float(c["de"]), float(c.get("ate") or float(c["de"]) + DUR[tipo][0])
        except (KeyError, TypeError, ValueError):
            continue
        lo, hi = DUR[tipo]
        ate = min(max(ate, de + lo), de + hi)
        if tipo == "zoom":
            if de >= tit["ate"] and ate <= dur - 0.6 and not (zooms and de < zooms[-1]["ate"] + 2.0):
                zooms.append(dict(c, de=round(de, 2), ate=round(ate, 2)))
            continue
        if tipo == "numero":
            try:
                float(c.get("valor"))
            except (TypeError, ValueError):
                continue
        if tipo == "lista":
            c["itens"] = [[str(x[0]), float(x[1])] for x in (c.get("itens") or []) if isinstance(x, (list, tuple)) and len(x) >= 2][:4]
            if len(c["itens"]) < 2:
                continue
            c["itens"] = [x for x in c["itens"] if x[1] <= de + hi - 1.2]   # a legenda some durante a lista: no máximo 8 s
            if len(c["itens"]) < 2:
                continue
            ate = min(max(ate, max(x[1] for x in c["itens"]) + 1.2), de + hi)
        if tipo in ("citacao", "palavra") and not str(c.get("texto") or "").strip():
            continue
        de = max(de, fim_ant + 0.6)
        ate = min(ate, dur - 0.3)
        if ate - de < lo * 0.8:
            continue
        limpas.append(dict(c, de=round(de, 2), ate=round(ate, 2)))
        fim_ant = ate
    p["cenas"] = limpas + zooms
    nome = p.get("nome") or {}
    if nome:
        livre = lambda x: not any(c["de"] - 0.3 < x + 3.4 and x < c["ate"] + 0.3 for c in limpas if c["tipo"] == "citacao")
        de = clamp(float(nome.get("de", tit["ate"] + 0.6)), tit["ate"] + 0.3, max(tit["ate"] + 0.3, dur - 4))
        while not livre(de) and de < dur - 4:
            de += 0.5
        nome["de"], nome["ate"] = round(de, 2), round(min(de + 3.4, dur - 0.3), 2)
    p["nome"] = nome
    return p


def render(plano_path):
    plano_path = Path(plano_path)
    p = json.loads(plano_path.read_text(encoding="utf-8"))
    base = plano_path.parent
    video = (base / p["video"]).resolve()
    saida = (base / p["saida"]).resolve()
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate:format=duration",
                        "-of", "json", str(video)], capture_output=True, text=True)
    info = json.loads(r.stdout)
    dur = float(info["format"]["duration"])
    num, den = info["streams"][0]["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    p = limpar_plano(p, dur)
    ws = [w for w in p.get("palavras") or [] if str(w.get("w", "")).strip()]
    chaves = {norm(c) for c in p.get("chaves") or [] if norm(c)}
    bls = [montar_bloco(b, chaves) for b in blocos(ws)]
    tit = montar_titulo(p["titulo"])
    cenas = p["cenas"]
    fundo = degrade()
    cache = {}
    dec = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-vf", "scale={}:{}".format(W, H), "-f", "rawvideo",
                            "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "{}x{}".format(W, H),
                            "-r", "{:.6f}".format(fps), "-i", "-", "-i", str(video), "-map", "0:v", "-map", "1:a?", "-c:v", "libx264",
                            "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "copy", "-shortest", str(saida)],
                           stdin=subprocess.PIPE)
    k, n_quadro = 0, W * H * 3
    try:
        while True:
            buf = dec.stdout.read(n_quadro)
            if len(buf) < n_quadro:
                break
            t = k / fps
            img = Image.frombuffer("RGB", (W, H), buf, "raw", "RGB", 0, 1)
            z = (ZOOM[0] + (ZOOM[1] - ZOOM[0]) * vai_e_volta(t / max(dur, 0.1))) * zoom_em(t, cenas)
            if z > 1.0005:   # aproximação, ancorada um pouco acima do meio (o rosto)
                cw, ch = W / z, H / z
                cx0, cy0 = (W - cw) / 2, (H - ch) * 0.38
                img = img.resize((W, H), Image.BICUBIC, box=(cx0, cy0, cx0 + cw, cy0 + ch))
            cv = img.convert("RGBA")
            cv.alpha_composite(fundo)
            cit = [c for c in cenas if c["tipo"] == "citacao" and c["de"] <= t < c["ate"]]
            cartao = any(c["tipo"] in ("numero", "lista") and c["de"] <= t < c["ate"] for c in cenas)
            for c in cit:
                desenha_citacao(cv, img, c, t, cache)
            for c in cenas:
                if c["de"] <= t < c["ate"]:
                    if c["tipo"] == "numero":
                        desenha_numero(cv, img, c, t, cache)
                    elif c["tipo"] == "palavra":
                        desenha_palavra(cv, c, t, cache)
                    elif c["tipo"] == "lista":
                        desenha_lista(cv, img, c, t, cache)
            desenha_titulo(cv, tit, p["titulo"], t)
            if p.get("nome"):
                desenha_nome(cv, p["nome"], t, cache)
            if not cit and not cartao:   # na citação a frase já está na tela; o cartão fica no lugar da legenda
                for bl in bls:
                    if bl["de"] <= t < bl["ate"]:
                        desenha_bloco(cv, bl, t)
            enc.stdin.write(cv.convert("RGB").tobytes())
            k += 1
    finally:
        enc.stdin.close()
        enc.wait()
        dec.stdout.close()
        dec.wait()
    if enc.returncode or not saida.exists():
        raise SystemExit("render do Glautin falhou")
    print(json.dumps({"ok": True, "quadros": k, "blocos": len(bls), "cenas": [c["tipo"] for c in cenas], "saida": str(saida)}))


def palavras_do_ass(ass, pal):
    """Texto revisado da legenda do Titeu (grafia certa: "Glauton") com o tempo de cada palavra medido no vídeo final."""
    linhas = []
    for l in Path(ass).read_text(encoding="utf-8", errors="ignore").splitlines():
        if not l.startswith("Dialogue:"):
            continue
        q = l.split(",", 9)
        if len(q) < 10 or q[3].strip() != "Cap" or "\\p1" in q[9]:
            continue
        txt = re.sub(r"\{[^}]*\}", "", q[9]).replace("\\N", " ").strip()
        h, m, s = q[1].split(":")
        t0 = int(h) * 3600 + int(m) * 60 + float(s)
        h, m, s = q[2].split(":")
        t1 = int(h) * 3600 + int(m) * 60 + float(s)
        if txt and t1 > t0:
            linhas.append((t0, t1, txt))
    linhas.sort()
    unicas = []   # a legenda do Titeu repete a mesma linha (sombra/texto, e o "segura" do fim da frase): uma vez só
    for x in linhas:
        if unicas and x[2] == unicas[-1][2] and x[0] < unicas[-1][1] + 0.3:
            unicas[-1] = (unicas[-1][0], max(unicas[-1][1], x[1]), x[2])
        else:
            unicas.append(x)
    linhas = unicas
    out = []
    for t0, t1, txt in linhas:
        ws = txt.split()
        perto = [w for w in pal if t0 - 0.25 <= float(w["t"]) <= t1 + 0.1]
        caixa_alta = sum(c.isalpha() for c in txt) >= 4 and txt == txt.upper()
        if caixa_alta:   # destaque amarelo do Titeu vem EM MAIÚSCULAS (l59c03): volta à grafia da fala
            if len(perto) == len(ws):
                ws = [x["w"].strip() if norm(x["w"]) == norm(w) else w.lower() for w, x in zip(ws, perto)]
            else:
                ws = [w.lower() for w in ws]
            if not out or FIM.search(out[-1]["w"]):
                ws[0] = ws[0][:1].upper() + ws[0][1:]
        if len(perto) == len(ws):
            out += [{"w": w, "t": float(x["t"]), "e": float(x["e"])} for w, x in zip(ws, perto)]
        else:
            d = (t1 - t0) / len(ws)
            out += [{"w": w, "t": round(t0 + i * d, 3), "e": round(t0 + (i + 1) * d, 3)} for i, w in enumerate(ws)]
    return out or pal


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    render(sys.argv[1])
