# cw-central

Plugin do Claude Code do **CW Studios (Cardápio Web)**. Liga o Claude do editor de vídeo à Central CW Studios:
mostra a etapa da edição ao vivo, mede o tempo ativo, registra as entregas, prepara o material da pasta do Drive e
traz as regras do time. Funciona com os agentes de edição de cada um.

## Instalar (uma vez por computador)
Precisa do Claude Code (aba **Code** do app do Claude, VS Code ou terminal) e de `python3` (já vem no macOS).

Numa sessão do Claude Code, digite:
```
/plugin marketplace add felipealexandrino85/cw-central
/plugin install cw-central@cw-studios
```
(ou no terminal: `claude plugin marketplace add felipealexandrino85/cw-central` e `claude plugin install cw-central@cw-studios`)

Abra uma sessão nova e diga: **"Me conecta na central com o token cw_…"** (o gestor gera o token na Central, aba Pessoas).

## Usar
Edite como sempre. Cite o card do Jira (`SCC-1762`) ou a demanda (`D-0012`) na conversa e, no fim, diga
**"entreguei"**. Para começar um vídeo com o material da pasta do Drive: **"vamos fazer o SCC-1762"**.

## Atualizar
`/plugin marketplace update cw-studios` e `/plugin update cw-central@cw-studios`.
