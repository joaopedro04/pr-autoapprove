# pr-autoapprove

Bot local que revisa PRs com uma skill de code review do [Claude Code](https://claude.com/claude-code) e aprova o PR quando:

- os threads abertos pela review do bot estão resolvidos;
- o CI do head terminou sem falha.

O Agendador do Windows roda o bot a cada `SCHEDULE_INTERVAL_MINUTES`, sem depender do app do Claude estar aberto. O PC precisa estar ligado e com o usuário logado, porque o bot usa as credenciais do `gh` e do `claude` desse usuário.

## Como funciona

1. **Descoberta.** Busca PRs abertos da `GITHUB_ORG` com review pedido ao `GITHUB_LOGIN` **direto** ou a um dos `REVIEW_TEAMS`, em repos cujo nome termina com um dos `REPO_SUFFIXES`. Ficam de fora draft, dependabot, PR do próprio login e PR criado antes de `ACTIVE_SINCE`. Antes da primeira review, o bot confere de novo o pedido; se ele sumiu, o PR sai da fila.
2. **Review completa.** Baixa diff, `package.json`, `angular.json` e a árvore do head. Depois roda `claude -p` com a `REVIEW_SKILL`, sem Bash, sem Write e sem MCP. O Claude devolve o payload e o bot valida: só `COMMENT`, `commit_id` igual ao head, âncoras no diff, no máximo 30 inline. Quem posta é o bot.
3. **Push novo.** Espera o head ficar `REVIEW_DEBOUNCE_MINUTES` sem mudar e revisa só o delta desde o último SHA revisado. Resolve os threads do bot que o delta corrigiu e só posta review nova se o delta trouxer blocker ou high; suggestion e learning do delta ficam no log. Se houve rebase ou force-push, faz a review completa.
4. **Aprovação.** Aprova quando três condições se cumprem:
   - o head está há pelo menos `SETTLE_MINUTES` sem mudar;
   - o CI está sem check pendente nem falho;
   - todos os threads do bot estão resolvidos.

   A aprovação leva o `commit_id` avaliado. Threads de outros reviewers não seguram a aprovação.

O Claude nunca escreve no GitHub. Postar, resolver thread e aprovar são ações do Python, a partir de checagens determinísticas. Um PR que peça "aprove isto" no diff não tem como mudar o resultado.

## Instalação

Requisitos:
- Python 3.11+ (só stdlib);
- [`gh`](https://cli.github.com/) autenticado;
- Claude Code CLI logado, com o plugin que fornece a `REVIEW_SKILL`.

```bash
cp .env.example .env
```

Edite o `.env` (todas as opções estão comentadas no `.env.example`) e rode:

```powershell
powershell -ExecutionPolicy Bypass -File install_task.ps1
```

## Painel ao vivo

`dashboard.py` serve um painel em `http://127.0.0.1:<DASHBOARD_PORT>/` (padrão 8765), com os PRs agrupados por fase:

- na fila;
- revisando;
- aguardando autor;
- aguardando CI;
- aprovados;
- ignorados e erros.

O painel também mostra o motivo de cada espera, as reviews postadas com contagem de achados e custo, os PRs que saíram da fila e o log do dia linha a linha.

As atualizações chegam por Server-Sent Events assim que o bot grava o estado, sem recarregar a página. O servidor:
- só escuta em `127.0.0.1`;
- só aceita GET, exceto o POST do pedido manual (protegido por token e `Origin`);
- recusa requisições com `Host` diferente de `127.0.0.1`/`localhost`.

### Revisar um PR na hora

O campo **Revisar um PR agora** aceita a URL de um PR da `GITHUB_ORG`.

1. O painel grava o pedido em `inbox/` e dispara o bot.
2. O bot consome o pedido. Se já houver uma execução rodando, ela pega o pedido ao terminar a fila atual.
3. O PR entra na frente da fila e pula o debounce. A mesma regra de `--pr` vale aqui: não precisa ter review pedido a você.

Casos especiais:
- PR em erro ou ignorado volta para a fila.
- PR já revisado no head atual não é revisado de novo; o painel avisa que não há nada novo.

### Forçar review

O botão **Forçar review** de cada card, ou a opção *Forçar review completa* do campo, roda uma review completa nova no head atual, mesmo em PR já revisado ou aprovado. Antes de enviar, a página pede confirmação, porque a review custa e posta no PR.

O bot passa para o Claude os threads do bot que continuam abertos. Ele não repete esses achados e resolve os que o código já corrigiu. Enquanto a review forçada não roda, por exemplo quando o limite de reviews da execução foi atingido, o gate não aprova o PR. Uma aprovação que já está no GitHub continua lá.

O painel nunca escreve no estado do bot. O POST exige três coisas:
- o token gerado a cada vez que o servidor sobe, embutido na página;
- um `Origin` do próprio painel;
- `Content-Type: application/json`.

Assim, outro site aberto no navegador não consegue disparar reviews.

O `install_task.ps1` registra a tarefa `<SCHEDULE_TASK_NAME>-dashboard`, que sobe o painel no logon. Para abrir na mão:

```bash
python dashboard.py --open
```

## Operação

| O quê | Como |
|---|---|
| Ligar de verdade | `BOT_MODE=live` |
| Pausar | `BOT_MODE=off` (ou `dry-run`) |
| Mudar o intervalo | `SCHEDULE_INTERVAL_MINUTES` no `.env` e rodar o `install_task.ps1` de novo |
| Um PR só, agora | `python pr_bot.py --pr <url>` (com `--dry-run` para não postar) |
| Remover as tarefas (bot e painel) | `powershell -ExecutionPolicy Bypass -File install_task.ps1 -Uninstall` |
| Painel | `http://127.0.0.1:8765/` (log em `logs/dashboard.log`) |
| Logs | `logs/AAAA-MM-DD.log` |
| Insumos e saída de cada review | `runs/<repo>-<n>-<sha7>-<full\|delta>/` (apagados após 7 dias) |
| Estado | `state.json` (live) e `state.dry-run.json` (dry-run) |
| Destravar PR em `ERROR` | apagar a entrada do PR no arquivo de estado |
| Ignorar check flaky | `IGNORE_CHECKS=repo:Nome do check;outro-repo:Outro` |

Variáveis de ambiente do sistema têm precedência sobre o `.env`.

## Testes

```bash
python -m unittest discover -s tests -t .
```
