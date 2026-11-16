#!/usr/bin/env python3
"""Motor do estilo Glautin · Editorial (cortes do Glauton Santos, CEO da Cardápio Web, para o perfil pessoal dele).

Modelo "A · Editorial" escolhido pelo Felipe em 16/11/2026 (maquete: claude.ai/artifact/Q6dthSjRoKEj1TStdZ6RXt), com o
cartão de número do modelo B:
  - o vídeo é o corte do Titeu já enquadrado no Glauton, renderizado SEM legenda, sem headline, sem logo e sem CTA;
  - aproximação lenta da câmera (1,00 → 1,05 no corte inteiro);
  - título em serifa no alto nos primeiros segundos (linha entre *asteriscos* em itálico dourado), assentando devagar;
  - legenda limpa em blocos de até 2 linhas, cada palavra surgindo de um leve desfoque no instante em que é dita;
  - uma palavra-chave por bloco em itálico serifado com um fio dourado que se desenha embaixo;
  - nome e cargo discretos uma vez (fio que cresce);
  - cartão de vidro fosco com o número quando ele cita um dado (o valor conta até o final).
Sem vinheta, sem CTA, sem logo, sem SFX.

Uso: python glautin.py plano.json
plano = {"video", "saida", "palavras": [{"w","t","e"}], "titulo": {"linhas": [...], "de", "ate"},
         "chaves": [palavra, ...], "nome": {"texto", "cargo", "de", "ate"}, "numeros": [{"de","ate","valor","prefixo","sufixo","texto"}]}
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
LEG_W = 940
TIT_Y = 236           # abaixo da barra do Instagram
NOME_Y = 1452
ZOOM = (1.0, 1.05)


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


def fonte(nome, tam, peso=None):
    f = ImageFont.truetype(str(FONTES / nome), tam)
    if peso:
        try:
            f.set_variation_by_axes([peso])
        except Exception:
            pass
    return f


SANS = lambda tam, peso=600: fonte("Figtree[wght].ttf", tam, peso)
SERIF = lambda tam: fonte("InstrumentSerif-Regular.ttf", tam)
ITAL = lambda tam: fonte("InstrumentSerif-Italic.ttf", tam)


def sprite(txt, f, cor=TINTA, sombra=True):
    """Texto num RGBA justo, com uma sombra macia (nada de contorno grosso: o modelo é sóbrio)."""
    x0, y0, x1, y1 = f.getbbox(txt)
    pad = 28
    im = Image.new("RGBA", (x1 - x0 + 2 * pad, y1 + 2 * pad), (0, 0, 0, 0))
    if sombra:
        sm = Image.new("L", im.size, 0)
        ImageDraw.Draw(sm).text((pad - x0, pad + 3), txt, font=f, fill=150)
        sm = sm.filter(ImageFilter.GaussianBlur(9))
        im.putalpha(sm)
    ImageDraw.Draw(im).text((pad - x0, pad), txt, font=f, fill=cor + (255,))
    return im, pad, x0


def borrados(im, raios=(0, 1.5, 3, 4.5, 6)):
    return [im if r == 0 else im.filter(ImageFilter.GaussianBlur(r)) for r in raios]


def com_alpha(im, a):
    if a >= 0.999:
        return im
    out = im.copy()
    out.putalpha(out.getchannel("A").point(lambda v: int(v * a)))
    return out


# ------------------------------------------------------------------------------------------- legenda em blocos
FIM = re.compile(r"[.?!…]['\")\]]*$")


def blocos(ws, max_chars=34, pausa=0.55):
    """Frases (ponto ou pausa) divididas em blocos de tamanho parecido: nunca uma palavra sozinha no fim da frase."""
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
    for k, b in enumerate(out):
        nxt = out[k + 1][0]["t"] if k + 1 < len(out) else None
        fim = float(b[-1]["e"]) + 0.7
        ate = max(fim if nxt is None else min(fim, float(nxt) - 0.06), float(b[-1]["e"]) + 0.15)
        if nxt is not None:   # nunca dois blocos na tela: o próximo entra, este já saiu
            ate = min(ate, float(nxt) - 0.04)
        out[k] = {"ws": b, "de": float(b[0]["t"]) - 0.04, "ate": ate}
    return out


def montar_bloco(bl, chaves):
    """Posição de cada palavra no bloco (até 2 linhas, centralizadas); a 1ª palavra-chave do bloco em itálico serifado."""
    f, fk = SANS(58), ITAL(76)
    marcada = None
    for k, w in enumerate(bl["ws"]):
        if norm(w["w"]) and norm(w["w"]) in chaves:
            marcada = k
            break
    itens = []
    for k, w in enumerate(bl["ws"]):
        chave = k == marcada
        im, pad, x0 = sprite(w["w"], fk if chave else f, TINTA)
        larg = (fk if chave else f).getlength(w["w"])
        itens.append({"w": w, "im": borrados(im), "pad": pad, "larg": larg, "chave": chave})
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
    if len(linhas) > 2:   # bloco longo demais: junta o resto na 2ª linha (a quebra de blocos já evita isso)
        resto = [it for ln, _ in linhas[1:] for it in ln]
        linhas = [linhas[0], (resto, sum(i["larg"] for i in resto) + esp * (len(resto) - 1))]
    base0 = LEG_Y + 64
    for n, (ln, lw) in enumerate(linhas):
        x = (W - lw) / 2
        base = base0 + n * 80
        for it in ln:
            asc = (ITAL(76) if it["chave"] else SANS(58)).getmetrics()[0]
            it["x"], it["y"] = x - it["pad"], base - asc - it["pad"]
            it["base"] = base
            x += it["larg"] + esp
    bl["itens"] = itens
    return bl


def desenha_bloco(cv, bl, t):
    sai = clamp((bl["ate"] - t) / 0.35)
    for it in bl["itens"]:
        t0 = float(it["w"]["t"]) - 0.05
        p = suave((t - t0) / 0.45)
        if p <= 0:
            continue
        nivel = min(len(it["im"]) - 1, int(round((1 - p) * (len(it["im"]) - 1))))
        im = com_alpha(it["im"][nivel], p * sai)
        cv.alpha_composite(im, (int(it["x"]), int(it["y"] + 10 * (1 - p))))
        if it["chave"]:
            q = vai_e_volta((t - t0 - 0.25) / 0.9)
            if q > 0:
                d = ImageDraw.Draw(cv)
                x0 = it["x"] + it["pad"]
                d.rectangle([x0, it["base"] + 12, x0 + it["larg"] * q, it["base"] + 14], fill=OURO + (int(255 * sai),))


# ------------------------------------------------------------------------------------------- título, nome e número
def montar_titulo(tit):
    linhas = []
    for ln in tit.get("linhas") or []:
        ital = ln.strip().startswith("*") and ln.strip().endswith("*")
        txt = ln.strip().strip("*").strip()
        if not txt:
            continue
        f = ITAL(86) if ital else SERIF(86)
        im, pad, x0 = sprite(txt, f, OURO if ital else TINTA)
        linhas.append((im, pad))
    return linhas


def desenha_titulo(cv, linhas, tit, t):
    de, ate = float(tit.get("de", 0.2)), float(tit.get("ate", 3.4))
    p, sai = suave((t - de) / 1.1), clamp((ate - t) / 0.6)
    if p <= 0 or sai <= 0:
        return
    for n, (im, pad) in enumerate(linhas):
        pn = suave((t - de - 0.18 * n) / 1.1)
        cv.alpha_composite(com_alpha(im, pn * sai), (MARGEM - pad, int(TIT_Y + n * 92 - pad + 16 * (1 - pn))))


def desenha_nome(cv, nome, t, cache):
    de, ate = float(nome.get("de", 0)), float(nome.get("ate", 0))
    p, sai = suave((t - de) / 0.8), clamp((ate - t) / 0.6)
    if p <= 0 or sai <= 0:
        return
    if "n" not in cache:
        cache["n"] = sprite(nome.get("texto") or "Glauton Santos", SERIF(54), TINTA)
        cache["c"] = sprite((nome.get("cargo") or "CEO · Cardápio Web").upper(), SANS(25, 650), TINTA)
    im, pad, _ = cache["n"]
    cv.alpha_composite(com_alpha(im, p * sai), (MARGEM - pad, NOME_Y - pad))
    q = vai_e_volta((t - de - 0.15) / 1.0)
    ImageDraw.Draw(cv).rectangle([MARGEM, NOME_Y + 70, MARGEM + 250 * q, NOME_Y + 71], fill=TINTA + (int(160 * sai),))
    im, pad, _ = cache["c"]
    cv.alpha_composite(com_alpha(im, 0.85 * p * sai), (MARGEM - pad, NOME_Y + 90 - pad))


def numero_txt(n, v):
    val = float(n.get("valor") or 0)
    dec = 0 if float(val).is_integer() else 1
    s = "{:,.{}f}".format(v, dec).replace(",", "X").replace(".", ",").replace("X", ".")
    return "{}{}{}".format(n.get("prefixo") or "", s, n.get("sufixo") or "")


CARD = (108, 300, 972, 560)


def desenha_numero(cv, quadro, n, t, cache):
    de, ate = float(n["de"]), float(n["ate"])
    p, sai = suave((t - de) / 0.8), clamp((ate - t) / 0.5)
    if p <= 0 or sai <= 0:
        return
    a = p * sai
    x0, y0, x1, y1 = CARD
    dy = int(24 * (1 - p))
    caixa = (x0, y0 + dy, x1, y1 + dy)
    vidro = quadro.crop(caixa).filter(ImageFilter.GaussianBlur(22)).convert("RGBA")
    vidro.alpha_composite(Image.new("RGBA", vidro.size, (255, 255, 255, 30)))
    masc = Image.new("L", vidro.size, 0)
    ImageDraw.Draw(masc).rounded_rectangle([0, 0, vidro.size[0] - 1, vidro.size[1] - 1], 36, fill=int(255 * a))
    vidro.putalpha(masc)
    cv.alpha_composite(vidro, caixa[:2])
    ImageDraw.Draw(cv).rounded_rectangle(caixa, 36, outline=(255, 255, 255, int(60 * a)), width=2)
    c = vai_e_volta((t - de - 0.3) / 1.4)
    v = float(n.get("valor") or 0) * c
    txt = numero_txt(n, v if c < 1 else float(n.get("valor") or 0))
    im, pad, _ = sprite(txt, SANS(140, 620), TINTA, sombra=False)
    cv.alpha_composite(com_alpha(im, a), (x0 + 44 - pad, y0 + dy + 34 - pad))
    k = ("t", id(n))
    if k not in cache:
        cache[k] = sprite(n.get("texto") or "", SANS(34, 500), TINTA, sombra=False)
    im, pad, _ = cache[k]
    cv.alpha_composite(com_alpha(im, 0.9 * a), (x0 + 46 - pad, y0 + dy + 196 - pad))


def degrade():
    """Escurece de leve o alto e a faixa da legenda (leitura sem caixa atrás do texto)."""
    a = np.zeros((H, 1), np.float32)
    y = np.arange(H, dtype=np.float32)
    a[:, 0] = np.clip((560 - y) / 560, 0, 1) ** 1.6 * 0.42 + np.clip((y - 1020) / 700, 0, 1) ** 1.3 * 0.55
    im = np.zeros((H, W, 4), np.uint8)
    im[..., 3] = (np.repeat(a, W, axis=1) * 255).astype(np.uint8)
    return Image.fromarray(im)


# ------------------------------------------------------------------------------------------- render
def limpar_plano(p, dur):
    """Tempos dentro do vídeo, título curto no começo, número sem cobrir o título, nome uma vez."""
    tit = p.get("titulo") or {}
    tit["linhas"] = [l for l in (tit.get("linhas") or []) if l.strip()][:2]
    tit["de"] = clamp(float(tit.get("de", 0.2)), 0, 1.0)
    tit["ate"] = clamp(float(tit.get("ate", 3.6)), tit["de"] + 2.0, min(5.0, dur))
    p["titulo"] = tit
    nums = []
    for n in sorted(p.get("numeros") or [], key=lambda n: float(n.get("de", 0))):
        try:
            de, ate = float(n["de"]), float(n["ate"])
            float(n.get("valor"))
        except (KeyError, TypeError, ValueError):
            continue
        de = max(de, tit["ate"] + 0.3)
        ate = min(max(ate, de + 2.5), de + 4.5, dur - 0.3)
        if ate - de >= 2.0 and not (nums and de < nums[-1]["ate"] + 1.0):
            nums.append(dict(n, de=round(de, 2), ate=round(ate, 2)))
    p["numeros"] = nums[:3]
    nome = p.get("nome") or {}
    if nome:
        de = clamp(float(nome.get("de", tit["ate"] + 0.6)), tit["ate"] + 0.3, max(tit["ate"] + 0.3, dur - 4))
        nome["de"], nome["ate"] = round(de, 2), round(min(de + 3.2, dur - 0.3), 2)
    p["nome"] = nome
    return p


def render(plano_path):
    plano_path = Path(plano_path)
    p = json.loads(plano_path.read_text(encoding="utf-8"))
    base = plano_path.parent
    video = (base / p["video"]).resolve()
    saida = (base / p["saida"]).resolve()
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate,width,height:format=duration",
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
            z = ZOOM[0] + (ZOOM[1] - ZOOM[0]) * vai_e_volta(t / max(dur, 0.1))
            if z > 1.0005:   # aproximação lenta, ancorada um pouco acima do meio (o rosto)
                cw, ch = W / z, H / z
                cx0, cy0 = (W - cw) / 2, (H - ch) * 0.38
                img = img.resize((W, H), Image.BICUBIC, box=(cx0, cy0, cx0 + cw, cy0 + ch))
            cv = img.convert("RGBA")
            cv.alpha_composite(fundo)
            for n in p["numeros"]:
                if n["de"] <= t < n["ate"]:
                    desenha_numero(cv, img, n, t, cache)
            desenha_titulo(cv, tit, p["titulo"], t)
            if p.get("nome"):
                desenha_nome(cv, p["nome"], t, cache)
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
    print(json.dumps({"ok": True, "quadros": k, "blocos": len(bls), "numeros": len(p["numeros"]), "saida": str(saida)}))


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
        if txt and t1 > t0 and (t0, txt) not in {(a, c) for a, _, c in linhas}:
            linhas.append((t0, t1, txt))
    linhas.sort()
    out, j = [], 0
    for t0, t1, txt in linhas:
        ws = txt.split()
        perto = [w for w in pal if t0 - 0.25 <= float(w["t"]) <= t1 + 0.1]
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
