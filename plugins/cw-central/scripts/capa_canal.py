#!/usr/bin/env python3
"""Capas dos canais de corte (Tarsila): Zeca · canal 2, Breu · canal dark e Nanquim (@mentalidaderestaurante).

A Capitu faz as capas do CW Cast (Titeu); a Tarsila faz as dos outros canais, cada uma no visual do canal:
  canal2  (Zeca, @cortescw.delivery)        foto em cor, título branco Montserrat com a palavra-chave verde-neon
  dark    (Breu, @corte_sdelivery)          foto escura quase sem cor, título Anton branco com barra vermelha na palavra-chave
  nanquim (@mentalidaderestaurante)         foto P&B (INSPIRADOR: cor de filme), título Montserrat com a palavra-chave roxa
Tudo o que importa fica no miolo 1080x1440 (o recorte 3:4 que o Instagram mostra no perfil).

  python3 capa_canal.py candidatos --video <bruto ou corte> [--trechos "[[a,b],...]"] --out <pasta>
      → candidatos.png (quadros numerados com o rosto grande e nítido) e candidatos.json ({n: segundo})
  python3 capa_canal.py montar --canal nanquim --video <bruto> --t 123.4 --titulo "Eu não abri restaurante pra ser *mais um*" \
      [--tipo inspirador] [--arroba @x] --out saida/capa_1/capa.png
      → capa.png 1080x1920 e grid.png (a capa ao lado do recorte 3:4 do perfil, para conferir)
O título leva a palavra-chave entre *asteriscos* (uma só). Sem logo da marca em nenhum canal.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

AQUI = Path(__file__).resolve().parent
FONTES = AQUI / "dark01" / "fonts"
W, H = 1080, 1920
MIOLO = (240, 1680)   # faixa de 1440 px que o perfil mostra (3:4)
CANAIS = {
    "canal2": {"arroba": "@cortescw.delivery", "fonte": "Montserrat-ExtraBold.ttf", "destaque": (60, 255, 140), "cor": "foto"},
    "dark": {"arroba": "@corte_sdelivery", "fonte": "Anton-Regular.ttf", "destaque": (229, 35, 43), "cor": "escura"},
    "nanquim": {"arroba": "@mentalidaderestaurante", "fonte": "Montserrat-ExtraBold.ttf", "destaque": (109, 40, 217), "cor": "pb"},
}


def fonte(nome, tam):
    return ImageFont.truetype(str(FONTES / nome), int(tam))


def tamanho_video(video):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
                        "-of", "json", str(video)], capture_output=True, text=True)
    d = json.loads(r.stdout or "{}")
    s = (d.get("streams") or [{}])[0]
    return int(s.get("width") or 1920), int(s.get("height") or 1080), float((d.get("format") or {}).get("duration") or 0)


def quadro(video, t, larg=None):
    vf = ["-vf", "scale={}:-2".format(larg)] if larg else []
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", "{:.3f}".format(max(0, t)), "-i", str(video), "-frames:v", "1"] + vf +
                       ["-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
    if not r.stdout:
        raise SystemExit("não consegui tirar o quadro de {} em {:.1f} s".format(video, t))
    import io
    return Image.open(io.BytesIO(r.stdout)).convert("RGB")


def rostos(img):
    """Rostos (x, y, w, h, nitidez) num quadro, pelo YuNet (o mesmo detector do falante.py)."""
    import cv2
    a = np.asarray(img)[:, :, ::-1]
    esc = 960 / a.shape[1]
    peq = cv2.resize(a, (960, int(a.shape[0] * esc)))
    yn = cv2.FaceDetectorYN.create(str(AQUI / "face_detection_yunet.onnx"), "", (peq.shape[1], peq.shape[0]), 0.7, 0.3, 5000)
    _, fs = yn.detect(peq)
    out = []
    for f in (fs if fs is not None else []):
        x, y, w, h = (float(v) / esc for v in f[:4])
        cinza = cv2.cvtColor(a[int(max(0, y)):int(y + h), int(max(0, x)):int(x + w)], cv2.COLOR_BGR2GRAY)
        nit = float(cv2.Laplacian(cinza, cv2.CV_64F).var()) if cinza.size else 0
        out.append((x, y, w, h, nit))
    return out


def cmd_candidatos(a):
    largura, altura, dur = tamanho_video(a.video)
    trechos = json.loads(a.trechos) if a.trechos else [[0, dur]]
    tempos = []
    for ini, fim in trechos:
        n = max(2, int((fim - ini) / 3))
        tempos += list(np.linspace(ini + 0.5, fim - 0.5, n))
    achados = []
    for t in tempos[:60]:
        img = quadro(a.video, t, 1280)
        rs = rostos(img)
        if not rs:
            continue
        x, y, w, h, nit = max(rs, key=lambda r: r[2] * r[3])
        centro = 1 - abs((x + w / 2) / img.width - 0.5)
        achados.append((w * h * centro * min(1.0, nit / 120), t, img))
    achados.sort(key=lambda x: -x[0])
    escolhidos = []
    for sc, t, img in achados:
        if all(abs(t - e[1]) > 4 for e in escolhidos):
            escolhidos.append((sc, t, img))
        if len(escolhidos) >= 9:
            break
    escolhidos.sort(key=lambda x: x[1])
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tw, th = 420, 236
    folha = Image.new("RGB", (tw * 3, th * ((len(escolhidos) + 2) // 3) or th), (20, 20, 20))
    d = ImageDraw.Draw(folha)
    mapa = {}
    for k, (sc, t, img) in enumerate(escolhidos):
        x, y = (k % 3) * tw, (k // 3) * th
        folha.paste(img.resize((tw, th)), (x, y))
        d.rectangle((x, y, x + 64, y + 40), fill=(0, 0, 0))
        d.text((x + 10, y + 4), str(k + 1), font=fonte("Montserrat-ExtraBold.ttf", 28), fill=(255, 255, 255))
        mapa[str(k + 1)] = round(float(t), 2)
    folha.save(out / "candidatos.png")
    (out / "candidatos.json").write_text(json.dumps(mapa, ensure_ascii=False, indent=1), encoding="utf-8")
    print("ok", out / "candidatos.png", len(mapa), "quadros")


def foto_vertical(img, cor, inspirador=False):
    """Recorte 9:16 em volta do rosto principal (rosto a ~30% da altura), já no tratamento de cor do canal."""
    rs = rostos(img)
    if rs:
        x, y, w, h, _ = max(rs, key=lambda r: r[2] * r[3])
        cx, cy, fh = x + w / 2, y + h / 2, h
    else:
        cx, cy, fh = img.width / 2, img.height * 0.4, img.height * 0.2
    ch = min(img.height, max(fh * 5.2, img.height * 0.6))   # cabeça e ombros, com folga
    cw = ch * 9 / 16
    if cw > img.width:
        cw, ch = img.width, img.width * 16 / 9
    x0 = min(max(0, cx - cw / 2), img.width - cw)
    y0 = min(max(0, cy - ch * 0.30), img.height - ch)
    foto = img.crop((int(x0), int(y0), int(x0 + cw), int(y0 + ch))).resize((W, H), Image.LANCZOS)
    if cor == "pb" and not inspirador:
        sys.path.insert(0, str(AQUI))
        import nanquim
        g = np.asarray(foto.convert("L"))
        foto = Image.fromarray(nanquim.lut_pb()[g]).convert("RGB")
    elif cor == "pb" and inspirador:
        sys.path.insert(0, str(AQUI))
        import nanquim
        foto = Image.fromarray(nanquim.grade_cinema(np.asarray(foto), np.ones((H, W, 1), np.float32)))
    elif cor == "escura":
        g = foto.convert("L")
        foto = Image.blend(foto, Image.merge("RGB", (g, g, g)), 0.85)
        foto = Image.eval(foto, lambda v: int(v * 0.72))
    return foto


def degrade(foto, de=0.48):
    """Escurece de baixo para cima a partir de `de` da altura, para o título ler sobre a foto."""
    m = np.linspace(0, 1, H)[:, None]
    a = np.clip((m - de) / (1 - de), 0, 1) ** 1.2 * 0.88
    arr = np.asarray(foto).astype(np.float32) * (1 - a[..., None])
    return Image.fromarray(arr.astype(np.uint8))


def tokens(titulo):
    out = []
    for k, parte in enumerate(re.split(r"\*", titulo)):
        for p in parte.split():
            out.append((p, k % 2 == 1))
    return out


def montar_titulo(cv, titulo, fonte_nome, destaque, modo, y_base, larg_max):
    """Título em caixa alta, quebrado em linhas e alinhado ao centro, terminando em y_base. modo cor: a palavra-chave pinta;
    modo barra (Breu): barra vermelha atrás da palavra-chave, texto branco."""
    toks = [(p.upper(), d) for p, d in tokens(titulo)]
    tam = 118 if fonte_nome.startswith("Anton") else 92
    while True:
        f = fonte(fonte_nome, tam)
        esp = f.getlength(" ")
        linhas, cur, lw = [], [], 0
        for p, d in toks:
            w = f.getlength(p)
            if cur and lw + esp + w > larg_max:
                linhas.append(cur)
                cur, lw = [], 0
            cur.append((p, d, w))
            lw += (esp if len(cur) > 1 else 0) + w
        if cur:
            linhas.append(cur)
        if len(linhas) <= 4 or tam <= 60:
            break
        tam -= 6
    asc, desc = f.getmetrics()
    lh = int(tam * (1.02 if fonte_nome.startswith("Anton") else 1.06))
    y = y_base - lh * len(linhas)
    camada = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(camada)
    sombra = Image.new("L", (W, H), 0)
    ds = ImageDraw.Draw(sombra)
    for ln in linhas:
        total = sum(w for _, _, w in ln) + esp * (len(ln) - 1)
        x = (W - total) / 2
        for p, dest, w in ln:
            if dest and modo == "barra":
                d.rectangle((x - 12, y + tam * 0.10, x + w + 12, y + asc + desc * 0.1), fill=destaque + (255,))
            cor = destaque if dest and modo == "cor" else (255, 255, 255)
            ds.text((x, y), p, font=f, fill=255)
            d.text((x, y), p, font=f, fill=cor + (255,))
            x += w + esp
        y += lh
    s = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    s.putalpha(sombra.filter(ImageFilter.GaussianBlur(tam * 0.14)).point(lambda v: int(v * 0.7)))
    cv.alpha_composite(s)
    cv.alpha_composite(camada)


def cmd_montar(a):
    c = CANAIS[a.canal]
    img = quadro(a.video, a.t)
    foto = degrade(foto_vertical(img, c["cor"], (a.tipo or "").lower() == "inspirador"))
    cv = foto.convert("RGBA")
    montar_titulo(cv, a.titulo, c["fonte"], c["destaque"], "barra" if a.canal == "dark" else "cor", MIOLO[1] - 150, W * 0.84)
    arroba = a.arroba or c["arroba"]
    fa = fonte("Montserrat-Bold.ttf", 34)
    d = ImageDraw.Draw(cv)
    d.text((W / 2, MIOLO[1] - 70), arroba, font=fa, fill=(235, 235, 238, 210), anchor="mm")
    if a.canal == "dark":   # o fio vermelho do Breu no alto do miolo
        d.rectangle((W / 2 - 60, MIOLO[0] + 40, W / 2 + 60, MIOLO[0] + 48), fill=c["destaque"] + (255,))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    final = cv.convert("RGB")
    final.save(out)
    grid = Image.new("RGB", (W // 2 + 30 + W // 2, H // 2), (30, 30, 30))
    grid.paste(final.resize((W // 2, H // 2)), (0, 0))
    perfil = final.crop((0, MIOLO[0], W, MIOLO[1])).resize((W // 2, int(1440 / 2)))
    grid.paste(perfil, (W // 2 + 30, (H // 2 - perfil.height) // 2))
    grid.save(out.with_name("grid.png"))
    print("ok", out)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("candidatos")
    p.add_argument("--video", required=True)
    p.add_argument("--trechos")
    p.add_argument("--out", required=True)
    p = sub.add_parser("montar")
    p.add_argument("--canal", required=True, choices=sorted(CANAIS))
    p.add_argument("--video", required=True)
    p.add_argument("--t", type=float, required=True)
    p.add_argument("--titulo", required=True)
    p.add_argument("--tipo")
    p.add_argument("--arroba")
    p.add_argument("--out", required=True)
    a = ap.parse_args()
    {"candidatos": cmd_candidatos, "montar": cmd_montar}[a.cmd](a)


if __name__ == "__main__":
    main()
