import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path

INSTALLER = Path(__file__).resolve().parent.parent / "install.sh"
HEADER = re.compile(r"^==> (.+) <==$")
BOT_DIR_NAME = 'a&b 50% $x "q" bot'
Result = namedtuple("Result", "rc out err files commands")


def parse_output(out):
    files, commands, current = {}, [], None
    for line in out.splitlines(keepends=True):
        header = HEADER.match(line.rstrip("\n"))
        if header:
            current = Path(header.group(1)).name
            files[current] = {"path": header.group(1), "lines": []}
        elif line.startswith("$ "):
            commands.append(line[2:].rstrip("\n"))
            current = None
        elif current is not None:
            files[current]["lines"].append(line)
    return {name: {"path": f["path"], "text": "".join(f["lines"])} for name, f in files.items()}, commands


def systemd_quote(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def exec_start(python, script):
    return 'ExecStart="{}" "{}"'.format(*(systemd_quote(v).replace("$", "$$") for v in (python, script)))


@unittest.skipIf(sys.platform == "win32" or shutil.which("bash") is None, "precisa de bash em Linux ou macOS")
class InstallScriptTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.root = root
        self.bot = root / BOT_DIR_NAME
        self.bot.mkdir()
        self.home = root / "home"
        self.home.mkdir()
        shutil.copy(INSTALLER, self.bot / "install.sh")
        self.real_bot = os.path.realpath(self.bot)
        self.unit_dir = self.home / ".config" / "systemd" / "user"
        self.agent_dir = self.home / "Library" / "LaunchAgents"

    def write_env(self, text=None, raw=None):
        if raw is None:
            if text is None:
                text = f"SCHEDULE_INTERVAL_MINUTES=12\nPYTHON_PATH={sys.executable}\n"
            raw = text.encode("utf-8")
        (self.bot / ".env").write_bytes(raw)

    def run_script(self, *args, path=None, home=None, extra_env=None):
        env = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
        env["HOME"] = str(home or self.home)
        env.update(extra_env or {})
        if path:
            env["PATH"] = path
        proc = subprocess.run(
            [shutil.which("bash"), str(self.bot / "install.sh"), *args],
            env=env, capture_output=True, text=True, timeout=60,
        )
        files, commands = parse_output(proc.stdout)
        return Result(proc.returncode, proc.stdout, proc.stderr, files, commands)

    def make_fakes(self):
        fake = self.root / "fake"
        fake.mkdir(exist_ok=True)
        self.log_path.touch()
        scripts = {
            "systemctl": (
                'echo "systemctl $*" >> "$FAKE_LOG"\n'
                '[ "$*" = "--user show-environment" ] && [ -n "$FAKE_NO_USER_MANAGER" ] && exit 1\n'
                "exit 0\n"
            ),
            "loginctl": 'echo "loginctl $*" >> "$FAKE_LOG"\necho "Linger=$FAKE_LINGER"\n',
            "launchctl": (
                'echo "launchctl $*" >> "$FAKE_LOG"\n'
                'case "$1" in\n'
                '  bootout) echo 2 > "$FAKE_STATE" ;;\n'
                "  print)\n"
                '    [ -n "$FAKE_PRINT_ALWAYS" ] && exit 0\n'
                '    n=$(cat "$FAKE_STATE" 2>/dev/null || echo 0)\n'
                '    if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$FAKE_STATE"; exit 0; fi\n'
                "    exit 1 ;;\n"
                "esac\n"
                "exit 0\n"
            ),
        }
        for name, body in scripts.items():
            script = fake / name
            script.write_text("#!/bin/sh\n" + body)
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return fake

    def fake_path(self):
        return f"{self.make_fakes()}:{os.environ['PATH']}"

    def run_real(self, *args, files=True, extra_env=None):
        if not (self.bot / ".env").exists():
            self.write_env()
        if files:
            for name in ("pr_bot.py", "dashboard.py"):
                (self.bot / name).touch()
        env = {"FAKE_LOG": str(self.log_path), "FAKE_STATE": str(self.root / "fake-state"), "FAKE_LINGER": "no"}
        env.update(extra_env or {})
        return self.run_script(*args, path=self.fake_path(), extra_env=env)

    @property
    def log_path(self):
        return self.root / "calls.log"

    def calls(self):
        return self.log_path.read_text().splitlines()

    def dry_run(self, platform, args, env, **kw):
        if env and not (self.bot / ".env").exists():
            self.write_env()
        return self.run_script("--dry-run", "--platform", platform, *args, **kw)

    def linux(self, *args, env=True, **kw):
        return self.dry_run("linux", args, env, **kw)

    def macos(self, *args, env=True, **kw):
        return self.dry_run("macos", args, env, **kw)

    def test_linux_generates_three_units_in_user_dir(self):
        res = self.linux()
        self.assertEqual(res.rc, 0, res.err)
        self.assertEqual(sorted(res.files), ["pr-autoapprove-dashboard.service", "pr-autoapprove.service", "pr-autoapprove.timer"])
        for name, item in res.files.items():
            self.assertEqual(item["path"], str(self.unit_dir / name))

    def test_linux_bot_service(self):
        res = self.linux()
        lines = res.files["pr-autoapprove.service"]["text"].splitlines()
        self.assertIn("Type=oneshot", lines)
        self.assertIn("TimeoutStartSec=2h", lines)
        self.assertIn(exec_start(sys.executable, f"{self.real_bot}/pr_bot.py"), lines)
        self.assertIn(f"WorkingDirectory={self.real_bot}".replace("%", "%%"), lines)
        self.assertIn(f'Environment="PATH={systemd_quote(os.environ["PATH"])}"', lines)

    def test_linux_timer_uses_env_interval(self):
        lines = self.linux().files["pr-autoapprove.timer"]["text"].splitlines()
        self.assertIn("OnUnitActiveSec=12min", lines)
        self.assertIn("OnBootSec=1min", lines)
        self.assertIn("AccuracySec=10s", lines)
        self.assertFalse([line for line in lines if line.startswith("OnActiveSec")])
        self.assertIn("Unit=pr-autoapprove.service", lines)
        self.assertIn("WantedBy=timers.target", lines)

    def test_interval_flag_overrides_env(self):
        self.assertIn("OnUnitActiveSec=7min", self.linux("--interval", "7").files["pr-autoapprove.timer"]["text"].splitlines())
        plist = plistlib.loads(self.macos("--interval", "7").files["pr-autoapprove.plist"]["text"].encode())
        self.assertEqual(plist["StartInterval"], 420)

    def test_linux_dashboard_service(self):
        lines = self.linux().files["pr-autoapprove-dashboard.service"]["text"].splitlines()
        self.assertIn("Restart=on-failure", lines)
        self.assertIn("RestartSec=60", lines)
        self.assertIn("WantedBy=default.target", lines)
        self.assertIn(exec_start(sys.executable, f"{self.real_bot}/dashboard.py"), lines)
        self.assertNotIn("Type=oneshot", lines)

    def test_linux_commands_enable_timer_and_restart_dashboard(self):
        commands = self.linux().commands
        self.assertEqual(commands, [
            "systemctl --user daemon-reload",
            "systemctl --user enable --now pr-autoapprove.timer",
            "systemctl --user enable pr-autoapprove-dashboard.service",
            "systemctl --user restart pr-autoapprove-dashboard.service",
        ])

    def test_linux_escapes_percent_quote_and_dollar(self):
        path = '/opt/50%/bin:/with"quote:/usr/bin:/bin'
        res = self.linux(path=path + ":" + os.environ["PATH"])
        lines = res.files["pr-autoapprove.service"]["text"].splitlines()
        self.assertIn(f'Environment="PATH={systemd_quote(path + ":" + os.environ["PATH"])}"', lines)
        self.assertTrue(any(line.startswith("WorkingDirectory=") and "50%%" in line for line in lines))
        exec_line = next(line for line in lines if line.startswith("ExecStart="))
        self.assertIn("50%%", exec_line)
        self.assertNotIn("50% ", exec_line)
        self.assertIn("$$x", exec_line)
        self.assertIn('\\"q\\"', exec_line)

    def test_macos_plists(self):
        res = self.macos()
        self.assertEqual(res.rc, 0, res.err)
        self.assertEqual(sorted(res.files), ["pr-autoapprove-dashboard.plist", "pr-autoapprove.plist"])
        for name, item in res.files.items():
            self.assertEqual(item["path"], str(self.agent_dir / name))
        bot = plistlib.loads(res.files["pr-autoapprove.plist"]["text"].encode())
        self.assertEqual(bot["Label"], "pr-autoapprove")
        self.assertEqual(bot["StartInterval"], 720)
        self.assertIs(bot["RunAtLoad"], True)
        self.assertEqual(bot["ProgramArguments"], [sys.executable, f"{self.real_bot}/pr_bot.py"])
        self.assertEqual(bot["WorkingDirectory"], self.real_bot)
        self.assertEqual(bot["EnvironmentVariables"]["PATH"], os.environ["PATH"])
        self.assertEqual(bot["ProcessType"], "Background")
        self.assertNotIn("KeepAlive", bot)
        dash = plistlib.loads(res.files["pr-autoapprove-dashboard.plist"]["text"].encode())
        self.assertEqual(dash["Label"], "pr-autoapprove-dashboard")
        self.assertIs(dash["KeepAlive"], True)
        self.assertIs(dash["RunAtLoad"], True)
        self.assertEqual(dash["ProgramArguments"], [sys.executable, f"{self.real_bot}/dashboard.py"])
        self.assertEqual(dash["EnvironmentVariables"]["PATH"], os.environ["PATH"])
        self.assertNotIn("StartInterval", dash)

    def test_macos_escapes_xml(self):
        res = self.macos(path='/opt/a&b:/x"y\'z<w>:' + os.environ["PATH"])
        self.assertIn("a&amp;b", res.files["pr-autoapprove.plist"]["text"])
        bot = plistlib.loads(res.files["pr-autoapprove.plist"]["text"].encode())
        self.assertEqual(bot["EnvironmentVariables"]["PATH"], '/opt/a&b:/x"y\'z<w>:' + os.environ["PATH"])
        self.assertEqual(bot["WorkingDirectory"], self.real_bot)

    def test_macos_commands_bootout_then_bootstrap(self):
        uid = os.getuid()
        commands = self.macos().commands
        self.assertEqual(len(commands), 4)
        self.assertEqual(commands[0], f"launchctl bootout gui/{uid}/pr-autoapprove")
        self.assertTrue(commands[1].startswith(f"launchctl bootstrap gui/{uid} "))
        self.assertIn("pr-autoapprove.plist", commands[1])
        self.assertEqual(commands[2], f"launchctl bootout gui/{uid}/pr-autoapprove-dashboard")
        self.assertTrue(commands[3].startswith(f"launchctl bootstrap gui/{uid} "))
        self.assertIn("pr-autoapprove-dashboard.plist", commands[3])

    def test_custom_task_name_in_files_labels_and_commands(self):
        self.write_env(f"SCHEDULE_TASK_NAME=acme-review.v2\nPYTHON_PATH={sys.executable}\n")
        linux = self.linux()
        self.assertEqual(sorted(linux.files), ["acme-review.v2-dashboard.service", "acme-review.v2.service", "acme-review.v2.timer"])
        self.assertIn("Unit=acme-review.v2.service", linux.files["acme-review.v2.timer"]["text"].splitlines())
        self.assertIn("systemctl --user enable --now acme-review.v2.timer", linux.commands)
        macos = self.macos()
        self.assertEqual(sorted(macos.files), ["acme-review.v2-dashboard.plist", "acme-review.v2.plist"])
        self.assertEqual(plistlib.loads(macos.files["acme-review.v2.plist"]["text"].encode())["Label"], "acme-review.v2")
        self.assertEqual(plistlib.loads(macos.files["acme-review.v2-dashboard.plist"]["text"].encode())["Label"], "acme-review.v2-dashboard")

    def test_env_with_bom_crlf_quotes_and_repeated_keys(self):
        raw = (
            "\ufeff  SCHEDULE_TASK_NAME = 'my-login-bot' \r\n"
            "# comentário\r\n"
            'SCHEDULE_INTERVAL_MINUTES="3"\r\n'
            "SCHEDULE_INTERVAL_MINUTES=9\r\n"
            f'PYTHON_PATH="{sys.executable}"\r\n'
        ).encode("utf-8")
        self.write_env(raw=raw)
        res = self.linux()
        self.assertEqual(res.rc, 0, res.err)
        self.assertIn("my-login-bot.timer", res.files)
        self.assertIn("OnUnitActiveSec=9min", res.files["my-login-bot.timer"]["text"].splitlines())
        self.assertIn(exec_start(sys.executable, f"{self.real_bot}/pr_bot.py"), res.files["my-login-bot.service"]["text"].splitlines())

    def test_defaults_without_env_file(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        res = self.linux(env=False, path=f"{bin_dir}:{os.environ['PATH']}")
        self.assertEqual(res.rc, 0, res.err)
        self.assertIn("OnUnitActiveSec=5min", res.files["pr-autoapprove.timer"]["text"].splitlines())
        self.assertIn(f'ExecStart="{bin_dir}/python3" ', res.files["pr-autoapprove.service"]["text"])

    def test_relative_python_path_is_resolved_against_bot_dir(self):
        (self.bot / "venv").mkdir()
        (self.bot / "venv" / "python").symlink_to(sys.executable)
        self.write_env("PYTHON_PATH=venv/python\n")
        res = self.linux()
        self.assertEqual(res.rc, 0, res.err)
        self.assertIn(exec_start(f"{self.real_bot}/venv/python", f"{self.real_bot}/pr_bot.py"), res.files["pr-autoapprove.service"]["text"])

    def test_invalid_task_names_fail(self):
        for name in ["bad name", "a/b", "x;y", "-x", "a$b"]:
            with self.subTest(name=name):
                self.write_env(f"SCHEDULE_TASK_NAME={name}\nPYTHON_PATH={sys.executable}\n")
                res = self.linux()
                self.assertNotEqual(res.rc, 0)
                self.assertIn("SCHEDULE_TASK_NAME", res.err)
                self.assertEqual(res.out, "")

    def test_missing_python_fails(self):
        self.write_env(f"PYTHON_PATH={self.root}/nao-existe/python\n")
        res = self.linux()
        self.assertNotEqual(res.rc, 0)
        self.assertIn("PYTHON_PATH", res.err)

    def test_python_older_than_3_11_fails(self):
        old = self.root / "old-python"
        old.write_text("#!/bin/sh\nexit 1\n")
        old.chmod(old.stat().st_mode | stat.S_IXUSR)
        self.write_env(f"PYTHON_PATH={old}\n")
        res = self.linux()
        self.assertNotEqual(res.rc, 0)
        self.assertIn("3.11", res.err)

    def test_invalid_intervals_fail(self):
        for value in ["0", "-3", "abc", "1.5", ""]:
            with self.subTest(flag=value):
                self.assertNotEqual(self.linux("--interval", value).rc, 0)
        self.assertNotEqual(self.run_script("--dry-run", "--platform", "linux", "--interval").rc, 0)
        self.write_env(f"SCHEDULE_INTERVAL_MINUTES=0\nPYTHON_PATH={sys.executable}\n")
        self.assertNotEqual(self.linux().rc, 0)

    def test_interval_with_leading_zero_is_decimal(self):
        self.assertIn("OnUnitActiveSec=10min", self.linux("--interval", "010").files["pr-autoapprove.timer"]["text"].splitlines())

    def test_unknown_flag_and_platform_fail(self):
        self.write_env()
        self.assertNotEqual(self.run_script("--dry-run", "--platform", "linux", "--nope").rc, 0)
        self.assertNotEqual(self.run_script("--dry-run", "--platform", "windows").rc, 0)

    def test_unsupported_system_points_to_powershell_installer(self):
        self.write_env()
        stub = self.root / "stub"
        stub.mkdir()
        uname = stub / "uname"
        uname.write_text("#!/bin/sh\necho MINGW64_NT-10.0\n")
        uname.chmod(uname.stat().st_mode | stat.S_IXUSR)
        res = self.run_script("--dry-run", path=f"{stub}:{os.environ['PATH']}")
        self.assertNotEqual(res.rc, 0)
        self.assertIn("install_task.ps1", res.err)

    def test_uninstall_linux_lists_removal_commands(self):
        self.write_env()
        res = self.run_script("--uninstall", "--dry-run", "--platform", "linux")
        self.assertEqual(res.rc, 0, res.err)
        self.assertEqual(res.files, {})
        unit = str(self.unit_dir)
        self.assertEqual(res.commands, [
            "systemctl --user disable --now pr-autoapprove.timer",
            "systemctl --user stop pr-autoapprove.service",
            "systemctl --user disable --now pr-autoapprove-dashboard.service",
            f"rm -f {unit}/pr-autoapprove.timer",
            f"rm -f {unit}/pr-autoapprove.service",
            f"rm -f {unit}/pr-autoapprove-dashboard.service",
            "systemctl --user daemon-reload",
            "systemctl --user reset-failed pr-autoapprove.service pr-autoapprove.timer pr-autoapprove-dashboard.service",
        ])

    def test_uninstall_macos_lists_removal_commands(self):
        self.write_env()
        uid = os.getuid()
        res = self.run_script("--uninstall", "--dry-run", "--platform", "macos")
        self.assertEqual(res.rc, 0, res.err)
        agents = str(self.agent_dir)
        self.assertEqual(res.commands, [
            f"launchctl bootout gui/{uid}/pr-autoapprove",
            f"launchctl bootout gui/{uid}/pr-autoapprove-dashboard",
            f"rm -f {agents}/pr-autoapprove.plist",
            f"rm -f {agents}/pr-autoapprove-dashboard.plist",
        ])

    def test_uninstall_does_not_need_a_working_python(self):
        self.write_env(f"PYTHON_PATH={self.root}/nao-existe\n")
        self.assertEqual(self.run_script("--uninstall", "--dry-run", "--platform", "linux").rc, 0)

    def test_mismatched_quotes_are_not_stripped(self):
        for value in ["\"foo'", "'foo\"", '"foo', '"']:
            with self.subTest(value=value):
                self.write_env(f"SCHEDULE_TASK_NAME={value}\nPYTHON_PATH={sys.executable}\n")
                res = self.linux()
                self.assertNotEqual(res.rc, 0)
                self.assertIn("SCHEDULE_TASK_NAME", res.err)
        for value in ['"foo"', "'foo'"]:
            with self.subTest(value=value):
                self.write_env(f"SCHEDULE_TASK_NAME={value}\nPYTHON_PATH={sys.executable}\n")
                self.assertIn("foo.timer", self.linux().files)

    def test_real_linux_install_writes_units_and_calls_systemctl(self):
        res = self.run_real("--platform", "linux")
        self.assertEqual(res.rc, 0, res.err)
        self.assertEqual(self.calls()[:5], [
            "systemctl --user show-environment",
            "systemctl --user daemon-reload",
            "systemctl --user enable --now pr-autoapprove.timer",
            "systemctl --user enable pr-autoapprove-dashboard.service",
            "systemctl --user restart pr-autoapprove-dashboard.service",
        ])
        self.assertTrue(self.calls()[5].startswith("loginctl show-user "))
        dry = self.run_script("--dry-run", "--platform", "linux", path=self.fake_path())
        for name, item in dry.files.items():
            self.assertEqual((self.unit_dir / name).read_text(), item["text"])
        self.assertIn("Tarefa pr-autoapprove a cada 12 min, com " + sys.executable, res.out)
        self.assertIn("Painel ao vivo: http://127.0.0.1:8765/", res.out)
        self.assertIn("loginctl enable-linger", res.out)

    def test_real_linux_install_skips_linger_tip_when_enabled(self):
        res = self.run_real("--platform", "linux", extra_env={"FAKE_LINGER": "yes"})
        self.assertEqual(res.rc, 0, res.err)
        self.assertNotIn("enable-linger", res.out)

    def test_real_linux_uninstall_removes_units_and_resets_failed(self):
        self.assertEqual(self.run_real("--platform", "linux").rc, 0)
        installed = len(self.calls())
        res = self.run_real("--uninstall", "--platform", "linux")
        self.assertEqual(res.rc, 0, res.err)
        self.assertEqual(list(self.unit_dir.iterdir()), [])
        self.assertEqual(self.calls()[installed:], [
            "systemctl --user disable --now pr-autoapprove.timer",
            "systemctl --user stop pr-autoapprove.service",
            "systemctl --user disable --now pr-autoapprove-dashboard.service",
            "systemctl --user daemon-reload",
            "systemctl --user reset-failed pr-autoapprove.service pr-autoapprove.timer pr-autoapprove-dashboard.service",
        ])
        self.assertEqual(res.out.count("Removido: "), 3)
        self.assertEqual(self.run_real("--uninstall", "--platform", "linux").rc, 0)

    def test_real_linux_without_user_manager_prints_cron_and_installs_nothing(self):
        res = self.run_real("--platform", "linux", extra_env={"FAKE_NO_USER_MANAGER": "1"})
        self.assertNotEqual(res.rc, 0)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(self.calls(), ["systemctl --user show-environment"])
        self.assertIn("systemd de usuário indisponível", res.err)
        lines = res.out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "PATH=" + self.fake_path())
        escaped = self.real_bot.replace("%", "\\%")
        self.assertEqual(lines[1], f"*/12 * * * * cd '{escaped}' && '{sys.executable}' pr_bot.py >/dev/null 2>&1")
        self.assertEqual(lines[2], f"@reboot cd '{escaped}' && '{sys.executable}' dashboard.py >/dev/null 2>&1")

    def test_real_linux_cron_warns_when_interval_is_not_exact(self):
        cases = {"7": True, "12": False, "90": True, "120": False, "300": True, "1440": False, "1500": True}
        for interval, approximate in cases.items():
            with self.subTest(interval=interval):
                res = self.run_real("--platform", "linux", "--interval", interval, extra_env={"FAKE_NO_USER_MANAGER": "1"})
                self.assertEqual("Aviso:" in res.err, approximate, res.err)
        hours = self.run_real("--platform", "linux", "--interval", "120", extra_env={"FAKE_NO_USER_MANAGER": "1"})
        self.assertTrue(hours.out.splitlines()[1].startswith("0 */2 * * * cd "))

    def test_real_install_without_bot_scripts_fails_before_touching_anything(self):
        for platform in ("linux", "macos"):
            with self.subTest(platform=platform):
                res = self.run_real("--platform", platform, files=False)
                self.assertNotEqual(res.rc, 0)
                self.assertIn("pr_bot.py", res.err)
                self.assertEqual(list(self.home.iterdir()), [])
                self.assertEqual(self.calls(), [])

    def test_real_macos_install_waits_for_bootout_before_bootstrap(self):
        uid = os.getuid()
        res = self.run_real("--platform", "macos")
        self.assertEqual(res.rc, 0, res.err)
        expected = []
        for label in ("pr-autoapprove", "pr-autoapprove-dashboard"):
            plist = plistlib.loads((self.agent_dir / f"{label}.plist").read_bytes())
            self.assertEqual(plist["Label"], label)
            expected += [f"launchctl bootout gui/{uid}/{label}"]
            expected += [f"launchctl print gui/{uid}/{label}"] * 4
            expected += [f"launchctl bootstrap gui/{uid} {self.agent_dir}/{label}.plist"]
        self.assertEqual(self.calls(), expected)
        self.assertIn("Painel ao vivo: http://127.0.0.1:8765/", res.out)

    def test_real_macos_install_fails_when_launchd_never_unloads(self):
        res = self.run_real("--platform", "macos", extra_env={"FAKE_PRINT_ALWAYS": "1"})
        self.assertNotEqual(res.rc, 0)
        self.assertIn("o launchd ainda não descarregou gui/", res.err)
        self.assertIn("pr-autoapprove", res.err)
        self.assertFalse([call for call in self.calls() if " bootstrap " in call])
        self.assertFalse([call for call in self.calls() if call.endswith("-dashboard")])

    def test_real_macos_uninstall_removes_plists(self):
        uid = os.getuid()
        self.assertEqual(self.run_real("--platform", "macos").rc, 0)
        installed = len(self.calls())
        res = self.run_real("--uninstall", "--platform", "macos")
        self.assertEqual(res.rc, 0, res.err)
        self.assertEqual(list(self.agent_dir.iterdir()), [])
        self.assertEqual(self.calls()[installed:], [
            f"launchctl bootout gui/{uid}/pr-autoapprove",
            f"launchctl bootout gui/{uid}/pr-autoapprove-dashboard",
        ])
        self.assertEqual(res.out.count("Removido: "), 2)

    def test_dry_run_creates_nothing(self):
        self.write_env()
        before = sorted(p.name for p in self.bot.iterdir())
        for args in (["--platform", "linux"], ["--platform", "macos"], ["--uninstall", "--platform", "linux"], ["--uninstall", "--platform", "macos"]):
            with self.subTest(args=args):
                res = self.run_script("--dry-run", *args)
                self.assertEqual(res.rc, 0, res.err)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.bot.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
