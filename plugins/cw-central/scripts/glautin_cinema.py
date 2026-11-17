#!/usr/bin/env python3
"""Motor do estilo Glautin · Cinema escuro (cortes do Glauton Santos para o perfil pessoal dele), 17/11/2026.

Escolhido pelo Felipe a partir de um Pin de referência ("edição dinâmica" de podcast) e aprovado na maquete
claude.ai/artifact/S75Pt5hWYeMUHuXT3CrnuZ, depois de achar o timeu01 ruim e o 3D com mascote "muito animado":
  - legenda de UMA palavra, pequena e branca;
  - frase de impacto: linha fina em cima + palavra forte embaixo, que aparece letra por letra saindo do desfoque
    (itálico serifado amarelo com brilho, ou caixa alta pesada branca), com aproximação lenta do vídeo;
  - tela escura (grafite arroxeado) só no ápice, no máximo 2 por corte; a volta ao vídeo entra com foco;
  - apoio nos números: barra de porcentagem enchendo e contador;
  - marca sem logo: amarelo #FFC400 da Cardápio Web, fonte Museo Sans Rounded (Nunito se ela faltar).
REGRAS do Felipe: legenda e texto NUNCA em cima do rosto, e a legenda FIXA (não sobe e desce acompanhando o rosto).
O rosto do Glauton (o MAIOR da tela; o rosto de uma foto de b-roll no gancho dividido é menor e não conta) é medido no vídeo
(YuNet); a linha do texto é uma só no corte inteiro: a base dele em quase todo o corte (percentil 95) + FOLGA, sem passar de
TETO. Assim, no gancho com b-roll embaixo, a legenda fica logo abaixo do Glauton e não em cima do b-roll.
O b-roll vem do corte do Titeu (vídeo com movimento, regra dos 12 s); sem rosto na tela o texto fica em SEM_ROSTO.

Uso: python glautin_cinema.py plano.json
plano = {"video", "saida", "palavras": [{"w","t","e"}], "palavras_medidas": [{"w","t","e"}] (opcional), "eventos": [evento],
         "gancho": {"ate": s, "emenda": 0.6} (opcional: gancho dividido com b-roll embaixo)}
Ajustes de 17/11/2026 (Felipe: "em alguns momentos a legenda demora, tem alguns problemas básicos"; medido no lote 66):
  - a legenda usava os tempos da legenda do Titeu, 0,14 a 0,47 s atrasados (até 1,5 s): agora o TEMPO vem da medição no vídeo
    (palavras_medidas) e da legenda do Titeu só a GRAFIA; palavra curta que piscaria (< 0,22 s) sai junto com a seguinte;
  - no gancho dividido, legenda e frase ficam no painel de cima (acima da emenda), não em cima do b-roll ("de mercado" amarelo
    sumia na foto amarela do restaurante);
  - linha fina maior e mais grossa; respiro de 2 s entre eventos, no máximo 1 a cada 7 s e nenhuma palavra forte repetida.
evento = {"de", "ate", "fundo": "video"|"escuro", "lead": "linha fina", "forte": [{"texto", "t"}] (2+ = trocam no lugar),
          "estilo": "serif"|"pesado", "cor": "amarelo"|"branco", "sub": {"texto", "t"}|null,
          "conta": {"de", "ate", "prefixo", "sufixo", "dur"}|null (o forte vira contador), "barra": 0–100|null}
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
AQUI = Path(__file__).resolve().parent
AMARELO = (255, 196, 0)
BRANCO = (255, 255, 255)
FOLGA, TETO, PISO, SEM_ROSTO = 0.07, 0.86, 0.52, 0.62
LARG = 950                  # largura máxima do texto (margem de 65 px de cada lado)
MAX_ESCURO = 2
RESPIRO = 2.0
NIVEIS = (0, 3, 7, 13, 22)  # desfoque das letras ao entrar (px), em degraus para caber no cache


def clamp(x, a=0.0, b=1.0):
    return a if x < a else b if x > b else x


def ease(x):
    return 1 - (1 - clamp(x)) ** 3


# ------------------------------------------------------------------------------------------- fontes
_ACHADAS = {}


def _achar(nome):
    """Museo/Nunito ficam no plugin cw-cortes (motion-vox/fontes); a serifa fica em scripts/glautin/fonts."""
    if nome in _ACHADAS:
        return _ACHADAS[nome]
    bases = [Path(os.environ["GLAUTIN_FONTES"])] if os.environ.get("GLAUTIN_FONTES") else []
    bases += [AQUI / "glautin" / "fonts", AQUI.parents[1] / "cw-cortes" / "skills" / "motion-vox" / "fontes",
              Path.home() / ".claude" / "plugins"]
    achada = None
    for b in bases:
        if (b / nome).exists():
            achada = b / nome
            break
        if b.exists() and b.name == "plugins":
            achada = next(iter(sorted(b.glob("**/" + nome), key=lambda p: -p.stat().st_mtime)), None)
            if achada:
                break
    _ACHADAS[nome] = achada
    return achada


_F = {}


def sans(tam, peso=900):
    k = ("s", tam, peso)
    if k not in _F:
        m = _achar("MuseoSansRounded-{}.otf".format(peso))
        if m:
            _F[k] = ImageFont.truetype(str(m), tam)
        else:
            f = ImageFont.truetype(str(_achar("Nunito.ttf") or _achar("Figtree[wght].ttf")), tam)
            try:
                f.set_variation_by_axes([peso])
            except Exception:
                pass
            _F[k] = f
    return _F[k]


def serif(tam):
    k = ("i", tam)
    if k not in _F:
        _F[k] = ImageFont.truetype(str(_achar("InstrumentSerif-Italic.ttf")), tam)
    return _F[k]


# ------------------------------------------------------------------------------------------- peças de texto
def cola(cv, im, x, y):
    """alpha_composite que aceita posição fora da tela (corta o que sobra)."""
    x, y = int(round(x)), int(round(y))
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(cv.width, x + im.width), min(cv.height, y + im.height)
    if x1 <= x0 or y1 <= y0:
        return
    cv.alpha_composite(im.crop((x0 - x, y0 - y, x1 - x, y1 - y)), (x0, y0))


def com_alpha(im, a):
    if a >= 0.999:
        return im
    out = im.copy()
    out.putalpha(out.getchannel("A").point(lambda v: int(v * a)))
    return out


class Palavra:
    """Texto montado letra a letra (cada letra com cache dos degraus de desfoque) + brilho ou sombra da palavra inteira."""

    def __init__(self, txt, f, cor, brilho=False, tracking=0):
        self.txt, self.f, self.cor = txt, f, cor
        self.pad = 30
        asc, desc = f.getmetrics()
        self.alt = asc + desc
        xs, x = [], 0.0
        for i, c in enumerate(txt):
            xs.append(f.getlength(txt[:i]) + i * tracking)
        self.larg = int(f.getlength(txt) + max(0, len(txt) - 1) * tracking)
        self.xs = xs
        self.letras = []
        for c in txt:
            if c == " ":
                self.letras.append(None)
                continue
            lw = int(f.getlength(c)) + 2 * self.pad + 8
            im = Image.new("RGBA", (lw, self.alt + 2 * self.pad), (0, 0, 0, 0))
            ImageDraw.Draw(im).text((self.pad, self.pad), c, font=f, fill=cor + (255,))
            self.letras.append({0: im})
        # brilho (amarelo) ou sombra macia (branco) da palavra inteira, por baixo das letras
        base = Image.new("L", (self.larg + 2 * self.pad + 60, self.alt + 2 * self.pad), 0)
        dr = ImageDraw.Draw(base)
        for c, x in zip(txt, xs):
            dr.text((self.pad + 30 + x, self.pad + (0 if brilho else 6)), c, font=f, fill=255)
        if brilho:
            g1 = base.filter(ImageFilter.GaussianBlur(16)).point(lambda v: int(v * .62))
            g2 = base.filter(ImageFilter.GaussianBlur(44)).point(lambda v: int(v * .4))
            a = Image.fromarray(np.maximum(np.asarray(g1), np.asarray(g2)))
            self.halo = Image.merge("RGBA", (*Image.new("RGB", base.size, (255, 170, 0)).split(), a))
        else:
            a = base.filter(ImageFilter.GaussianBlur(16)).point(lambda v: int(v * .42))
            self.halo = Image.merge("RGBA", (*Image.new("RGB", base.size, (0, 0, 0)).split(), a))

    def letra(self, i, nivel):
        c = self.letras[i]
        if nivel not in c:
            c[nivel] = c[0].filter(ImageFilter.GaussianBlur(nivel))
        return c[nivel]

    def desenha(self, cv, cx, y, t0, t, passo=.045, dur=.3, alfa=1.0):
        """Letra por letra: entra do desfoque, um pouco maior, e assenta. cx = centro, y = topo."""
        x0 = cx - self.larg / 2
        n = len(self.txt)
        ultima = ease((t - t0 - (n - 1) * passo) / dur)
        if ultima > 0.02:
            cola(cv, com_alpha(self.halo, ultima * alfa), x0 - self.pad - 30, y - self.pad)
        for i in range(n):
            if self.letras[i] is None:
                continue
            k = ease((t - t0 - i * passo) / dur)
            if k <= 0.01:
                continue
            nivel = min(NIVEIS, key=lambda v: abs(v - (1 - k) * NIVEIS[-1]))
            im = self.letra(i, nivel)
            s = 1.18 - .18 * k
            if s > 1.005:
                im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.BILINEAR)
            gx = x0 + self.xs[i] - self.pad - (im.width - self.letras[i][0].width) / 2
            gy = y - self.pad - (im.height - self.letras[i][0].height) / 2
            cola(cv, com_alpha(im, k * alfa), gx, gy)


def caber(txt, estilo, cor):
    """Escolhe o tamanho (e, se preciso, 2 linhas) para caber em LARG."""
    brilho = cor == AMARELO
    mk = (lambda s: serif(s)) if estilo == "serif" else (lambda s: sans(s, 900))
    if estilo != "serif":
        txt = txt.upper()
    tam = (165 if estilo == "serif" else 146) + (40 if len(txt) <= 5 else 0)   # número curto ("40%") ganha tamanho
    while tam >= 104:
        if mk(tam).getlength(txt) <= LARG:
            return [Palavra(txt, mk(tam), cor, brilho)]
        tam -= 6
    ws = txt.split()
    if len(ws) > 1:
        corte = min(range(1, len(ws)), key=lambda i: abs(len(" ".join(ws[:i])) - len(" ".join(ws[i:]))))
        linhas = [" ".join(ws[:corte]), " ".join(ws[corte:])]
        tam = 150 if estilo == "serif" else 132
        while tam > 70 and max(mk(tam).getlength(l) for l in linhas) > LARG:
            tam -= 6
        return [Palavra(l, mk(tam), cor, brilho) for l in linhas]
    while tam > 60 and mk(tam).getlength(txt) > LARG:
        tam -= 6
    return [Palavra(txt, mk(tam), cor, brilho)]


# ------------------------------------------------------------------------------------------- rosto (regra do Felipe)
def medir_rostos(video):
    """Base do rosto MAIOR (fração da altura) ~8 vezes por segundo. Só rostos de verdade (altura > 5% do quadro)."""
    import cv2
    w2, h2 = 270, 480
    yn = cv2.FaceDetectorYN.create(str(AQUI / "face_detection_yunet.onnx"), "", (w2, h2), 0.6, 0.3, 5000)
    dec = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-vf", "fps=8,scale={}:{}".format(w2, h2),
                            "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)
    out, k, n = [], 0, w2 * h2 * 3
    while True:
        buf = dec.stdout.read(n)
        if len(buf) < n:
            break
        _, fs = yn.detect(np.frombuffer(buf, np.uint8).reshape(h2, w2, 3))
        rs = [f for f in (fs if fs is not None else []) if f[3] / h2 > 0.05]
        maior = max(rs, key=lambda f: f[2] * f[3]) if rs else None
        out.append((k / 8.0, float(maior[1] + maior[3]) / h2 if maior is not None else None))
        k += 1
    dec.wait()
    return out


def linha_fixa(rostos):
    """Uma linha só para o corte inteiro (legenda fixa): abaixo do rosto do Glauton em 95% do tempo."""
    bs = sorted(y for t, y in rostos if y is not None)
    if not bs:
        return SEM_ROSTO
    return clamp(bs[min(len(bs) - 1, int(len(bs) * .95))] + FOLGA, PISO, TETO)


# ------------------------------------------------------------------------------------------- plano
def na_fala(ws, texto, de, ate):
    """Segundo em que o texto começa a ser dito (as palavras dele em sequência, na janela) ou None. Número aceita com/sem "R$"."""
    alvo = [x for x in (_norm(p) for p in str(texto or "").split()) if x and x not in ("r",)]
    if not alvo:
        return None
    forte = [x for x in alvo if len(x) >= 3 or x.isdigit()] or alvo   # "a virada": o "a" sozinho não vale
    nomes = [_norm(w["w"]) for w in ws]
    casa = lambda a, b: a == b or (a.isdigit() and b.startswith(a))
    for k, w in enumerate(ws):
        if not (de <= float(w["t"]) <= ate):
            continue
        if casa(alvo[0], nomes[k]) or casa(forte[0], nomes[k]):
            seq = nomes[max(0, k - 1):k + len(alvo) + 2]
            if sum(1 for x in forte if any(casa(x, y) for y in seq)) >= max(1, round(len(forte) * .8)):
                return float(w["t"])
    return None


def ancorar(evs, ws, dur):
    """O texto só entra quando é DITO (time do lote 66: "o motion apareceu antes da fala e muito rápido") e a linha fina é fala
    literal ("a legenda ficou parada" era a linha fina com um texto que ele não disse). Cada palavra forte fica 1,2 s no mínimo."""
    if not ws:
        return evs
    for e in evs:
        de, ate = float(e["de"]), float(e["ate"])
        for f in e.get("forte") or []:
            t = na_fala(ws, f.get("texto"), de - 2.0, ate + 2.0)
            if t is not None:
                f["t"] = t
        if e.get("lead") and na_fala(ws, e["lead"], de - 3.0, ate + 1.0) is None:
            e["lead"] = ""   # paráfrase: sai (ficava parada na tela com um texto que ele não disse)
        ts = [float(f["t"]) for f in e.get("forte") or [] if isinstance(f.get("t"), (int, float))]
        if ts:
            e["de"] = max(0.0, min(de, min(ts) - (0.6 if e.get("lead") else 0.25)))
            e["ate"] = min(dur, max(ate, max(ts) + 1.2))
    return evs


def limpar(evs, dur):
    """Tempos dentro do vídeo, sem sobrepor, no máximo MAX_ESCURO telas escuras, textos curtos."""
    out, vistos = [], set()
    for e in sorted((e for e in evs or [] if isinstance(e, dict)), key=lambda e: float(e.get("de") or 0)):
        try:
            de, ate = max(0.0, float(e["de"])), min(dur, float(e["ate"]))
        except (KeyError, TypeError, ValueError):
            continue
        forte = e.get("forte") or []
        if isinstance(forte, str):
            forte = [{"texto": forte}]
        forte = [f if isinstance(f, dict) else {"texto": str(f)} for f in forte if f]
        forte = [f for f in forte if str(f.get("texto") or "").strip()][:4]
        if not forte and not e.get("conta"):
            continue
        if ate - de < 1.6 or (out and de < out[-1]["ate"] + RESPIRO):   # 2 s só de rosto e legenda entre dois eventos
            continue
        chave = {_norm(str(f.get("texto") or "")) for f in forte}
        if chave & vistos:   # a mesma palavra forte duas vezes no corte ("SUPER APP" e "SUPERAPP")
            continue
        vistos |= chave
        ts = []
        for i, f in enumerate(forte or [{"texto": ""}]):
            t = f.get("t")
            t = float(t) if isinstance(t, (int, float)) else de + 0.2 + i * 1.1
            ts.append(clamp(t, de, ate - 1.0))   # cada palavra forte fica pelo menos ~1 s
        for i in range(1, len(ts)):
            ts[i] = max(ts[i], ts[i - 1] + 0.5)
        sub = e.get("sub") if isinstance(e.get("sub"), dict) and str(e["sub"].get("texto") or "").strip() else None
        if sub:
            st = sub.get("t")
            sub = {"texto": str(sub["texto"])[:28], "t": clamp(float(st) if isinstance(st, (int, float)) else ts[-1] + .5, de, ate - .3)}
        barra = e.get("barra")
        barra = clamp(float(barra), 0, 100) if isinstance(barra, (int, float)) else None
        conta = e.get("conta") if isinstance(e.get("conta"), dict) else None
        if conta:
            try:
                conta = {"de": float(conta.get("de") or 0), "ate": float(conta["ate"]), "prefixo": str(conta.get("prefixo") or ""),
                         "sufixo": str(conta.get("sufixo") or ""), "dur": clamp(float(conta.get("dur") or .8), .3, 2.0)}
            except (KeyError, TypeError, ValueError):
                conta = None
        out.append({"de": de, "ate": ate, "fundo": "escuro" if e.get("fundo") == "escuro" else "video",
                    "lead": str(e.get("lead") or "")[:40], "estilo": "serif" if e.get("estilo") == "serif" else "pesado",
                    "cor": AMARELO if e.get("cor") == "amarelo" else BRANCO, "sub": sub, "barra": barra, "conta": conta,
                    "forte": [{"texto": str(f.get("texto") or "")[:32], "t": t} for f, t in zip(forte or [{"texto": ""}], ts)]})
    teto = max(2, int(dur / 7) + 1)   # no máximo 1 evento a cada 7 s: sobram a abertura e os mais longos
    if len(out) > teto:
        fica = {0} | set(sorted(range(1, len(out)), key=lambda i: -(out[i]["ate"] - out[i]["de"]))[:teto - 1])
        out = [e for i, e in enumerate(out) if i in fica]
    escuros = [e for e in out if e["fundo"] == "escuro"]
    for e in escuros[MAX_ESCURO:]:
        e["fundo"] = "video"
    return out


def sombra_bloco():
    """Escurecido macio atrás da frase sobre o vídeo (o itálico amarelo some na camiseta roxa sem ele)."""
    y, x = np.mgrid[0:700, 0:W].astype(np.float32)
    d = np.sqrt(((x - W / 2) / (W * .62)) ** 2 + ((y - 350) / 330) ** 2)
    a = (np.clip(1 - d, 0, 1) ** 1.4 * 170).astype(np.uint8)
    return Image.merge("RGBA", (*Image.new("RGB", (W, 700), (8, 4, 14)).split(), Image.fromarray(a)))


def fundo_escuro():
    """Grafite com um roxo leve no centro (o mesmo da maquete)."""
    y, x = np.mgrid[0:H, 0:W].astype(np.float32)
    d = np.sqrt(((x - W * .5) / (W * .7)) ** 2 + ((y - H * .46) / (H * .42)) ** 2)
    c0, c1, c2 = np.array([42, 24, 64]), np.array([20, 11, 32]), np.array([6, 3, 10])
    k1 = np.clip(d / .55, 0, 1)[..., None]
    k2 = np.clip((d - .55) / .45, 0, 1)[..., None]
    rgb = c0 * (1 - k1) + c1 * k1
    rgb = rgb * (1 - k2) + c2 * k2
    g = np.clip(1 - np.sqrt(((x - W * .5) / (W * .38)) ** 2 + ((y - H * .46) / (H * .2)) ** 2), 0, 1)[..., None]
    rgb = rgb + np.array([165, 67, 250]) * .14 * g
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))


class Evento:
    def __init__(self, e):
        self.e = e
        self.lead = Palavra(e["lead"].upper() if e["fundo"] == "escuro" else e["lead"],
                            sans(44 if e["fundo"] == "escuro" else 64, 500), BRANCO, tracking=6 if e["fundo"] == "escuro" else 0) if e["lead"] else None
        self.fortes = [] if e["conta"] else [caber(f["texto"], e["estilo"], e["cor"]) for f in e["forte"]]
        self.sub = Palavra(e["sub"]["texto"].upper(), sans(58, 900), AMARELO if e["cor"] == BRANCO else BRANCO,
                           brilho=e["cor"] == BRANCO) if e["sub"] else None
        self.contas = {}

    def conta_txt(self, t):
        c = self.e["conta"]
        v = c["de"] + (c["ate"] - c["de"]) * ease((t - self.e["forte"][0]["t"]) / c["dur"])
        casas = 0 if abs(c["ate"]) >= 10 or float(c["ate"]).is_integer() else 1
        s = "{}{:,.{}f}{}".format(c["prefixo"], v, casas, c["sufixo"]).replace(",", "X").replace(".", ",").replace("X", ".")
        if s not in self.contas:
            self.contas[s] = caber(s, "pesado", self.e["cor"])
        return self.contas[s]

    def altura(self):
        h = (self.lead.alt + 10) if self.lead else 0
        maior = max((sum(p.alt for p in ls) for ls in self.fortes), default=150)
        h += maior if self.fortes else 150
        h += (self.sub.alt + 8) if self.sub else 0
        h += 50 if self.e["barra"] is not None else 0
        return h

    def desenha(self, cv, topo, t):
        e = self.e
        sai = clamp((t - e["ate"]) / .22)
        alfa = 1 - sai
        y = topo
        if self.lead:
            kl = ease((t - e["de"]) / .35)
            self.lead.desenha(cv, W / 2, y + (1 - kl) * 24, e["de"] - 1, t, alfa=kl * alfa * (.8 if e["fundo"] == "escuro" else 1))
            y += self.lead.alt + 10
        if e["conta"]:
            t0 = e["forte"][0]["t"]
            if t >= t0 - .01:
                for p in self.conta_txt(t):
                    p.desenha(cv, W / 2, y, t0, t, passo=0, dur=.25, alfa=alfa)
                    y += p.alt
            else:
                y += sum(p.alt for p in self.conta_txt(t0))
        else:
            h_max = max(sum(p.alt for p in ls) for ls in self.fortes)
            for i, (f, ls) in enumerate(zip(e["forte"], self.fortes)):
                fim = e["forte"][i + 1]["t"] if i + 1 < len(e["forte"]) else e["ate"] + 1
                if not (f["t"] - .01 <= t < fim):
                    continue
                a = alfa * (1 - clamp((t - (fim - .14)) / .14))
                yy = y + (h_max - sum(p.alt for p in ls)) / 2
                n = sum(len(p.txt) for p in ls)
                for p in ls:
                    p.desenha(cv, W / 2, yy, f["t"], t, passo=.03 if n > 9 else .045, alfa=a)
                    yy += p.alt
            y += h_max
        if self.sub:
            self.sub.desenha(cv, W / 2, y + 8, e["sub"]["t"], t, passo=.03, dur=.25, alfa=alfa)
            y += self.sub.alt + 8
        if e["barra"] is not None:
            k = ease((t - (e["forte"][0]["t"] + .3)) / .8)
            bw, bh = 590, 14
            barra = Image.new("RGBA", (bw + 80, bh + 80), (0, 0, 0, 0))
            d = ImageDraw.Draw(barra)
            d.rounded_rectangle((40, 40, 40 + bw, 40 + bh), bh // 2, fill=(255, 255, 255, 46))
            cheio = int(bw * e["barra"] / 100 * k)
            if cheio > bh:
                g = Image.new("RGBA", barra.size, (0, 0, 0, 0))
                ImageDraw.Draw(g).rounded_rectangle((40, 40, 40 + cheio, 40 + bh), bh // 2, fill=AMARELO + (200,))
                barra.alpha_composite(g.filter(ImageFilter.GaussianBlur(10)))
                ImageDraw.Draw(barra).rounded_rectangle((40, 40, 40 + cheio, 40 + bh), bh // 2, fill=AMARELO + (255,))
            cola(cv, com_alpha(barra, alfa * ease((t - e["de"]) / .3)), W / 2 - bw / 2 - 40, y + 36 - 40)


# ------------------------------------------------------------------------------------------- legenda de uma palavra
def _norm(s):
    import unicodedata
    s = unicodedata.normalize("NFD", str(s).lower())
    return re.sub(r"[^a-z0-9%]", "", "".join(c for c in s if unicodedata.category(c) != "Mn"))


def alinhar(grafia, medidas):
    """Texto da legenda revisada (grafia certa) com o TEMPO medido no vídeo. Palavra só da medição (ruído) fica de fora."""
    import difflib
    A, B = [_norm(w["w"]) for w in grafia], [_norm(w["w"]) for w in medidas]
    out = []
    for op, a0, a1, b0, b1 in difflib.SequenceMatcher(None, A, B, autojunk=False).get_opcodes():
        if op == "equal" or (op == "replace" and a1 - a0 == b1 - b0):
            out += [{"w": grafia[a0 + k]["w"], "t": float(medidas[b0 + k]["t"]), "e": float(medidas[b0 + k]["e"])} for k in range(a1 - a0)]
        elif op == "replace":   # contagens diferentes: espalha as palavras da grafia no tempo medido do trecho
            t0, t1 = float(medidas[b0]["t"]), float(medidas[b1 - 1]["e"])
            d = max(0.05, (t1 - t0) / (a1 - a0))
            out += [{"w": grafia[a0 + k]["w"], "t": t0 + k * d, "e": t0 + (k + 1) * d} for k in range(a1 - a0)]
        elif op == "delete":   # sem medida: fica com o tempo da grafia, sem passar da vizinha
            out += [dict(w, t=float(w["t"]), e=float(w["e"])) for w in grafia[a0:a1]]
    for k in range(1, len(out)):   # a ordem é a da fala (a medição às vezes troca a ordem); o tempo nunca anda para trás
        out[k]["t"] = max(out[k]["t"], out[k - 1]["t"] + 0.05)
        out[k]["e"] = max(out[k]["e"], out[k]["t"] + 0.05)
    return out


def unidades(ws):
    """Uma palavra por vez; a palavra que ficaria menos de 0,22 s na tela sai junto com a seguinte (até 2 palavras, 16 letras)."""
    limpa = lambda w: re.sub(r"[.,;:!?…]+$", "", str(w["w"]).strip())
    us, i = [], 0
    while i < len(ws):
        t0, txt = float(ws[i]["t"]), limpa(ws[i])
        prox = float(ws[i + 1]["t"]) if i + 1 < len(ws) else t0 + .7
        if (prox - t0 < 0.22 and i + 1 < len(ws) and len(txt) + len(limpa(ws[i + 1])) <= 15
                and not re.search(r"[.,;:?!…]$", str(ws[i]["w"]).strip())):   # nunca atravessa vírgula nem fim de frase
            txt += " " + limpa(ws[i + 1])
            i += 1
        us.append([t0, txt])
        i += 1
    for k, u in enumerate(us):   # fica até a próxima, no máximo 0,9 s
        u.append(min(us[k + 1][0] if k + 1 < len(us) else u[0] + .7, u[0] + .9))
    return us


def palavra_em(us, t):
    for i, (t0, txt, t1) in enumerate(us):
        if t0 - .08 <= t < t1 - .08:   # entra um tiquinho antes da voz, nunca depois
            return i, txt
        if t0 - .08 > t:
            break
    return None, ""


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
    ws = [w for w in p.get("palavras") or [] if str(w.get("w", "")).strip()]
    med = [w for w in p.get("palavras_medidas") or [] if str(w.get("w", "")).strip()]
    if med:
        ws = alinhar(ws, med) if ws else med
    us = unidades(ws)
    brutos = [dict(e, forte=[dict(f) if isinstance(f, dict) else {"texto": str(f)} for f in (e.get("forte") or [])]
                   if not isinstance(e.get("forte"), str) else [{"texto": e["forte"]}])
              for e in p.get("eventos") or [] if isinstance(e, dict) and "de" in e and "ate" in e]
    evs = [Evento(e) for e in limpar(ancorar(brutos, ws, dur), dur)]
    linha = linha_fixa(medir_rostos(video))
    g = p.get("gancho") or {}
    g_ate, emenda = float(g.get("ate") or 0), float(g.get("emenda") or 0.6) * H
    no_gancho = lambda t: t < g_ate
    topos = {}
    for ev in evs:
        e = ev.e
        if e["fundo"] == "escuro":
            topos[id(ev)] = int(H * .46 - ev.altura() / 2)
        else:
            topos[id(ev)] = int(H * linha)   # frase alta demais para o espaço: corta o excesso embaixo, nunca sobe no rosto
            if no_gancho(e["de"]):   # gancho dividido: no painel de cima, acima da emenda com o b-roll
                topos[id(ev)] = int(min(topos[id(ev)], emenda - ev.altura() - 24))
    escuro, sombra = fundo_escuro(), sombra_bloco()
    leg_cache = {}
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
            ativos = [ev for ev in evs if ev.e["de"] <= t <= ev.e["ate"] + .25]
            # aproximação lenta durante a frase sobre o vídeo
            z = 1.0
            for ev in ativos:
                if ev.e["fundo"] == "video":
                    z = 1 + .05 * ease((t - ev.e["de"]) / (ev.e["ate"] - ev.e["de"] + .3))
            if z > 1.0005:
                cw, ch = W / z, H / z
                cx0, cy0 = (W - cw) / 2, (H - ch) * .3
                img = img.resize((W, H), Image.BICUBIC, box=(cx0, cy0, cx0 + cw, cy0 + ch))
            # foco entrando no começo e na volta de cada tela escura
            foco = 14 * (1 - ease(t / .45))
            for ev in evs:
                if ev.e["fundo"] == "escuro" and ev.e["ate"] < t < ev.e["ate"] + .5:
                    foco = max(foco, 14 * (1 - ease((t - ev.e["ate"]) / .5)))
            if foco > .4:
                peq = img.resize((W // 4, H // 4), Image.BILINEAR).filter(ImageFilter.GaussianBlur(foco / 4))
                img = peq.resize((W, H), Image.BILINEAR)
            for ev in ativos:
                if ev.e["fundo"] == "escuro":
                    a = clamp((t - ev.e["de"]) / .14) * (1 - clamp((t - ev.e["ate"]) / .22))
                    if a > 0.001:
                        img = Image.blend(img, escuro, a)
            cv = img.convert("RGBA")
            for ev in ativos:
                if ev.e["fundo"] == "video":
                    a = clamp((t - ev.e["de"]) / .3) * (1 - clamp((t - ev.e["ate"]) / .22))
                    alt = ev.altura()
                    sb = sombra.resize((W, max(200, int(alt * 1.9))), Image.BILINEAR)
                    cola(cv, com_alpha(sb, a), 0, topos[id(ev)] + alt / 2 - sb.height / 2)
                ev.desenha(cv, topos[id(ev)], t)
            if not any(ev.e["de"] - .05 <= t <= ev.e["ate"] + .2 for ev in evs):
                i, w = palavra_em(us, t)
                if w:
                    if w not in leg_cache:
                        leg_cache[w] = Palavra(w, sans(62, 900), BRANCO)
                    pw = leg_cache[w]
                    y = min(H * linha, emenda - pw.alt - 40) if no_gancho(t) else H * linha
                    pw.desenha(cv, W / 2, y, us[i][0] - .08, t, passo=0, dur=.08)
            enc.stdin.write(cv.convert("RGB").tobytes())
            k += 1
    finally:
        enc.stdin.close()
        enc.wait()
        dec.stdout.close()
        dec.wait()
    if enc.returncode or not saida.exists():
        raise SystemExit("render do Glautin (cinema) falhou")
    print(json.dumps({"ok": True, "quadros": k, "eventos": [(round(ev.e["de"], 1), ev.e["fundo"], [f["texto"] for f in ev.e["forte"]]) for ev in evs],
                      "linha": round(linha, 3), "saida": str(saida)}, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    render(sys.argv[1])
