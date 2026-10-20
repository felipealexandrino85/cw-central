#!/usr/bin/env python3
"""Ferramentas da CW Studios no Claude do editor (servidor MCP local, só biblioteca padrão).

Fala com a função "ferramentas" da central usando o token pessoal do editor; as chaves das APIs (Higgsfield e outras)
ficam só na central. Cada geração conta no limite mensal do editor e aparece para a gestão.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
import central  # noqa: E402

TERMINAIS = {"completed", "failed", "nsfw", "canceled", "cancelled", "erro"}

FERRAMENTAS = [
    {"name": "listar", "description": "Lista as APIs da CW Studios disponíveis (Higgsfield e outras), com o link da documentação "
     "e quantas gerações você já usou no mês. Use antes de gerar.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "gerar", "description": "Gera imagem/vídeo/áudio numa API da CW Studios com a conta da empresa (PAGO: conta no limite "
     "mensal do editor). Antes, leia a documentação da ferramenta (listar → documentacao) para achar o caminho do modelo e os "
     "parâmetros, e confirme com o editor o que vai gerar. Ex. Higgsfield: caminho '/higgsfield-ai/soul/v2/standard', "
     "parametros {\"prompt\": \"...\"}. Arquivos de entrada precisam ser URLs públicas. Devolve request_id; use status para acompanhar.",
     "inputSchema": {"type": "object", "required": ["ferramenta", "caminho", "parametros"], "properties": {
         "ferramenta": {"type": "string", "description": "ex.: higgsfield"},
         "caminho": {"type": "string", "description": "caminho do modelo na API, ex.: /higgsfield-ai/soul/v2/standard"},
         "parametros": {"type": "object", "description": "corpo JSON do pedido, conforme a documentação do modelo"},
         "trabalho": {"type": "string", "description": "card do Jira ou demanda (SCC-1762, D-0012), se houver"}}}},
    {"name": "status", "description": "Andamento de uma geração (queued, in_progress, completed, failed). Com esperar_segundos, "
     "espera até terminar (máx. 600). Quando completed, devolve as URLs do resultado.",
     "inputSchema": {"type": "object", "required": ["ferramenta", "request_id"], "properties": {
         "ferramenta": {"type": "string"}, "request_id": {"type": "string"},
         "esperar_segundos": {"type": "integer", "minimum": 0, "maximum": 600}}}},
    {"name": "texto", "description": "Manda um texto LONGO para um modelo barato (OpenRouter, conta da empresa, centavos; não gasta o seu "
     "limite do Claude) e devolve só a resposta. Use para o trabalho braçal de leitura: triagem de momentos numa transcrição de episódio, "
     "resumos, listas de títulos, checar legenda. Passe o CAMINHO do arquivo (o conector lê aqui na máquina): nunca cole o texto inteiro na "
     "chamada. A decisão final (quais cortes, revisão) continua com você. Modelos: anthropic/claude-haiku-5.5 (padrão, bom e barato), "
     "anthropic/claude-sonnet-5.5 (mais caprichado, ~20x mais caro), google/gemini-3.7-flash, google/gemini-3.1-flash-lite (o mais barato).",
     "inputSchema": {"type": "object", "required": ["instrucao"], "properties": {
         "instrucao": {"type": "string", "description": "o que o modelo deve fazer com o texto e em que formato responder"},
         "arquivos": {"type": "array", "items": {"type": "string"}, "description": "caminhos de arquivos de texto (.txt .json .srt .vtt .md .csv), até ~3 MB no total"},
         "texto": {"type": "string", "description": "texto curto opcional (para longo, use arquivos)"},
         "modelo": {"type": "string", "description": "padrão: anthropic/claude-haiku-5.5"},
         "salvar_em": {"type": "string", "description": "opcional: grava a resposta neste arquivo e devolve só um resumo (bom para respostas longas)"},
         "trabalho": {"type": "string", "description": "card do Jira ou demanda (SCC-1762, D-0012), se houver"}}}},
    {"name": "baixar", "description": "Baixa o resultado de uma geração (URL https) para a pasta atual do trabalho. Os arquivos "
     "ficam na API só por ~7 dias: baixe o que for usar.",
     "inputSchema": {"type": "object", "required": ["url"], "properties": {
         "url": {"type": "string"}, "nome": {"type": "string", "description": "nome do arquivo (opcional)"}}}},
]


def chamar(corpo, timeout=60):
    cfg = central.config()
    if not central.configurada(cfg):
        raise RuntimeError("este Claude não está conectado à central (peça o token ao gestor: \"me conecta na central com o token cw_...\")")
    req = urllib.request.Request(cfg["url"] + "/functions/v1/ferramentas", data=json.dumps(corpo).encode(), method="POST",
                                 headers={"apikey": cfg["chave"], "Authorization": "Bearer " + cfg["chave"],
                                          "x-cw-token": cfg["token"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read())
        except ValueError:
            j = {}
        raise RuntimeError(j.get("erro") or "central respondeu {}".format(e.code)) from None


TEXTO_OK = re.compile(r"\.(txt|json|srt|vtt|md|csv|tsv|xml)$", re.I)
SENSIVEL = re.compile(r"(^|[/\\])(\.ssh|\.aws|\.gnupg|\.config|Keychains|\.netrc|\.env[^/\\]*|[^/\\]*(credential|secret|token|senha)[^/\\]*|central\.json)([/\\]|$)", re.I)


def ler_arquivos(caminhos):
    """Lê os textos aqui na máquina (o Claude só passa o caminho). Recusa arquivos que não são texto ou que parecem guardar senha/chave."""
    partes, total = [], 0
    for c in caminhos or []:
        p = Path(os.path.expanduser(str(c))).resolve()
        if SENSIVEL.search(str(p)) or not TEXTO_OK.search(p.name):
            raise RuntimeError("não envio esse arquivo: {} (só texto de trabalho: .txt .json .srt .vtt .md .csv)".format(p.name))
        if not p.is_file():
            raise RuntimeError("arquivo não encontrado: {}".format(p))
        t = p.read_text(encoding="utf-8", errors="replace")
        total += len(t)
        if total > 3_000_000:
            raise RuntimeError("textos grandes demais (máx. ~3 MB no total)")
        partes.append("=== {} ===\n{}".format(p.name, t))
    return "\n\n".join(partes)


def executar(nome, args):
    if nome == "listar":
        return chamar({"acao": "listar"})
    if nome == "gerar":
        return chamar({"acao": "gerar", "ferramenta": args.get("ferramenta"), "caminho": args.get("caminho"),
                       "corpo": args.get("parametros") or {}, "trabalho": args.get("trabalho")}, timeout=120)
    if nome == "status":
        fim = time.time() + min(int(args.get("esperar_segundos") or 0), 600)
        while True:
            r = chamar({"acao": "status", "ferramenta": args.get("ferramenta"), "request_id": args.get("request_id")})
            if r.get("status") in TERMINAIS or time.time() >= fim:
                return r
            time.sleep(8)
    if nome == "texto":
        corpo = "\n\n".join(x for x in (args.get("texto") or "", ler_arquivos(args.get("arquivos"))) if x)
        r = chamar({"acao": "texto", "ferramenta": "openrouter", "modelo": args.get("modelo") or "anthropic/claude-haiku-5.5",
                    "instrucao": args.get("instrucao") or "", "texto": corpo, "trabalho": args.get("trabalho")}, timeout=300)
        if args.get("salvar_em") and r.get("texto"):
            destino = Path(os.path.expanduser(args["salvar_em"]))
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_text(r["texto"], encoding="utf-8")
            r["texto"] = "(resposta gravada em {}; {} caracteres) {}".format(destino, len(r["texto"]), r["texto"][:600])
        return r
    if nome == "baixar":
        url = str(args.get("url") or "")
        if not url.startswith("https://"):
            raise RuntimeError("só baixo URLs https")
        nome_arq = re.sub(r"[^\w.\-]+", "_", args.get("nome") or url.split("?")[0].rstrip("/").split("/")[-1] or "resultado")[:120]
        destino = Path.cwd() / nome_arq
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "cw-studios"}), timeout=600) as r, open(destino, "wb") as f:
            total = 0
            while True:
                bloco = r.read(4 << 20)
                if not bloco:
                    break
                total += len(bloco)
                if total > 2_000_000_000:
                    raise RuntimeError("arquivo grande demais")
                f.write(bloco)
        return {"arquivo": str(destino), "bytes": total}
    raise RuntimeError("ferramenta desconhecida: " + nome)


def responder(mid, resultado=None, erro=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if erro:
        msg["error"] = erro
    else:
        msg["result"] = resultado
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for linha in sys.stdin:
        try:
            m = json.loads(linha)
        except ValueError:
            continue
        metodo, mid, params = m.get("method"), m.get("id"), m.get("params") or {}
        if mid is None:          # notificação (ex.: notifications/initialized)
            continue
        if metodo == "initialize":
            responder(mid, {"protocolVersion": params.get("protocolVersion") or "2025-06-18", "capabilities": {"tools": {}},
                            "serverInfo": {"name": "cw-studios", "version": "1"}})
        elif metodo == "tools/list":
            responder(mid, {"tools": FERRAMENTAS})
        elif metodo == "tools/call":
            try:
                saida = executar(params.get("name"), params.get("arguments") or {})
                responder(mid, {"content": [{"type": "text", "text": json.dumps(saida, ensure_ascii=False, indent=1)}]})
            except Exception as e:
                responder(mid, {"content": [{"type": "text", "text": "Erro: {}".format(e)}], "isError": True})
        elif metodo == "ping":
            responder(mid, {})
        else:
            responder(mid, erro={"code": -32601, "message": "método não suportado: {}".format(metodo)})


if __name__ == "__main__":
    main()
