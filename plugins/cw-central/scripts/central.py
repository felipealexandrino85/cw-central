#!/usr/bin/env python3
"""Central CW Studios: a fila de demandas de edição, vista da máquina do editor.

Só biblioteca padrão (python3 do sistema, 3.9+). Fala com o Supabase da central pelas funções cw_*
(ver central/supabase.sql no repositório do hub); cada editor se identifica com o token pessoal que
o gestor gerou na página da central.

Comandos:
  entrar   --token cw_...        grava o token deste editor e confere com a central
  quem                            mostra editor, central e se o token vale
  fila     [--json]               demandas que este editor pode pegar (as dele primeiro)
  pegar    [--id N] [--json]      pega a demanda N (ou a próxima da fila); imprime os dados para editar
  ver      --id N [--json]        detalhes e histórico de uma demanda
  etapa    --id N --etapa "..." [--registro ID]   atualiza o que está acontecendo (aparece no painel)
  entregar --id N --link URL [--pecas K] [--nota "..."]   manda para aprovação de quem pediu
  devolver --id N --motivo "..."  não vai dar para fazer: volta para a fila geral
  pulso                           manda o status.json do registro agora (o registro já faz isso sozinho)

Configuração (a primeira que existir vence):
  CW_CENTRAL_URL / CW_CENTRAL_CHAVE / CW_CENTRAL_TOKEN   (variáveis de ambiente)
  ~/.cw-cortes/central.json   {"url": ..., "chave": ..., "token": ...}   (token fica aqui, só seu)
  <plugin>/central.json       {"url": ..., "chave": ...}   (do time: URL e chave pública anon do Supabase)
Demandas aparecem como D-0001; nos comandos, --id aceita 12 ou D-0012.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HUB = Path(os.environ.get("CW_CORTES_HOME", Path.home() / ".cw-cortes"))
PESSOAL = HUB / "central.json"
DO_TIME = Path(__file__).resolve().parents[1] / "central.json"
TIMEOUT_S = 8


class ErroCentral(Exception):
    pass


def _ler(caminho):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def config():
    time_, pessoal = _ler(DO_TIME), _ler(PESSOAL)
    return {
        "url": (os.environ.get("CW_CENTRAL_URL") or pessoal.get("url") or time_.get("url") or "").rstrip("/"),
        "chave": os.environ.get("CW_CENTRAL_CHAVE") or pessoal.get("chave") or time_.get("chave") or "",
        "token": os.environ.get("CW_CENTRAL_TOKEN") or pessoal.get("token") or "",
        "jira_projetos": time_.get("jira_projetos") or ["SCC", "CONT"],
        "pasta_entrada": pessoal.get("pasta_entrada") or time_.get("pasta_entrada") or "",
        "jira": time_.get("jira") or "https://cardapio-web.atlassian.net",
    }


def configurada(cfg=None):
    cfg = cfg or config()
    return bool(cfg["url"] and cfg["chave"] and cfg["token"])


def rpc(funcao, timeout=TIMEOUT_S, **params):
    cfg = config()
    if not (cfg["url"] and cfg["chave"]):
        raise ErroCentral("central não configurada: falta {} (ou CW_CENTRAL_URL/CW_CENTRAL_CHAVE)".format(DO_TIME))
    if not cfg["token"]:
        raise ErroCentral("este editor ainda não entrou na central: rode 'central.py entrar --token cw_...' "
                          "com o token que o gestor gerou na página da central")
    corpo = json.dumps(dict(p_token=cfg["token"], **params)).encode()
    req = urllib.request.Request(
        "{}/rest/v1/rpc/{}".format(cfg["url"], funcao), data=corpo, method="POST",
        headers={"apikey": cfg["chave"], "Authorization": "Bearer " + cfg["chave"],
                 "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            texto = r.read().decode() or "null"
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("message")
        except (ValueError, AttributeError):
            msg = None
        raise ErroCentral(msg or "central respondeu HTTP {}".format(e.code))
    except (urllib.error.URLError, OSError) as e:
        raise ErroCentral("sem conexão com a central ({})".format(getattr(e, "reason", e)))
    return json.loads(texto)


def id_num(valor):
    m = re.fullmatch(r"(?:D-?)?0*(\d+)", str(valor).strip(), re.I)
    if not m:
        raise ErroCentral("id inválido: {} (use 12 ou D-0012)".format(valor))
    return int(m.group(1))


# ---------------------------------------------------------------- saída

STATUS = {"nova": "nova", "em_edicao": "em edição", "em_revisao": "esperando aprovação",
          "refazer": "REFAZER", "entregue": "entregue", "cancelada": "cancelada"}


def linha(d):
    extras = [d.get("tipo") or "", d.get("perfil") or ""]
    if d.get("prioridade") == "urgente":
        extras.insert(0, "URGENTE")
    if d.get("prazo"):
        extras.append("prazo " + d["prazo"])
    return "{}  [{}]  {}  ({})".format(d["codigo"], STATUS.get(d["status"], d["status"]), d["titulo"],
                                       ", ".join(x for x in extras if x))


def ficha(d):
    campos = [("Demanda", "{} · {}".format(d["codigo"], d["titulo"])), ("Status", STATUS.get(d["status"], d["status"])),
              ("Tipo", d.get("tipo")), ("Perfil/destino", d.get("perfil")), ("Prioridade", d.get("prioridade")),
              ("Prazo", d.get("prazo")), ("Pediu", "{} ({})".format(d.get("solicitante_nome") or "",
                                                                    d.get("solicitante_email"))),
              ("Área", d.get("area")), ("Bruto", d.get("link_bruto")), ("Roteiro", d.get("link_roteiro")),
              ("Rodadas de refazer", d.get("rodadas") or None), ("Etapa", d.get("etapa")),
              ("Entrega", d.get("link_entrega"))]
    out = ["{}: {}".format(k, v) for k, v in campos if v]
    if d.get("descricao"):
        out += ["", "Pedido:", d["descricao"]]
    eventos = d.get("eventos") or []
    if eventos:
        out += ["", "Histórico:"]
        out += ["  {}  {}  {}{}".format(e["ts"][:16].replace("T", " "), e["autor"], e["tipo"],
                                        ": " + e["detalhe"] if e.get("detalhe") else "") for e in eventos]
    return "\n".join(out)


def imprimir(dados, como_json, formato):
    if como_json:
        print(json.dumps(dados, ensure_ascii=False, indent=1))
    else:
        print(formato(dados))


# ---------------------------------------------------------------- comandos

def cmd_entrar(a):
    atual = _ler(PESSOAL)
    atual["token"] = a.token.strip()
    for campo in ("url", "chave"):
        if getattr(a, campo):
            atual[campo] = getattr(a, campo).strip()
    HUB.mkdir(parents=True, exist_ok=True)
    PESSOAL.write_text(json.dumps(atual, indent=1), encoding="utf-8")
    os.chmod(PESSOAL, 0o600)
    eu = rpc("cw_quem_sou")
    print("Conectado à central como {} ({}).".format(eu["nome"], eu["slug"]))
    try:  # primeiro sinal já aparece no painel, e o histórico que já existe nesta máquina sobe junto
        import registro
        registro.atualizar_status(os.environ.get("CLAUDE_CODE_SESSION_ID", ""), "sinal")
        print("Histórico enviado: {} vídeo(s).".format(registro.enviar_edicoes()))
    except Exception as e:
        print("Histórico não subiu agora ({}); sobe sozinho no próximo início ou fim de vídeo.".format(e))


def cmd_quem(_a):
    cfg = config()
    print("Central: {}".format(cfg["url"] or "(não configurada)"))
    print("Token: {}".format("configurado" if cfg["token"] else "(falta: central.py entrar --token ...)"))
    if configurada(cfg):
        eu = rpc("cw_quem_sou")
        print("Editor: {} ({})".format(eu["nome"], eu["slug"]))


def cmd_fila(a):
    fila = rpc("cw_fila") or []
    if a.json:
        imprimir(fila, True, None)
    elif not fila:
        print("Fila vazia: nenhuma demanda nova nem nada seu em aberto.")
    else:
        print("\n".join(linha(d) for d in fila))


def cmd_pegar(a):
    d = rpc("cw_pegar", p_id=id_num(a.id) if a.id else None)
    if not d:
        print("Nada para pegar: a fila está vazia.")
        return
    d = rpc("cw_ver", p_id=d["id"])
    imprimir(d, a.json, ficha)


def cmd_ver(a):
    imprimir(rpc("cw_ver", p_id=id_num(a.id)), a.json, ficha)


def cmd_etapa(a):
    rpc("cw_etapa", p_id=id_num(a.id), p_etapa=a.etapa, p_registro=a.registro)
    print("ok")


def cmd_entregar(a):
    rpc("cw_entregar", p_id=id_num(a.id), p_link=a.link, p_pecas=a.pecas, p_nota=a.nota)
    print("Enviada para aprovação de quem pediu.")


def cmd_devolver(a):
    rpc("cw_devolver", p_id=id_num(a.id), p_motivo=a.motivo)
    print("Devolvida para a fila geral.")


def enviar_status(status, timeout=3):
    """Chamado pelo registro.py a cada sinal de vida. Silencioso: nunca derruba o registro."""
    if not configurada():
        return False
    try:
        r = rpc("cw_pulso", timeout=timeout, p_status=status)
    except ErroCentral:
        return False
    descartar = (r or {}).get("descartar") if isinstance(r, dict) else None
    if descartar:   # a gestão marcou como "não é edição": fecha o registro local
        try:
            import registro
            abertos = registro.trabalhos_abertos()
            for tid in descartar:
                if tid.startswith("sessao-"):   # sessão sem vídeo aberto: marca a atividade dela como descartada
                    for arq in registro.LOCAL.glob("atividade-{}*.json".format(tid[7:])):
                        at = json.loads(arq.read_text())
                        at["descartada"] = True
                        arq.write_text(json.dumps(at, ensure_ascii=False))
                elif tid in abertos:
                    registro.gravar({"ev": "fim", "id": tid, "status": "nao_edicao", "pecas": 0, "saida": "",
                                     "nota": "marcado na central como não é edição", "sessao": ""})
        except Exception:
            pass
    return True


def cmd_pulso(_a):
    import registro
    st = registro.atualizar_status(os.environ.get("CLAUDE_CODE_SESSION_ID", ""), "sinal")
    rpc("cw_pulso", p_status=st)  # de novo, mas mostrando o erro se houver
    print("Status enviado à central.")


SEGREDO = re.compile(r"(cw_|sbp_|ATATT|sk-|ghp_|github_pat_|eyJ|AIza)[A-Za-z0-9_\-=.]{8,}")
TIPOS = ("reel", "cortes", "tutorial", "youtube", "anuncio", "story", "outro")


def cmd_sugerir_regras(a):
    """Manda à gestão regras de edição tiradas do CLAUDE.md do editor (JSON: [{"texto": ..., "tipo": "reel"|null}, ...])."""
    bruto = Path(os.path.expanduser(a.arquivo)).read_text(encoding="utf-8") if a.arquivo else sys.stdin.read()
    try:
        itens = json.loads(bruto)
    except ValueError:
        sys.exit("o arquivo precisa ser uma lista JSON: [{\"texto\": \"...\", \"tipo\": null}]")
    limpos = []
    for i in itens if isinstance(itens, list) else []:
        texto = SEGREDO.sub("[oculto]", str((i or {}).get("texto") or "")).strip()
        tipo = (i or {}).get("tipo")
        if len(texto) >= 5:
            limpos.append({"texto": texto[:400], "tipo": tipo if tipo in TIPOS else None})
    if not limpos:
        sys.exit("nenhuma regra para mandar")
    n = rpc("cw_regra_sugerir", p_itens=limpos[:60], p_origem=a.origem or "CLAUDE.md")
    print("Mandei {} sugestão(ões) para a gestão aprovar no Padrão do time ({} já existiam ou estavam repetidas).".format(n, len(limpos) - n))


def main():
    ap = argparse.ArgumentParser(description="Central CW Studios (fila de demandas)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("entrar")
    p.add_argument("--token", required=True)
    p.add_argument("--url")
    p.add_argument("--chave")
    sub.add_parser("quem")
    p = sub.add_parser("fila")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("pegar")
    p.add_argument("--id")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("ver")
    p.add_argument("--id", required=True)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("etapa")
    p.add_argument("--id", required=True)
    p.add_argument("--etapa", required=True)
    p.add_argument("--registro")
    p = sub.add_parser("entregar")
    p.add_argument("--id", required=True)
    p.add_argument("--link", required=True)
    p.add_argument("--pecas", type=int)
    p.add_argument("--nota")
    p = sub.add_parser("devolver")
    p.add_argument("--id", required=True)
    p.add_argument("--motivo", required=True)
    sub.add_parser("pulso")
    p = sub.add_parser("sugerir-regras")
    p.add_argument("--arquivo", help="JSON com as regras (sem isto, lê da entrada)")
    p.add_argument("--origem", help="de onde vieram, ex.: CLAUDE.md de Skill editor - CW Studio")
    a = ap.parse_args()
    {"entrar": cmd_entrar, "quem": cmd_quem, "fila": cmd_fila, "pegar": cmd_pegar, "ver": cmd_ver,
     "etapa": cmd_etapa, "entregar": cmd_entregar, "devolver": cmd_devolver, "pulso": cmd_pulso, "sugerir-regras": cmd_sugerir_regras}[a.cmd](a)


if __name__ == "__main__":
    try:
        main()
    except ErroCentral as e:
        sys.exit("central: {}".format(e))
