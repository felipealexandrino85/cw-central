#!/usr/bin/env python3
"""Fotos e vídeos grátis do Pexels e do Pixabay (só biblioteca padrão): foto do gancho, imagens do Canal 2 e b-roll em vídeo.

Chaves: PEXELS_API_KEY e/ou PIXABAY_API_KEY no ambiente do serviço (a chave nunca é impressa nem gravada aqui).

  buscar  python3 fotos_banco.py search "pizzaria cheia" [--orient portrait|landscape|any] [--n 8]
          lista (fonte:id, tamanho, descrição, autor) dos dois bancos, SEM baixar
  baixar  python3 fotos_banco.py get pexels:123 pixabay:456 [--dest work/<corte>]
          baixa em tamanho grande com nome descritivo e devolve JSON [{"arquivo", "credito"}]
  vídeo   python3 fotos_banco.py search "delivery motorcycle" --video   lista vídeos (fonte:id, tamanho, duração, tags)
          python3 fotos_banco.py get pixabay-video:123 --dest broll     baixa o mp4 (o maior até 1920 px)
"""
import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

UA = {"User-Agent": "cw-studios-esteira/1.0"}


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=dict(UA, **(headers or {})))
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _slug(t):
    t = unicodedata.normalize("NFD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", t).strip("_")[:50] or "foto"


def pexels(q, orient, n):
    k = os.environ.get("PEXELS_API_KEY")
    if not k:
        return []
    p = {"query": q, "per_page": n, "locale": "pt-BR"}
    if orient != "any":
        p["orientation"] = orient
    d = _get("https://api.pexels.com/v1/search?" + urllib.parse.urlencode(p), {"Authorization": k})
    return [{"id": "pexels:{}".format(f["id"]), "tam": "{}x{}".format(f["width"], f["height"]), "desc": f.get("alt") or "",
             "autor": f.get("photographer") or ""} for f in d.get("photos") or []]


def pixabay(q, orient, n):
    k = os.environ.get("PIXABAY_API_KEY")
    if not k:
        return []
    p = {"key": k, "q": q, "image_type": "photo", "per_page": max(3, n), "lang": "pt", "safesearch": "true"}
    if orient != "any":
        p["orientation"] = "vertical" if orient == "portrait" else "horizontal"
    d = _get("https://pixabay.com/api/?" + urllib.parse.urlencode(p))
    return [{"id": "pixabay:{}".format(f["id"]), "tam": "{}x{}".format(f["imageWidth"], f["imageHeight"]), "desc": f.get("tags") or "",
             "autor": f.get("user") or ""} for f in d.get("hits") or []][:n]


def pixabay_videos(q, orient, n):
    k = os.environ.get("PIXABAY_API_KEY")
    if not k:
        return []
    p = {"key": k, "q": q, "per_page": max(3, min(n * 3, 50)), "lang": "pt", "safesearch": "true"}
    d = _get("https://pixabay.com/api/videos/?" + urllib.parse.urlencode(p))
    res = []
    for f in d.get("hits") or []:
        v = (f.get("videos") or {}).get("medium") or (f.get("videos") or {}).get("small") or {}
        w, h = v.get("width") or 0, v.get("height") or 0
        if orient == "portrait" and w > h:
            continue
        if orient == "landscape" and h > w:
            continue
        res.append({"id": "pixabay-video:{}".format(f["id"]), "tam": "{}x{} {}s".format(w, h, f.get("duration") or "?"),
                    "desc": f.get("tags") or "", "autor": f.get("user") or ""})
    return res[:n]


def pexels_videos(q, orient, n):
    k = os.environ.get("PEXELS_API_KEY")
    if not k:
        return []
    p = {"query": q, "per_page": n, "locale": "pt-BR"}
    if orient != "any":
        p["orientation"] = orient
    d = _get("https://api.pexels.com/videos/search?" + urllib.parse.urlencode(p), {"Authorization": k})
    return [{"id": "pexels-video:{}".format(f["id"]), "tam": "{}x{} {}s".format(f["width"], f["height"], f.get("duration") or "?"),
             "desc": Path(urllib.parse.urlparse(f.get("url") or "").path).name.replace("-", " "), "autor": (f.get("user") or {}).get("name") or ""}
            for f in d.get("videos") or []]


def _maior_ate(opcoes, lim=1920):
    """O maior arquivo cujo lado maior não passa de lim (ou o menor de todos, se todos passarem)."""
    ok = [o for o in opcoes if o.get("url") and max(o.get("width") or 0, o.get("height") or 0) <= lim]
    if ok:
        return max(ok, key=lambda o: (o.get("width") or 0) * (o.get("height") or 0))
    return min((o for o in opcoes if o.get("url")), key=lambda o: (o.get("width") or 0) * (o.get("height") or 0))


def baixar(ident, dest):
    fonte, num = ident.split(":", 1)
    if fonte == "pixabay-video":
        d = _get("https://pixabay.com/api/videos/?" + urllib.parse.urlencode({"key": os.environ["PIXABAY_API_KEY"], "id": num}))
        f = d["hits"][0]
        url = _maior_ate(list((f.get("videos") or {}).values()))["url"]
        desc, cred, ext = f.get("tags") or "", "Vídeo: {} / Pixabay".format(f.get("user") or ""), ".mp4"
    elif fonte == "pexels-video":
        f = _get("https://api.pexels.com/videos/videos/" + urllib.parse.quote(num), {"Authorization": os.environ["PEXELS_API_KEY"]})
        url = _maior_ate([{"url": x.get("link"), "width": x.get("width"), "height": x.get("height")} for x in f.get("video_files") or []
                          if (x.get("file_type") or "").endswith("mp4")])["url"]
        desc = Path(urllib.parse.urlparse(f.get("url") or "").path).name.replace("-", " ")
        cred, ext = "Vídeo: {} / Pexels".format((f.get("user") or {}).get("name") or ""), ".mp4"
    if fonte.endswith("-video"):
        arq = Path(dest) / "{}_{}_{}{}".format(fonte.split("-")[0], _slug(desc), num, ext)
        arq.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300) as r, open(arq, "wb") as o:
            o.write(r.read())
        return {"arquivo": str(arq), "credito": cred}
    if fonte == "pexels":
        f = _get("https://api.pexels.com/v1/photos/" + urllib.parse.quote(num), {"Authorization": os.environ["PEXELS_API_KEY"]})
        url, desc, cred = f["src"].get("large2x") or f["src"]["original"], f.get("alt") or "", "Foto: {} / Pexels".format(f.get("photographer") or "")
    elif fonte == "pixabay":
        d = _get("https://pixabay.com/api/?" + urllib.parse.urlencode({"key": os.environ["PIXABAY_API_KEY"], "id": num}))
        f = d["hits"][0]
        url, desc, cred = f["largeImageURL"], f.get("tags") or "", "Foto: {} / Pixabay".format(f.get("user") or "")
    else:
        raise SystemExit("fonte desconhecida: " + fonte)
    ext = ".png" if url.lower().split("?")[0].endswith(".png") else ".jpg"
    arq = Path(dest) / "{}_{}_{}{}".format(fonte, _slug(desc), num, ext)
    arq.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r, open(arq, "wb") as o:
        o.write(r.read())
    return {"arquivo": str(arq), "credito": cred}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("termo")
    s.add_argument("--orient", default="portrait", choices=["portrait", "landscape", "any"])
    s.add_argument("--n", type=int, default=8)
    s.add_argument("--video", action="store_true", help="busca vídeos (b-roll) em vez de fotos")
    g = sub.add_parser("get")
    g.add_argument("ids", nargs="+")
    g.add_argument("--dest", default="broll/fotos_banco")
    a = ap.parse_args()
    if not (os.environ.get("PEXELS_API_KEY") or os.environ.get("PIXABAY_API_KEY")):
        sys.exit("sem chave do Pexels nem do Pixabay neste servidor")
    if a.cmd == "search":
        res = []
        for f in ((pexels_videos, pixabay_videos) if a.video else (pexels, pixabay)):
            try:
                res += f(a.termo, a.orient, a.n)
            except Exception as e:
                print("aviso: {} falhou: {}".format(f.__name__, e), file=sys.stderr)
        for r in res:
            print("{id}\t{tam}\t{desc}\t({autor})".format(**r))
        if not res:
            print("nada encontrado: tente outro termo (em inglês costuma achar mais)")
    else:
        print(json.dumps([baixar(i, a.dest) for i in a.ids], ensure_ascii=False))


if __name__ == "__main__":
    main()
