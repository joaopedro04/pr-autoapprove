# pr-autoapprove

Bot local que revisa PRs com uma skill de code review do [Claude Code](https://claude.com/claude-code) e aprova o PR quando:

- os threads abertos pela review do bot estão resolvidos;
- o CI do head terminou sem falha.

O agendador do sistema (Agendador do Windows, systemd no Linux ou launchd no macOS) roda o bot a cada `SCHEDULE_INTERVAL_MINUTES`, sem depender do app do Claude estar aberto. A máquina precisa estar ligada e, em geral, com o usuário logado, porque o bot usa as credenciais do `gh` e do `claude` desse usuário. No Linux, dá para rodar sem sessão aberta (veja a instalação no Linux).

## Como funciona

1. **Descoberta.** Busca PRs abertos da `GITHUB_ORG` com review pedido ao `GITHUB_LOGIN` **direto** ou a um dos `REVIEW_TEAMS`, em repos cujo nome termina com um dos `REPO_SUFFIXES`. Ficam de fora draft, dependabot, PR do próprio login e PR criado antes de `ACTIVE_SINCE`. Antes da primeira review, o bot confere de novo o pedido; se ele sumiu, o PR sai da fila.
2. **Review completa.** Baixa diff, `package.json`, `angular.json` e a árvore do head. Depois roda `claude -p` com a `REVIEW_SKILL`, sem Bash, sem Write e sem MCP. O Claude devolve o payload e o bot valida: só `COMMENT`, `commit_id` igual ao head, âncoras no diff, no máximo 30 inline. Quem posta é o bot.

   Até `REVIEW_CONCURRENCY` reviews rodam em paralelo, com teto de `MAX_REVIEWS_PER_RUN` por execução. Só os `claude -p` rodam em threads; ler o GitHub, postar, resolver thread, aprovar e salvar o estado acontecem numa linha só. Cada review é preparada quando abre uma vaga, com o head mais recente. Se o head mudar durante a review, o bot não roda o gate naquele momento e deixa o delta para a próxima execução.
3. **Push novo.** Espera o head ficar `REVIEW_DEBOUNCE_MINUTES` sem mudar e revisa só o delta desde o último SHA revisado. Resolve os threads do bot que o delta corrigiu e só posta review nova se o delta trouxer blocker ou high; suggestion e learning do delta ficam no log. Se houve rebase ou force-push, faz a review completa.
4. **Labels.** Logo depois da primeira review do bot num PR que entrou na fila, o bot aplica uma vez as labels de `PR_LABELS`. Assim, uma automação disparada pela label já encontra os comentários da review. PR ignorado não recebe label, e uma falha na aplicação é tentada de novo nas execuções seguintes.
   - O formato é uma lista com vírgula ou um array JSON. No JSON, cada item pode trazer cor e descrição.
   - Se a label não existe no repo, o bot cria com essa cor e descrição.
   - Se o autor tirar a label depois, ela não volta.
   - Falha ao aplicar não trava a review: o bot tenta de novo até 3 vezes e registra o erro.
   - PRs que já estavam na fila quando as labels foram configuradas não recebem a label.
5. **Aprovação.** Aprova quando três condições se cumprem:
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

Edite o `.env` (todas as opções estão comentadas no `.env.example`) e rode o instalador do seu sistema. Ele registra o bot e o painel no agendador. Para ver os arquivos e comandos antes de instalar, use `bash install.sh --dry-run` (Linux e macOS).

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File install_task.ps1
```

### Linux

```bash
bash install.sh
```

O script cria três unidades do systemd de usuário em `~/.config/systemd/user/`:
- `<SCHEDULE_TASK_NAME>.service` e `.timer`, que rodam o bot a cada `SCHEDULE_INTERVAL_MINUTES`;
- `<SCHEDULE_TASK_NAME>-dashboard.service`, que mantém o painel no ar.

Sem linger, o timer só roda com a sua sessão aberta. Com `loginctl enable-linger $USER`, ele roda também sem login, desde que o token do `gh` esteja em arquivo (`gh auth login --insecure-storage`). No keyring (Secret Service), o token fica trancado sem sessão.

Se o systemd de usuário não estiver disponível, o script não instala nada e mostra as linhas de cron aproximadas, com o `PATH` atual. A linha `PATH=` vale para as entradas abaixo dela no crontab. O cron só repete a intervalos exatos quando N divide 60, ou quando N é múltiplo de 60 e N/60 divide 24; nos outros casos o script avisa.

### macOS

```bash
bash install.sh
```

O script cria dois agentes do launchd em `~/Library/LaunchAgents/`: `<SCHEDULE_TASK_NAME>.plist` (o bot) e `<SCHEDULE_TASK_NAME>-dashboard.plist` (o painel).

Reinstalar no macOS interrompe uma execução em andamento, porque o `bootout` mata o job.

Não deixe o repo em `~/Documents`, `~/Desktop` ou `~/Downloads`: o launchd esbarra na proteção de privacidade (TCC) e o job falha com `Operation not permitted`. Os agentes aparecem em Ajustes do Sistema > Geral > Itens de Início.

Precisa de `python3` 3.11+. O `/usr/bin/python3` do Xcode pode ser mais antigo. Nesse caso, instale um mais novo e aponte o `PYTHON_PATH` no `.env`.

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
- por padrão, só escuta em `127.0.0.1` e recusa requisições com `Host` diferente de `127.0.0.1`/`localhost`;
- só aceita GET, exceto o POST do pedido manual (protegido por token e `Origin`).

Para abrir na rede (por exemplo, para olhar do celular):
1. No `.env`, coloque `DASHBOARD_BIND=0.0.0.0` e uma `DASHBOARD_ACCESS_KEY` de 16+ caracteres. Sem a chave, o painel continua só local.
2. Reinicie o painel: rode o instalador de novo (ele reinicia o painel) ou reinicie a tarefa, a unidade ou o agente dele.
3. Abra `http://<ip>:<porta>/?key=<chave>` uma vez. A página guarda a chave num cookie (`HttpOnly`, `SameSite=Strict`) e tira a chave do endereço.
4. Quem não tem a chave recebe 401.

Na própria máquina nada muda, e os links de rede aparecem no topo do painel. O firewall da máquina precisa liberar a porta. Um exemplo no Windows, num PowerShell como administrador:

```powershell
New-NetFirewallRule -DisplayName "pr-autoapprove painel" -Direction Inbound -Protocol TCP -LocalPort 8765 -RemoteAddress LocalSubnet -Program (Get-Command pythonw.exe).Source -Action Allow -Profile Any
```

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

O `install_task.ps1` e o `install.sh` registram `<SCHEDULE_TASK_NAME>-dashboard`, que sobe o painel no logon. Para abrir na mão:

```bash
python dashboard.py --open
```

## Operação

| O quê | Como |
|---|---|
| Ligar de verdade | `BOT_MODE=live` |
| Pausar | `BOT_MODE=off` (ou `dry-run`) |
| Mudar o intervalo | `SCHEDULE_INTERVAL_MINUTES` no `.env` e rodar o instalador de novo: `install_task.ps1` (Windows) ou `bash install.sh` (Linux/macOS) |
| Um PR só, agora | `python pr_bot.py --pr <url>` (com `--dry-run` para não postar) |
| Remover as tarefas (bot e painel) | Windows: `powershell -ExecutionPolicy Bypass -File install_task.ps1 -Uninstall`. Linux/macOS: `bash install.sh --uninstall` |
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
