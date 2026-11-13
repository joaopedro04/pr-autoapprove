#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Uso: bash install.sh [--uninstall] [--interval N] [--dry-run] [--platform linux|macos]

  --uninstall         remove o timer/agente do bot e o do painel
  --interval N        minutos entre execuções do bot (padrão: SCHEDULE_INTERVAL_MINUTES do .env, ou 5)
  --dry-run           mostra os arquivos e comandos sem gravar nem executar nada
  --platform linux|macos
                      força o sistema (padrão: detectado por uname); útil com --dry-run
EOF
}

die() {
  echo "install.sh: $*" >&2
  exit 1
}

usage_error() {
  echo "install.sh: $*" >&2
  usage
  exit 2
}

BOT_DIR=$(cd "$(dirname "$0")" && pwd -P)
ENV_FILE=$BOT_DIR/.env

env_value() {
  local v first last
  [ -f "$ENV_FILE" ] || return 0
  v=$(tr -d '\r' < "$ENV_FILE" \
    | LC_ALL=C sed -e $'1s/^\xef\xbb\xbf//' \
    | sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" \
    | tail -n 1 \
    | sed -e 's/[[:space:]]*$//')
  if [ "${#v}" -ge 2 ]; then
    first=${v:0:1}
    last=${v: -1}
    if [ "$first" = "$last" ] && { [ "$first" = '"' ] || [ "$first" = "'" ]; }; then
      v=${v:1:${#v}-2}
    fi
  fi
  printf '%s\n' "$v"
}

UNINSTALL=0
DRY_RUN=0
INTERVAL_SET=0
INTERVAL_ARG=
PLATFORM=

while [ $# -gt 0 ]; do
  case $1 in
    --uninstall) UNINSTALL=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --interval)
      [ $# -ge 2 ] || usage_error "--interval exige um valor"
      INTERVAL_SET=1
      INTERVAL_ARG=$2
      shift
      ;;
    --platform)
      [ $# -ge 2 ] || usage_error "--platform exige linux ou macos"
      PLATFORM=$2
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *) usage_error "opção desconhecida: $1" ;;
  esac
  shift
done

case $PLATFORM in
  '' | linux | macos) ;;
  *) usage_error "--platform deve ser linux ou macos" ;;
esac

if [ -z "$PLATFORM" ]; then
  case $(uname -s) in
    Linux) PLATFORM=linux ;;
    Darwin) PLATFORM=macos ;;
    *) die "sistema não suportado ($(uname -s)); no Windows use install_task.ps1" ;;
  esac
fi

NUM_RE='^[0-9]{1,6}$'
NAME_RE='^[A-Za-z0-9_.][A-Za-z0-9_.-]*$'

positive_int() {
  [[ $2 =~ $NUM_RE ]] || die "$1 deve ser um inteiro maior que zero (recebido: $2)"
  [ $((10#$2)) -gt 0 ] || die "$1 deve ser um inteiro maior que zero (recebido: $2)"
  echo $((10#$2))
}

TASK_NAME=$(env_value SCHEDULE_TASK_NAME)
TASK_NAME=${TASK_NAME:-pr-autoapprove}
[[ $TASK_NAME =~ $NAME_RE ]] || die "SCHEDULE_TASK_NAME inválido: use só letras, números, _, . e -, sem - no início (recebido: $TASK_NAME)"

if [ "$INTERVAL_SET" = 1 ]; then positive_int "--interval" "$INTERVAL_ARG" > /dev/null; fi

show_cmd() {
  local line='$' arg
  for arg in "$@"; do
    line="$line $(printf '%q' "$arg")"
  done
  printf '%s\n' "$line"
}

run() {
  if [ "$DRY_RUN" = 1 ]; then show_cmd "$@"; else "$@"; fi
}

run_soft() {
  if [ "$DRY_RUN" = 1 ]; then show_cmd "$@"; else "$@" > /dev/null 2>&1 || true; fi
}

write_file() {
  local path=$1
  shift
  if [ "$DRY_RUN" = 1 ]; then
    printf '==> %s <==\n' "$path"
    "$@"
  else
    mkdir -p "$(dirname "$path")"
    "$@" > "$path"
  fi
}

remove_file() {
  local had=0
  if [ -e "$1" ]; then had=1; fi
  run rm -f "$1"
  if [ "$DRY_RUN" != 1 ] && [ "$had" = 1 ]; then echo "Removido: $1"; fi
}

spec_escape() { printf '%s' "$1" | sed -e 's/%/%%/g'; }
quote_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/%/%%/g'; }
exec_escape() { quote_escape "$1" | sed -e 's/\$/$$/g'; }
xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g' -e "s/'/\&#39;/g"; }
shell_quote() { printf "'%s'" "$(printf '%s' "$1" | sed -e "s/'/'\\\\''/g")"; }

DASH_NAME=$TASK_NAME-dashboard

linux_unit_dir() { echo "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"; }

render_bot_service() {
  cat <<EOF
[Unit]
Description=Revisa PRs com uma skill do Claude Code e aprova quando os threads do bot estão resolvidos e o CI está verde.

[Service]
Type=oneshot
WorkingDirectory=$(spec_escape "$BOT_DIR")
Environment="PATH=$(quote_escape "$PATH")"
ExecStart="$(exec_escape "$PYTHON")" "$(exec_escape "$BOT_DIR/pr_bot.py")"
TimeoutStartSec=2h
EOF
}

render_timer() {
  cat <<EOF
[Unit]
Description=Agenda o $TASK_NAME a cada $INTERVAL min.

[Timer]
OnBootSec=1min
AccuracySec=10s
OnUnitActiveSec=${INTERVAL}min
Unit=$TASK_NAME.service

[Install]
WantedBy=timers.target
EOF
}

render_dashboard_service() {
  cat <<EOF
[Unit]
Description=Painel ao vivo do pr-autoapprove em 127.0.0.1.

[Service]
WorkingDirectory=$(spec_escape "$BOT_DIR")
Environment="PATH=$(quote_escape "$PATH")"
ExecStart="$(exec_escape "$PYTHON")" "$(exec_escape "$BOT_DIR/dashboard.py")"
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
EOF
}

render_plist() {
  local label=$1 script=$2 schedule=$3
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$(xml_escape "$label")</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$PYTHON")</string>
    <string>$(xml_escape "$BOT_DIR/$script")</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$(xml_escape "$BOT_DIR")</string>
$schedule
  <key>RunAtLoad</key>
  <true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$(xml_escape "$PATH")</string>
  </dict>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
EOF
}

render_bot_plist() {
  render_plist "$TASK_NAME" pr_bot.py "$(printf '  <key>StartInterval</key>\n  <integer>%s</integer>' $((INTERVAL * 60)))"
}

render_dashboard_plist() {
  render_plist "$DASH_NAME" dashboard.py "$(printf '  <key>KeepAlive</key>\n  <true/>')"
}

uninstall_linux() {
  local dir
  dir=$(linux_unit_dir)
  run_soft systemctl --user disable --now "$TASK_NAME.timer"
  run_soft systemctl --user stop "$TASK_NAME.service"
  run_soft systemctl --user disable --now "$DASH_NAME.service"
  remove_file "$dir/$TASK_NAME.timer"
  remove_file "$dir/$TASK_NAME.service"
  remove_file "$dir/$DASH_NAME.service"
  run_soft systemctl --user daemon-reload
  run_soft systemctl --user reset-failed "$TASK_NAME.service" "$TASK_NAME.timer" "$DASH_NAME.service"
}

uninstall_macos() {
  local dir=$HOME/Library/LaunchAgents uid
  uid=$(id -u)
  run_soft launchctl bootout "gui/$uid/$TASK_NAME"
  run_soft launchctl bootout "gui/$uid/$DASH_NAME"
  remove_file "$dir/$TASK_NAME.plist"
  remove_file "$dir/$DASH_NAME.plist"
}

resolve_python() {
  local py=$1 found
  if [ -z "$py" ]; then
    py=$(command -v python3 || true)
    [ -n "$py" ] || die "python3 não encontrado no PATH; defina PYTHON_PATH no .env"
  fi
  case $py in
    "~/"*) py=$HOME/${py:2} ;;
    /*) ;;
    */*) py=$BOT_DIR/$py ;;
    *)
      found=$(command -v "$py" || true)
      [ -n "$found" ] || die "PYTHON_PATH não encontrado no PATH: $py"
      py=$found
      ;;
  esac
  [ "${py#/}" != "$py" ] || py=$PWD/$py
  [ -f "$py" ] && [ -x "$py" ] || die "python não encontrado ou não executável: $py (ajuste PYTHON_PATH no .env)"
  "$py" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || die "$py não é Python 3.11+; defina PYTHON_PATH no .env"
  echo "$py"
}

cron_escape() { printf '%s' "$1" | sed -e 's/%/\\%/g'; }

cron_fallback() {
  local every exact=1 dir py
  if [ "$INTERVAL" -lt 60 ]; then
    every="*/$INTERVAL * * * *"
    [ $((60 % INTERVAL)) -eq 0 ] || exact=0
  else
    every="0 */$((INTERVAL / 60)) * * *"
    if [ $((INTERVAL % 60)) -ne 0 ] || [ $((24 % (INTERVAL / 60))) -ne 0 ]; then exact=0; fi
  fi
  dir=$(cron_escape "$(shell_quote "$BOT_DIR")")
  py=$(cron_escape "$(shell_quote "$PYTHON")")
  echo "install.sh: systemd de usuário indisponível (systemctl --user falhou); nada foi instalado." >&2
  echo "Linhas de cron aproximadas (crontab -e):" >&2
  if [ "$exact" = 0 ]; then echo "Aviso: o cron não repete a cada $INTERVAL min exatos; ajuste a linha se precisar." >&2; fi
  echo "PATH=$PATH"
  echo "$every cd $dir && $py pr_bot.py >/dev/null 2>&1"
  echo "@reboot cd $dir && $py dashboard.py >/dev/null 2>&1"
  exit 1
}

install_linux() {
  local dir user linger
  dir=$(linux_unit_dir)
  if [ "$DRY_RUN" != 1 ]; then
    if ! command -v systemctl > /dev/null 2>&1 || ! systemctl --user show-environment > /dev/null 2>&1; then
      cron_fallback
    fi
  fi
  write_file "$dir/$TASK_NAME.service" render_bot_service
  write_file "$dir/$TASK_NAME.timer" render_timer
  write_file "$dir/$DASH_NAME.service" render_dashboard_service
  run systemctl --user daemon-reload
  run systemctl --user enable --now "$TASK_NAME.timer"
  run systemctl --user enable "$DASH_NAME.service"
  run systemctl --user restart "$DASH_NAME.service"
  [ "$DRY_RUN" != 1 ] || return 0
  user=${USER:-$(id -un)}
  linger=$(loginctl show-user "$user" -p Linger 2> /dev/null || true)
  if [ "$linger" != "Linger=yes" ]; then
    echo "Dica: sem linger o timer só roda com a sua sessão aberta. Para rodar sempre: loginctl enable-linger $user"
  fi
}

wait_booted_out() {
  local i=0
  [ "$DRY_RUN" != 1 ] || return 0
  while [ "$i" -lt 10 ] && launchctl print "$1" > /dev/null 2>&1; do
    sleep 0.5
    i=$((i + 1))
  done
  if launchctl print "$1" > /dev/null 2>&1; then
    die "o launchd ainda não descarregou $1; rode bash install.sh de novo em alguns segundos"
  fi
}

install_macos() {
  local dir=$HOME/Library/LaunchAgents uid
  uid=$(id -u)
  write_file "$dir/$TASK_NAME.plist" render_bot_plist
  write_file "$dir/$DASH_NAME.plist" render_dashboard_plist
  run_soft launchctl bootout "gui/$uid/$TASK_NAME"
  wait_booted_out "gui/$uid/$TASK_NAME"
  run launchctl bootstrap "gui/$uid" "$dir/$TASK_NAME.plist"
  run_soft launchctl bootout "gui/$uid/$DASH_NAME"
  wait_booted_out "gui/$uid/$DASH_NAME"
  run launchctl bootstrap "gui/$uid" "$dir/$DASH_NAME.plist"
}

if [ "$UNINSTALL" = 1 ]; then
  "uninstall_$PLATFORM"
  exit 0
fi

if [ "$INTERVAL_SET" = 1 ]; then
  INTERVAL=$(positive_int "--interval" "$INTERVAL_ARG")
else
  INTERVAL=$(env_value SCHEDULE_INTERVAL_MINUTES)
  INTERVAL=$(positive_int "SCHEDULE_INTERVAL_MINUTES" "${INTERVAL:-5}")
fi
PORT=$(env_value DASHBOARD_PORT)
PORT=${PORT:-8765}
PYTHON=$(resolve_python "$(env_value PYTHON_PATH)")

if [ "$DRY_RUN" != 1 ]; then
  [ -f "$BOT_DIR/pr_bot.py" ] && [ -f "$BOT_DIR/dashboard.py" ] || die "pr_bot.py e dashboard.py devem estar em $BOT_DIR"
fi

"install_$PLATFORM"

if [ "$DRY_RUN" != 1 ]; then
  echo "Tarefa $TASK_NAME a cada $INTERVAL min, com $PYTHON"
  echo "Painel ao vivo: http://127.0.0.1:$PORT/"
fi
