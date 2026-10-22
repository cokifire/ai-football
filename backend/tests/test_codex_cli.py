"""llm.codex_cli 单元测试：不真实拉起 codex 子进程，全部用 monkeypatch 替换 Popen。

覆盖点（对应实现的关键约束）：
- prompt 经 stdin 传递（communicate(input=...)），命令行不携带 prompt 与 api_key；
- api_key 只进子进程环境变量，绝不进 argv（防 ps 泄漏）；
- base_url 非空才用 -c 覆盖 provider；
- 结果优先取 -o 最终消息文件，空文件回退 stdout；
- 空输出按 retries 重试后返回 None；
- 墙钟超时杀进程树并立即放弃；
- schema 文件缺失属于配置错误（CodexError），不重试直接上抛。
"""

import subprocess
import time

import pytest

import llm.codex_cli as codex_cli
from llm.codex_cli import (
    PREDICTION_SCHEMA_PATH,
    CodexError,
    run_codex,
)

API_KEY = "secret-key-123"


class _FakeProc:
    """记录 Popen 参数并可按需产出 stdout / -o 文件内容 / 超时的假进程。"""

    def __init__(self, cmd, *, stdout_text="", stderr_text="", exit_code=0,
                 hang_seconds=0.0, output_file_text=None):
        self.cmd = cmd
        self.kwargs = None  # 由工厂回填
        self.pid = 4321
        self.returncode = exit_code
        self.killed = False
        self.communicate_calls = 0
        self.stdin_input = None
        self._stdout = stdout_text
        self._stderr = stderr_text
        self._hang_seconds = hang_seconds
        self._output_file_text = output_file_text

    def communicate(self, input=None, timeout=None):
        self.communicate_calls += 1
        self.stdin_input = input
        if self._hang_seconds and not self.killed:
            # 模拟真实行为：超时到期时 communicate 抛 TimeoutExpired 且不杀进程
            if timeout is not None and timeout < self._hang_seconds:
                raise subprocess.TimeoutExpired(self.cmd, timeout)
            time.sleep(self._hang_seconds)
            return "", ""
        self.returncode = 0 if self.returncode is None else self.returncode
        if self._output_file_text is not None:
            out_path = self.cmd[self.cmd.index("-o") + 1]
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(self._output_file_text)
        return self._stdout, self._stderr

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True


def _spawn(monkeypatch, which="/usr/bin/codex", **proc_overrides):
    """拦截真实 Popen，返回 {'proc': 假进程} 供断言使用。

    只拦截 codex 调用本身：kill 进程树时会用到 subprocess.run（Windows 下是
    taskkill），那条路径必须走真实 Popen，否则假进程会覆盖掉被测的那一个。
    """
    holder = {}
    original_popen = codex_cli.subprocess.Popen

    def fake_popen(cmd, **kwargs):
        if not cmd or cmd[0] != "codex":
            return original_popen(cmd, **kwargs)
        proc = _FakeProc(cmd, **proc_overrides)
        proc.kwargs = kwargs
        holder["proc"] = proc
        return proc

    monkeypatch.setattr(codex_cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(codex_cli.shutil, "which", lambda _name: which)
    return holder


def test_prompt_and_key_never_in_argv(monkeypatch):
    holder = _spawn(monkeypatch, output_file_text='{"win":"主胜"}')
    prompt = "请预测" + "很长的比赛数据" * 500

    result = run_codex(prompt, model="gpt-test", api_key=API_KEY, label="t")

    assert result == '{"win":"主胜"}'
    cmd = holder["proc"].cmd
    argv_joined = " ".join(str(c) for c in cmd)
    # prompt 与 key 都不允许出现在命令行
    assert prompt[:50] not in argv_joined
    assert API_KEY not in argv_joined
    # prompt 经 stdin 传递
    assert holder["proc"].stdin_input == prompt
    # key 只注入环境变量
    env = holder["proc"].kwargs["env"]
    assert env["CODEX_API_KEY"] == API_KEY
    # 非交互模式 + 跳过 git 检查 + 临时会话
    assert cmd[1:3] == ["exec", "-"]
    assert "--skip-git-repo-check" in cmd
    assert "--ephemeral" in cmd
    # 模型经 -m 传入
    assert cmd[cmd.index("-m") + 1] == "gpt-test"


def test_sandbox_and_workspace_flags(monkeypatch):
    holder = _spawn(monkeypatch, output_file_text="ok")
    run_codex("p", sandbox="workspace-write")
    cmd = holder["proc"].cmd
    assert cmd[cmd.index("-s") + 1] == "workspace-write"
    # 工作目录是每次新建的临时目录（-C 与 cwd 一致）
    cwd = holder["proc"].kwargs["cwd"]
    assert cmd[cmd.index("-C") + 1] == cwd


def test_base_url_override_adds_provider_config(monkeypatch):
    holder = _spawn(monkeypatch, output_file_text="ok")
    run_codex("p", base_url="http://localhost:8080/v1")
    cmd = holder["proc"].cmd
    assert 'model_providers.codex_env.base_url = "http://localhost:8080/v1"' in cmd
    assert 'model_provider = "codex_env"' in cmd
    assert 'model_providers.codex_env.wire_api = "responses"' in cmd


def test_no_base_url_keeps_server_provider(monkeypatch):
    holder = _spawn(monkeypatch, output_file_text="ok")
    run_codex("p", base_url="")
    cmd = holder["proc"].cmd
    assert not any(str(c).startswith("model_providers.") for c in cmd)
    assert not any(str(c).startswith("model_provider =") for c in cmd)


def test_output_file_takes_priority_over_stdout(monkeypatch):
    holder = _spawn(monkeypatch, stdout_text="noise stdout", output_file_text='{"accepted_ids":[1]}')
    result = run_codex("p")
    assert result == '{"accepted_ids":[1]}'
    assert holder["proc"].communicate_calls == 1


def test_stdout_fallback_when_output_file_empty(monkeypatch):
    holder = _spawn(monkeypatch, stdout_text="plain text answer")
    result = run_codex("p")
    assert result == "plain text answer"


def test_empty_output_retries_then_none(monkeypatch):
    counter = {"n": 0}

    def fake_popen(cmd, **kwargs):
        counter["n"] += 1
        return _FakeProc(cmd)

    monkeypatch.setattr(codex_cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(codex_cli.shutil, "which", lambda _n: "/usr/bin/codex")

    assert run_codex("p", retries=2) is None
    assert counter["n"] == 3  # retries=2 -> 共 3 次尝试


def test_wall_clock_timeout_kills_process_tree(monkeypatch):
    holder = _spawn(monkeypatch, hang_seconds=30.0)
    started = time.monotonic()
    assert run_codex("p", timeout=0.5, retries=2) is None
    elapsed = time.monotonic() - started
    assert elapsed < 5  # 超时预算耗尽即返回，绝不等子进程跑完
    assert holder["proc"].killed is True
    assert holder["proc"].communicate_calls == 1  # 超时后改为 wait 收尸，不再二次 communicate


def test_schema_file_attached_and_missing_raises(monkeypatch):
    holder = _spawn(monkeypatch, output_file_text="{}")
    run_codex("p", schema_path=PREDICTION_SCHEMA_PATH)
    cmd = holder["proc"].cmd
    assert cmd[cmd.index("--output-schema") + 1] == PREDICTION_SCHEMA_PATH

    # schema 文件不存在属于配置错误：不重试，直接上抛
    with pytest.raises(CodexError):
        run_codex("p", schema_path="Z:/no/such/file.schema.json")


def test_prompt_or_binary_missing_skips(monkeypatch):
    monkeypatch.setattr(codex_cli.shutil, "which", lambda _n: "/usr/bin/codex")
    assert run_codex("   ") is None
    monkeypatch.setattr(codex_cli.shutil, "which", lambda _n: None)
    assert run_codex("p") is None
