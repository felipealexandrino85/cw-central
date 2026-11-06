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
ABERTURA = AQUI / "nanquim" / "abertura_charge.mp3"   # som da abertura escolhido pelo Felipe (05/11), −6 dB, no quadro 0
CACHE = Path(os.environ.get("NANQUIM_CACHE", Path.home() / ".cache" / "nanquim"))

PEQ, MED, GRA = 52, 74, 106         # altura de maiúscula ≈ 38 / 54 / 77 px (Montserrat: maiúscula ≈ 0,72 da fonte)
TRACK = -0.025                      # letras quase encostadas
ZOOM_MAX = 1.75                     # no plano aberto do CW Cast a cabeça é pequena: até 1,75× (recorte de 823 px de largura do 1080p)
ABRE = 27                           # quadros da abertura em fenda
SEGURA = 0.6                        # respiro depois da última palavra
CARD = 3.0                          # tela preta com o CTA e a trilha sumindo
CTA = ["ME SIGA PARA MAIS", "CONTEÚDOS COMO ESTE"]
CTA_KICKER = "GOSTOU DESSA HISTÓRIA?"
BRANCO = (255, 255, 255)
DESTAQUE = (109, 40, 217)   # #6D28D9 (Felipe, 06/11/2026; antes 05/11: roxo em vez de amarelo)
MAX_IMPACTO = 2             # frases em destaque por corte (Felipe, 06/11: destacar menos vezes)
MAX_ROXO_INSP = 2           # INSPIRADOR: palavras grandes em destaque por corte
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


def montar_bloco(ws, idx, grande, medio, alinhar, max_w, impacto=False, roxo=False):
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
            # frase de impacto: a frase inteira em destaque (Felipe, 05/11); INSPIRADOR: a palavra grande escolhida ("roxo")
            cor = DESTAQUE if impacto or (roxo and tam > MED) else BRANCO
            out.append({"t": p["t"], "i": p["i"], "tam": tam, "x": x, "y": y, "grande": tam > MED, "cor": cor})
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


FALA_BOCA = 0.30     # sync boca × voz (falante.py) a partir do qual o rosto está falando naquela janela
TROCA_MIN = 2        # janelas seguidas (≈ 2 s) de outra pessoa falando para o recorte trocar de pessoa no mesmo plano


def falas_boca(falante):
    """Quem fala janela a janela (falante.py v2): [(segundo, cx, sync)] com o rosto de boca mais sincronizada com a voz."""
    melhor = {}
    for p in (falante or {}).get("planos") or []:
        for r in p.get("rostos") or []:
            for t, sy in r.get("serie") or []:
                if sy is not None and sy >= FALA_BOCA and (t not in melhor or sy > melhor[t][1]):
                    melhor[t] = (r["cx"], sy)
    return sorted((t, cx, sy) for t, (cx, sy) in melhor.items())


def falante_do_plano(falante, t0, t1):
    """cx de quem fala no plano [t0, t1) pela medida do plano inteiro (boca × voz): o falante marcado ou, se o falante.py ficou
    em dúvida, o rosto de boca mais sincronizada (se passar de 0,2). None se não mediu."""
    melhor, peso = None, 0.0
    for p in (falante or {}).get("planos") or []:
        sob = min(p["fim"], t1) - max(p["ini"], t0)
        if sob <= 0 or not p.get("rostos"):
            continue
        if p.get("falante") is not None:
            cx, conf = p["rostos"][p["falante"]]["cx"], 1.0
        else:
            r = max(p["rostos"], key=lambda r: r["sync"] if r.get("sync") is not None else -9)
            if r.get("sync") is None or r["sync"] < 0.2:
                continue
            cx, conf = r["cx"], 0.5
        if sob * conf > peso:
            melhor, peso = cx, sob * conf
    return melhor


def trilha_recorte(video, dur, emendas, falante=None, ws=None):
    """Janela de recorte (x, y, largura, altura em px do vídeo 1920x1080) para cada quadro.
    Enquadramento SEMPRE o mesmo (Felipe, 05/11/2026: sem zoom e sem mudança de enquadramento): um zoom só para o corte todo,
    sem movimento. QUEM aparece é decidido pela BOCA de quem fala (Felipe, 06/11/2026), em todo plano: falante.py mede a boca
    de cada rosto × a voz janela a janela; o recorte fica em quem fala e, se no mesmo plano a fala passa para a outra pessoa
    (≥ 2 s), corta seco para ela na pausa entre as palavras. Só sem medida de boca é que vale o rosto maior."""
    rostos, cortes, n = detectar(video, dur)
    limites = sorted(set(cortes + [n]))   # só troca de câmera; emenda não muda o recorte
    jan = np.zeros((n, 4))
    serie = falas_boca(falante)
    pausas = [(a["f"] + b["i"]) / 2 for a, b in zip(ws or [], (ws or [])[1:]) if b["i"] - a["f"] >= 0.08]
    planos, alturas = [], []

    def rosto_em(a, b, alvo_cx):
        amostras = [r for i in range(a, b) if rostos[i] for r in rostos[i]]
        if not amostras:
            return None
        if alvo_cx is None:   # sem medida de boca: o rosto maior do plano
            alvo_cx = max(amostras, key=lambda r: r[2])[0]
        perto = [r for r in amostras if abs(r[0] - alvo_cx) < 0.12] or amostras
        return tuple(float(np.median([r[k] for r in perto])) for k in range(3))

    for a, b in zip(limites[:-1], limites[1:]):
        if b <= a:
            continue
        t0, t1 = a / FPS, b / FPS
        base_cx = falante_do_plano(falante, t0, t1)
        # quem fala janela a janela dentro do plano; troca de pessoa só se a outra fala por TROCA_MIN janelas seguidas
        jj = [(t, cx) for t, cx, _ in serie if t0 <= t < t1]
        trechos = []   # [(quadro inicial, cx)]
        atual = base_cx if base_cx is not None else (jj[0][1] if jj else None)
        k = 0
        while k < len(jj):
            t, cx = jj[k]
            if atual is None or abs(cx - atual) < 0.12:
                atual = atual if atual is not None else cx
                k += 1
                continue
            m = k
            while m < len(jj) and abs(jj[m][1] - cx) < 0.12:
                m += 1
            if m - k >= TROCA_MIN:
                tt = max(t0, t - 0.5)   # a janela é de 2 s: a fala nova começa por volta de meio segundo antes do meio dela
                perto = [p for p in pausas if abs(p - tt) <= 0.8 and t0 + 1.0 <= p <= t1 - 1.0]
                if perto:
                    tt = min(perto, key=lambda p: abs(p - tt))
                if t0 + 1.0 <= tt <= t1 - 1.0:
                    trechos.append((int(round(tt * FPS)), cx))
                    atual = cx
            k = m
        bordas = [a] + [q for q, _ in trechos] + [b]
        alvos = [base_cx if base_cx is not None else (jj[0][1] if jj else None)] + [cx for _, cx in trechos]
        for (qa, qb), alvo in zip(zip(bordas[:-1], bordas[1:]), alvos):
            if qb <= qa:
                continue
            c = rosto_em(qa, qb, alvo)
            planos.append((qa, qb, None if c is None else c[:2]))
            if c is not None:
                alturas += [c[2]] * (qb - qa)
    # um zoom só para o corte todo: cabeça ≈ 1,45 × o rosto do detector, ocupando ~40% da altura da faixa
    z = clamp(1080.0 / (float(np.median(alturas)) * 1080 * 1.45 / 0.40), 1.0, ZOOM_MAX) if alturas else 1.0
    ch = 1080.0 / z
    cw = ch * 4 / 3
    for a, b, c in planos:
        if c is None:
            jan[a:b] = [(1920 - cw) / 2, (1080 - ch) / 2, cw, ch]
            continue
        x = float(np.clip(c[0] * 1920 - 0.5 * cw, 0, 1920 - cw))
        y = float(np.clip(c[1] * 1080 - 0.36 * ch, 0, 1080 - ch))
        jan[a:b] = [x, y, cw, ch]
    return jan, n


# ---------------------------------------------------------------- cor
def lut_pb():
    x = np.arange(256) / 255.0
    y = np.clip((x - 0.5) * 1.22 + 0.5 - 0.02, 0, 1)
    y = np.where(y < 0.07, y * 0.4, y)            # pretos profundos
    y = 1 - (1 - y) ** 1.08                        # realce sem estourar
    return (np.clip(y, 0, 0.97) * 255).astype(np.uint8)


def grade_cinema(arr, vinheta):
    """INSPIRADOR: cor de filme que PRESERVA o rosto e as cores da marca (Felipe, 05/11/2026: a versão teal & orange anterior
    deixava a pele pálida e o logo azul). A pele fica natural e um pouco mais quente; só a luz roxa do estúdio fica mais
    contida; curva em S com pretos densos (conversa com o P&B do canal), realce suave, sombras levemente frias e vinheta leve."""
    import cv2
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV_FULL).astype(np.float32)
    h, s, v = hsv[..., 0] * 360 / 256, hsv[..., 1] / 255, hsv[..., 2] / 255
    pele = np.clip(1 - np.abs(((h - 22 + 180) % 360) - 180) / 30, 0, 1) * np.clip((s - 0.10) / 0.15, 0, 1) * np.clip((v - 0.15) / 0.2, 0, 1)
    pele = cv2.GaussianBlur(pele, (0, 0), 3)
    roxo = np.clip(1 - np.abs(((h - 290 + 180) % 360) - 180) / 45, 0, 1) * (1 - pele)
    s = s * (1 - roxo * 0.45) * 0.92 + pele * 0.04
    v = np.where(v > 0.75, 0.75 + (v - 0.75) * 0.8, v)
    hsv = np.stack([hsv[..., 0], np.clip(s, 0, 1) * 255, np.clip(v, 0, 1) * 255], -1).astype(np.uint8)
    f = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB_FULL).astype(np.float32) / 255
    luma = f[..., 0:1] * 0.299 + f[..., 1:2] * 0.587 + f[..., 2:3] * 0.114
    f = np.clip(0.5 + (f - 0.5) * 1.16, 0, 1) - 0.035 * np.clip(1 - luma * 3, 0, 1)
    f = f + np.clip(1 - luma * 2.4, 0, 1) ** 1.5 * (1 - pele[..., None]) * np.array([-0.02, 0.006, 0.025], np.float32)
    f = f + pele[..., None] * np.array([0.02, 0.007, -0.012], np.float32)
    f = f * vinheta
    return (np.clip(f, 0, 1) * 255).astype(np.uint8)


def mascara_vinheta():
    yy, xx = np.mgrid[0:FH, 0:FW].astype(np.float32)
    r2 = ((xx - FW / 2) / (FW / 2)) ** 2 + ((yy - FH / 2) / (FH / 2)) ** 2
    return (1 - 0.12 * np.clip(r2 - 0.3, 0, None))[..., None].astype(np.float32)


# ---------------------------------------------------------------- trilha
PASTA_TIME = CACHE / "drive"   # cópia das pastas de música do time (Drive 1GFrIyATM_GSxkgZO_FhMuLtrc32odcQy), feita pelo servidor
LETRA = re.compile(r"BIA_-_WE_ON_GO|David_Kushner|Narvent|Memory_Reboot", re.I)   # letra cantada compete com a fala: fora


def catalogo():
    """Trilhas do Nanquim. Principal: as pastas do time, lidas na hora ("Épicas e inspiradoras" = epico, para o INSPIRADOR;
    "Tristes" e "Dramáticas e melodramáticas" = triste, para o REFLEXIVO). Reserva (Mixkit, trilhas.json) só entra quando
    não há nenhuma faixa do time na máquina para aquele clima."""
    time_ = []
    if PASTA_TIME.is_dir():
        for arq in sorted(PASTA_TIME.rglob("*.mp3")):
            if LETRA.search(arq.name):
                continue
            pasta = arq.parent.name if arq.parent != PASTA_TIME else ""
            clima = "epico" if re.search(r"[ée]pica|inspira", pasta, re.I) else "triste"
            titulo = re.sub(r"\s*\[[^\]]*\]$", "", arq.stem).replace("_", " ")
            time_.append({"id": "time-" + re.sub(r"[^a-z0-9]+", "-", titulo.lower()).strip("-")[:60], "titulo": titulo, "clima": clima,
                          "arquivo": str(arq), "fonte": "pasta do time: " + pasta, "licenca": "escolhida pelo time"})
    try:
        reserva = [t for t in json.loads(CATALOGO.read_text(encoding="utf-8"))["trilhas"]]
    except (OSError, ValueError, KeyError):
        reserva = []
    climas_time = {t["clima"] for t in time_}
    return time_ + [t for t in reserva if t["clima"] not in climas_time]


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


def boom(arq, sr=48000, dur=2.4):
    """Boom grave sintetizado (queda de 72 para 32 Hz + estalo filtrado), para o corte seco para o CTA."""
    t = np.arange(int(sr * dur)) / sr
    f = 32 + 40 * np.exp(-t * 9)
    fase = 2 * np.pi * np.cumsum(f) / sr
    corpo = np.sin(fase) * (1 - np.exp(-t * 400)) * np.exp(-t * 2.0)
    rng = np.random.default_rng(3)
    ruido = rng.standard_normal(len(t))
    for _ in range(3):   # passa-baixa simples, três vezes
        ruido = np.convolve(ruido, np.ones(24) / 24, "same")
    estalo = ruido / (np.abs(ruido).max() + 1e-9) * np.exp(-t * 22) * 0.45
    x = corpo + estalo
    x = (x / np.abs(x).max() * 0.9 * 32767).astype(np.int16)
    import wave
    with wave.open(str(arq), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.repeat(x, 2).tobytes())
    return arq


def monta_audio(video, voz_dur, total, trilha, virada, saida, tmp, boom_em=None):
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
        # como nas referências: começa ~8 dB abaixo do nível cheio e cresce com a fala até a virada (Felipe, 05/11/2026:
        # "está começando muito alta e sem sentido"); depois segura e some no card
        sobe = max(6.0, min(float(virada or 0) or total * 0.6, total - CARD - 1))
        fc.append("[1:a]atrim=0:{:.3f},asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,loudnorm=I=-27:TP=-8:LRA=11,"
                  "volume='pow(10,(-8+8*min(1,t/{:.3f}))/20)':eval=frame,"
                  "afade=t=in:d=0.6,afade=t=out:st={:.3f}:d={:.3f}[mus]".format(total, sobe, total - CARD, CARD))
        fc.append("[voz][mus]amix=inputs=2:normalize=0:duration=first[mx]")
        mix = "[mx]"
    n_in = 2 if trilha else 1
    if ABERTURA.exists():   # som da abertura (charge) no começo do vídeo, 6 dB mais baixo que o arquivo
        ins += ["-i", str(ABERTURA)]
        fc.append("[{}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume=-6dB,apad=whole_dur={:.3f}[ab]".format(n_in, total))
        fc.append("{}[ab]amix=inputs=2:normalize=0:duration=first[ma]".format(mix))
        mix = "[ma]"
        n_in += 1
    if boom_em is not None:   # boom no corte seco para o CTA
        ins += ["-i", str(boom(tmp / "boom.wav"))]
        ms = round(boom_em * 1000)
        fc.append("[{}:a]volume=0.55,adelay={}|{},apad=whole_dur={:.3f}[bm]".format(n_in, ms, ms, total))
        fc.append("{}[bm]amix=inputs=2:normalize=0:duration=first[mb]".format(mix))
        mix = "[mb]"
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
def fundo_card(faixa_pb):
    """Fundo do CTA na faixa 4:3: o último quadro do corte (P&B, sem legenda), bem desfocado e escurecido."""
    if faixa_pb is None:
        return Image.new("RGB", (FW, FH), (0, 0, 0))
    im = faixa_pb.filter(ImageFilter.GaussianBlur(26)).point(lambda v: int(v * 0.38))
    return im.convert("RGB") if im.mode == "RGB" else Image.merge("RGB", (im, im, im))


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
    """CTA dentro da faixa 4:3 (preto em cima e embaixo, como o resto do corte)."""
    cv = Image.blend(Image.new("RGB", (FW, FH), (0, 0, 0)), fundo, clamp(u / 0.3)).convert("RGBA")
    cx = FW / 2
    # pergunta pequena e cinza, depois o convite digitado, depois o botão
    fk = fonte(34)
    if u > 0.05:
        k_img, dx, dy = sprite(CTA_KICKER, fk, (190, 190, 194))
        k_img.putalpha(k_img.getchannel("A").point(lambda v: int(v * clamp((u - 0.05) / 0.25))))
        cv.alpha_composite(k_img, dest=(round(cx - largura(CTA_KICKER, fk) / 2 - dx), round(FH * 0.20 - dy)))
    fc = fonte(62)
    for li, linha in enumerate(CTA):
        texto_digitado(cv, linha, fc, cx, FH * 0.29 + li * 72, u, 0.3 + 0.45 * li, 0.7 + 0.45 * li)
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
        by = FH * 0.58
        cv.alpha_composite(bot, dest=(round(cx - bot.width / 2), round(by + (bh + 8) / 2 - bot.height / 2)))
        if t_clique <= u < t_clique + 0.5:   # onda do toque
            q = (u - t_clique) / 0.5
            onda = Image.new("RGBA", (FW, FH), (0, 0, 0, 0))
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
    inspirador = (plano.get("tipo") or "").lower() == "inspirador"   # INSPIRADOR: cor de filme e palavra grande em destaque
    escolhas = {int(b["n"]): b for b in plano.get("blocos") or [] if isinstance(b, dict) and str(b.get("n", "")).lstrip("-").isdigit()}
    fr = frases(ws)
    blocos = []
    for n, idx in enumerate(fr):
        e = escolhas.get(n, {})
        blocos.append({"de": ws[idx[0]]["i"] - 0.04, "idx": idx, "grande": (e.get("grande") or "").strip(), "medio": bool(e.get("medio")),
                       "impacto": bool(e.get("impacto")), "roxo": bool(e.get("roxo"))})
    # destaque é raro (Felipe, 06/11): no máximo MAX_IMPACTO frases, as mais perto da virada; INSPIRADOR: MAX_ROXO_INSP palavras
    virada = float(plano.get("virada") or 0)
    marcadas = sorted((b for b in blocos if b["impacto"]), key=lambda b: abs(b["de"] - virada))
    for b in marcadas[MAX_IMPACTO:]:
        b["impacto"] = False
    if inspirador:
        cand = [b for b in blocos if b["grande"] and not b["impacto"]]
        if not any(b["roxo"] for b in cand) and cand:   # plano antigo sem "roxo": as palavras mais espaçadas
            passo_r = max(1, len(cand) // (MAX_ROXO_INSP + 1))
            for b in cand[passo_r::passo_r][:MAX_ROXO_INSP]:
                b["roxo"] = True
        for b in [b for b in cand if b["roxo"]][MAX_ROXO_INSP:]:
            b["roxo"] = False
    for k, b in enumerate(blocos):
        b["ate"] = blocos[k + 1]["de"] if k + 1 < len(blocos) else fim_img + 1 / FPS

    falante = None
    if plano.get("falante"):
        try:
            falante = json.loads((raiz / plano["falante"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            falante = None
    jan, n_lidos = trilha_recorte(video, fim_img, plano.get("emendas") or [], falante, ws)
    lut = lut_pb()

    # alinhamento: alterna entre os blocos (o recorte já põe o rosto entre 46% e 56%; a legenda fica embaixo, no peito)
    ordem = ["center", "left", "right", "center", "right", "left"]
    for k, b in enumerate(blocos):
        b["alinhar"] = ordem[k % len(ordem)]
        max_w = FW * (0.80 if b["alinhar"] == "center" else 0.70)
        b["lay"] = montar_bloco(ws, b["idx"], b["grande"], b["medio"], b["alinhar"], max_w, b["impacto"], inspirador and b["roxo"])
        if b["lay"]:
            alt = max(p["y"] + altura_maiuscula(p["tam"]) for p in b["lay"])
            # o bloco começa sempre na mesma altura (58% da faixa, no peito) e cresce para baixo, linha a linha;
            # só sobe se não couber até 93%
            b["y0"] = min(FH * 0.58, FH * 0.93 - alt)
            b["x0"] = {"left": FW * 0.13, "right": FW * 0.87, "center": FW / 2}[b["alinhar"]]

    _spr = {}

    def spr(t, tam, cor=BRANCO):
        k = (t, tam, cor)
        if k not in _spr:
            _spr[k] = sprite(t, fonte(tam), cor)
        return _spr[k]

    card = {}
    marca = None
    if plano.get("arroba"):   # marca d'água do canal, pequena, no preto logo abaixo da faixa, o vídeo todo
        im, dx, dy = sprite(plano["arroba"], fonte(30), (235, 235, 238))
        im.putalpha(im.getchannel("A").point(lambda v: int(v * 0.6)))
        marca = (np.asarray(im).astype(np.float32), round(W / 2 - largura(plano["arroba"], fonte(30)) / 2 - dx), FY + FH + 26 - dy)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        audio = tmp / "audio.wav"
        trilha = plano.get("trilha")
        monta_audio(video, voz_dur, total, trilha, plano.get("virada"), audio, tmp, boom_em=fim_img)
        if trilha:
            plano["trilha"] = trilha
            Path(plano_path).write_text(json.dumps(plano, ensure_ascii=False, indent=1), encoding="utf-8")
        dec = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-t", "{:.3f}".format(fim_img),
                                "-vf", "fps={}".format(FPS), "-f", "rawvideo", "-pix_fmt", "rgb24" if inspirador else "gray", "-"],
                               stdout=subprocess.PIPE)
        vinheta = mascara_vinheta() if inspirador else None
        canais = 3 if inspirador else 1
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
                buf = dec.stdout.read(sw * sh * canais)
                if len(buf) == sw * sh * canais:
                    ultimo = np.frombuffer(buf, np.uint8).reshape((sh, sw, 3) if inspirador else (sh, sw))
                if ultimo is not None:
                    x, y, cw, ch = jan[min(n, len(jan) - 1)]
                    if inspirador:
                        rec = Image.fromarray(ultimo[int(y):int(y + ch), int(x):int(x + cw)]).resize((FW, FH), Image.BILINEAR)
                        rec = Image.fromarray(grade_cinema(np.asarray(rec), vinheta))
                        faixa = rec.convert("RGBA")
                    else:
                        rec = Image.fromarray(lut[ultimo]).crop((int(x), int(y), int(x + cw), int(y + ch))).resize((FW, FH), Image.BILINEAR)
                        faixa = Image.merge("RGB", (rec, rec, rec)).convert("RGBA")
                    card["ultima"] = rec   # fundo do CTA: o último quadro da faixa, sem legenda
                else:
                    faixa = Image.new("RGBA", (FW, FH), (0, 0, 0, 255))
                b = next((b for b in blocos if b["de"] <= T < b["ate"] and b["lay"]), None)
                if b:
                    for p in b["lay"]:
                        u = T - (p["i"] - 0.03)
                        if u < 0:
                            continue
                        im, dx, dy = spr(p["t"], p["tam"], p.get("cor", BRANCO))
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
                # abertura em fenda: máscara que revela, de 50 px até a faixa cheia, linear (quadros 0-1 pretos). Vem DEPOIS da
                # legenda: a abertura fica por cima de tudo, a legenda só aparece onde a fenda já abriu (Felipe, 05/11/2026)
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
                quadro[FY:FY + FH] = np.asarray(faixa.convert("RGB"))
            else:   # card: o último quadro desfocado atrás, o convite em máquina de escrever e o botão "Seguir" sendo clicado
                if "fundo" not in card:
                    card["fundo"] = fundo_card(card.get("ultima"))
                quadro = np.zeros((H, W, 3), np.uint8)
                quadro[FY:FY + FH] = np.asarray(desenha_card(card["fundo"], T - fim_img))
            if marca is not None:
                im, mx, my = marca
                reg = quadro[my:my + im.shape[0], mx:mx + im.shape[1]].astype(np.float32)
                al = im[..., 3:4] / 255.0
                quadro = np.array(quadro)
                quadro[my:my + im.shape[0], mx:mx + im.shape[1]] = (reg * (1 - al) + im[..., :3] * al).astype(np.uint8)
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
