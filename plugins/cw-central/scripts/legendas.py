#!/usr/bin/env python3
"""Revisor de legendas do CW Studios: corrige palavras mal transcritas antes do vídeo final (marcas, nomes, termos).

1. Dicionário do time (Central › Regras do time › Dicionário): "dominos" → "Domino's", "cardapio web" → "Cardápio Web"…
2. Uma IA barata (etapa "revisao_legenda" do roteamento; padrão Claude Haiku pelo OpenRouter) lê a fala frase por frase e
   aponta só palavras erradas (marca, nome, termo técnico, palavra que não faz sentido). Troca palavra por palavra:
   o tempo das legendas não muda (quando duas palavras viram uma marca, o tempo das duas fica com a marca). Nunca reescreve a fala nem muda o sentido.

Uso:
  legendas.py revisar <arquivo.json> [--contexto "episódio, convidado, assunto"] [--campo w]
     arquivo = transcript.json do Titeu ({"words": [{"w": ...}]}) ou palavras.json dos reels ([{"w": ...}]).
     Grava as correções no próprio arquivo (guarda <arquivo>.antes_revisao.json) e lista o que mudou em <arquivo>.revisao.json.
"""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))

MODELO_PADRAO = "anthropic/claude-haiku-5.5"
PEDIDO = """Você revisa a LEGENDA de um vídeo do CW Studios (Cardápio Web: podcast e reels para donos de restaurante e delivery).
A legenda veio de uma transcrição automática e às vezes erra marcas, nomes e termos (ex.: "dominos" em vez de "Domino's").
Contexto do vídeo: {contexto}
Dicionário do time (escreva sempre assim): {dicionario}

Abaixo, a fala com o número de cada palavra entre colchetes. Aponte SÓ palavras claramente erradas: marca ou nome escrito errado,
termo técnico errado, palavra que não existe ou que não faz sentido no contexto e foi mal ouvida.
NÃO mude o jeito de falar (pra, tá, né, gírias e repetições ficam), não corrija gramática, não reescreva nada.
Cada correção troca UMA palavra por UMA palavra (pode ter apóstrofo, hífen ou espaço, ex.: "Domino's", "Anota AI").
Responda APENAS com JSON: {{"correcoes": [{{"i": 12, "de": "dominos", "para": "Domino's", "por_que": "marca"}}]}} (lista vazia se estiver tudo certo).

{texto}"""


def _norm(t):
    t = unicodedata.normalize("NFD", (t or "").lower())
    return re.sub(r"[^\w' ]", "", "".join(c for c in t if unicodedata.category(c) != "Mn")).strip()


def glossario():
    try:
        import central
        return central.rpc("cw_glossario", timeout=15) or []
    except Exception:
        return []


def modelo_da_etapa():
    try:
        import registro
        for r in registro.roteamento() or []:
            if r.get("etapa") == "revisao_legenda" and r.get("modelo"):
                return r["modelo"]
    except Exception:
        pass
    return MODELO_PADRAO


def pelo_dicionario(palavras, gloss):
    """Troca direta pelo dicionário: uma palavra (ou duas/três seguidas) que bate com um "errado" vira o "certo"."""
    mudou = []
    mapa = {}
    for g in gloss:
        for e in g.get("errados") or []:
            mapa[_norm(e)] = g["certo"]
    for n in (3, 2, 1):
        i = 0
        while i + n <= len(palavras):
            trecho = " ".join(_norm(p) for p in palavras[i:i + n])
            certo = mapa.get(trecho)
            if certo and " ".join(palavras[i:i + n]) != certo:
                pont = re.search(r"[.,!?;:…]+$", palavras[i + n - 1] or "")
                antes = " ".join(palavras[i:i + n])
                partes = certo.split(" ")
                if len(partes) == n:   # "cardapio web" → "Cardápio" "Web"
                    palavras[i:i + n] = partes
                else:                  # "i food" → "iFood" + palavra vazia (o tempo das duas fica com a legenda)
                    palavras[i:i + n] = [certo] + [""] * (n - 1)
                if pont:
                    palavras[i + min(len(partes), n) - 1] += pont.group(0)
                mudou.append({"i": i, "de": antes, "para": palavras[i], "por_que": "dicionário"})
                i += n
            else:
                i += 1
    return mudou


def pela_ia(palavras, contexto, gloss, modelo):
    import ferramentas_mcp
    mudou = []
    dic = "; ".join(g["certo"] + (" (não " + ", ".join(g["errados"][:3]) + ")" if g.get("errados") else "") for g in gloss)[:3000]
    idx = [i for i, p in enumerate(palavras) if p]
    for a in range(0, len(idx), 400):   # ~400 palavras por pedido (um corte inteiro costuma caber num só)
        bloco = idx[a:a + 400]
        texto = " ".join("[{}]{}".format(i, palavras[i]) for i in bloco)
        try:
            r = ferramentas_mcp.chamar({"acao": "texto", "ferramenta": "openrouter", "modelo": modelo, "etapa": "revisao_legenda",
                                        "instrucao": "Responda só com JSON válido.", "trabalho": "revisao_legenda",
                                        "texto": PEDIDO.format(contexto=contexto or "(sem contexto)", dicionario=dic or "(vazio)", texto=texto)},
                                       timeout=120)
            bruto = r.get("texto") or ""
            j = json.loads(re.search(r"\{.*\}", bruto, re.S).group(0))
        except Exception as e:
            print("revisor de legendas: a IA não respondeu ({}); segue só com o dicionário".format(e), file=sys.stderr)
            continue
        for c in j.get("correcoes") or []:
            try:
                i = int(c.get("i"))
            except (TypeError, ValueError):
                continue
            para = str(c.get("para") or "").strip()
            if i not in bloco or not para or len(para) > 40 or _norm(palavras[i]) != _norm(str(c.get("de") or palavras[i])):
                continue   # índice fora, ou a IA "corrigiu" outra palavra: ignora
            if _norm(para) == _norm(palavras[i]) and para == palavras[i]:
                continue
            pont = re.search(r"[.,!?;:…]+$", palavras[i])
            novo = para if re.search(r"[.,!?;:…]$", para) or not pont else para + pont.group(0)
            mudou.append({"i": i, "de": palavras[i], "para": novo, "por_que": c.get("por_que") or "ia"})
            palavras[i] = novo
    return mudou


def revisar_arquivo(arq, contexto="", campo="w"):
    arq = Path(arq)
    dados = json.loads(arq.read_text(encoding="utf-8"))
    lista = dados["words"] if isinstance(dados, dict) else dados
    palavras = [str(w.get(campo) or "") for w in lista]
    gloss = glossario()
    mudou = pelo_dicionario(palavras, gloss)
    mudou += pela_ia(palavras, contexto, gloss, modelo_da_etapa())
    if mudou:
        arq.with_name(arq.stem + ".antes_revisao.json").write_text(arq.read_text(encoding="utf-8"), encoding="utf-8")
        for k, w in enumerate(lista):
            w[campo] = palavras[k]
        # "i food" → "iFood": a palavra que sobrou vazia sai, e o tempo dela passa para a marca (roda antes de qualquer
        # etapa que use a posição das palavras, então os índices ainda não foram usados)
        for k in range(len(lista) - 1, 0, -1):
            if lista[k].get(campo) == "":
                for fim in ("end", "e", "f"):
                    if fim in lista[k] and fim in lista[k - 1]:
                        lista[k - 1][fim] = lista[k][fim]
                del lista[k]
        arq.write_text(json.dumps(dados, ensure_ascii=False, indent=1 if isinstance(dados, dict) else None), encoding="utf-8")
    arq.with_name(arq.stem + ".revisao.json").write_text(json.dumps(mudou, ensure_ascii=False, indent=1), encoding="utf-8")
    return mudou


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("revisar")
    r.add_argument("arquivo")
    r.add_argument("--contexto", default="")
    r.add_argument("--campo", default="w")
    a = ap.parse_args()
    mudou = revisar_arquivo(a.arquivo, a.contexto, a.campo)
    print("legenda revisada: {} correção(ões)".format(len(mudou)))
    for m in mudou:
        print("  [{}] {} → {} ({})".format(m["i"], m["de"], m["para"], m["por_que"]))


if __name__ == "__main__":
    main()
