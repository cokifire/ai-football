"""codex CLI 子进程封装：以 `codex exec` 非交互模式执行一次推理。

设计约束（来自项目历史教训与安全要求）：
- prompt 走 stdin（预测/情报 prompt 可达上万字符，规避 argv 长度限制）；
- 每次调用新建临时工作目录，防止 codex 加载仓库 AGENTS.md/工程上下文偏离任务，
  并把 workspace-write 的写入范围锁死在临时目录内，用完即删；
- api_key 只通过子进程环境变量注入，绝不进命令行（避免 ps 泄漏）；
- 墙钟超时用进程组强杀，杜绝 codex 残留子进程挂死后端 worker
  （此前 HTTP 路径就出现过 worker 永久挂起 / 前端 socket hang up）；
- 失败仅对 codex 自身重试（不回退 HTTP）。
"""

import os
import re
import shutil
import subprocess
import tempfile
import time

from loguru import logger

from app.core.config import settings

_SCHEMA_DIR = os.path.join(os.path.dirname(__file__), "schemas")
# 最终预测输出契约（10 字段，落 predictions.llm_*）
PREDICTION_SCHEMA_PATH = os.path.join(_SCHEMA_DIR, "prediction_output.schema.json")
# 情报审核输出契约（accepted_ids）
INTELLIGENCE_AUDIT_SCHEMA_PATH = os.path.join(_SCHEMA_DIR, "intelligence_audit.schema.json")


class CodexError(RuntimeError):
    """codex 调用配置/环境错误（如可执行文件不存在、schema 文件缺失）。"""


def codex_available() -> bool:
    """codex 可执行文件是否能解析到（情报/预测侧的跳过守卫用它判定）。"""
    return shutil.which(settings.codex_bin) is not None


def _build_command(*, workdir: str, out_path: str, model: str, base_url: str,
                   schema_path: str | None, sandbox: str) -> list[str]:
    cmd = [
        settings.codex_bin, "exec", "-",
        "--skip-git-repo-check",
        "--ephemeral",
        "-s", sandbox,
        "-C", workdir,
    ]
    if model:
        cmd += ["-m", model]
    if schema_path:
        if not os.path.isfile(schema_path):
            raise CodexError(f"schema 文件不存在: {schema_path}")
        cmd += ["--output-schema", schema_path]
    # 最终消息文件：优先于 stdout 解析
    cmd += ["-o", out_path]
    if base_url:
        # .env 配了 base_url 时覆盖 config.toml 的 provider；key 不在此处，
        # 由 _spawn_env() 以环境变量注入（provider 的 env_key 指向该变量名）。
        pid = settings.codex_provider_id
        cmd += [
            "-c", f'model_providers.{pid}.name = "{pid}"',
            "-c", f'model_providers.{pid}.base_url = "{base_url}"',
            "-c", f'model_providers.{pid}.wire_api = "responses"',
            # env_key 必须显式声明：缺了它 codex 不知道读哪个环境变量，
            # _spawn_env() 注入的 key 不会进请求头，代理报 401 Missing API key
            "-c", f'model_providers.{pid}.env_key = "{settings.codex_api_key_env}"',
            "-c", f'model_provider = "{pid}"',
        ]
    if settings.codex_network:
        # codex exec 没有 --search 参数，联网开关只能走 -c 覆盖
        cmd += ["-c", 'network_access = "enabled"']
    return cmd


def _spawn_env(api_key: str) -> dict:
    env = os.environ.copy()
    if settings.codex_home:
        env["CODEX_HOME"] = settings.codex_home
    if api_key:
        env[settings.codex_api_key_env] = api_key
    return env


def _spawn(cmd: list[str], *, workdir: str, env: dict) -> subprocess.Popen:
    kwargs: dict = dict(
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=workdir,
        env=env,
    )
    if os.name == "posix":
        # 独立进程组，超时可整棵杀掉，避免残留子进程
        kwargs["start_new_session"] = True
    else:  # Windows 本地测试用
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(cmd, **kwargs)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """强杀 codex 整棵进程树：只杀主进程会让子进程继续持有管道，worker 仍会挂死。

    posix 用进程组（_spawn 里 start_new_session=True）；Windows 没有进程组，
    用 taskkill /T 递归杀，失败再退化为 kill 主进程。
    """
    if os.name == "posix":
        try:
            # 不加 poll() 守卫：主进程可能已退出但子进程仍在，那样就漏杀了
            os.killpg(os.getpgid(proc.pid), 9)
        except Exception:
            pass
    else:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _reap(proc: subprocess.Popen) -> None:
    """收尸：wait 有上界并关掉管道，避免 Popen 变僵尸进程并泄漏 fd。"""
    try:
        proc.wait(timeout=5)
    except Exception:
        pass
    for name in ("stdout", "stderr"):
        try:
            pipe = getattr(proc, name, None)
            if pipe is not None:
                pipe.close()
        except Exception:
            pass


def _redact(text: str) -> str:
    """日志脱敏：第三方输出的报错里可能带回 key。"""
    return re.sub(r"(sk|api)[-_]?key\S*", "***", text or "", flags=re.IGNORECASE)


def _run_once(prompt: str, *, model: str, base_url: str, api_key: str,
              schema_path: str | None, sandbox: str, timeout: float,
              label: str) -> str:
    """执行一次 codex exec，返回最终消息文本；失败抛异常。"""
    with tempfile.TemporaryDirectory(prefix="codex-run-") as workdir:
        out_path = os.path.join(workdir, "last_message.txt")
        cmd = _build_command(workdir=workdir, out_path=out_path, model=model,
                             base_url=base_url, schema_path=schema_path,
                             sandbox=sandbox)
        proc = _spawn(cmd, workdir=workdir, env=_spawn_env(api_key))
        started = time.monotonic()
        try:
            try:
                stdout, stderr = proc.communicate(input=prompt, timeout=max(timeout, 1.0))
            except subprocess.TimeoutExpired:
                # communicate 的 timeout 不会杀子进程（官方文档要求手动杀），
                # 这里杀整棵进程树再收尸，防残留子进程挂死后端 worker
                _kill_process_tree(proc)
                _reap(proc)
                logger.warning("{} 墙钟超时({}s)，已强杀进程树", label, round(timeout))
                raise TimeoutError(f"codex exec timeout after {timeout}s")
        finally:
            if proc.poll() is None:  # 仍在跑才补杀，成功返回时无需多余动作
                _kill_process_tree(proc)
                _reap(proc)
        duration = round(time.monotonic() - started, 1)
        code = proc.returncode
        if code != 0:
            logger.warning("{} 退出码 {}，耗时 {}s，stderr_tail={!r}",
                           label, code, duration, _redact((stderr or "")[-500:]))
        else:
            logger.info("{} 完成，耗时 {}s", label, duration)

        # 优先读 -o 文件（codex 写入的最终消息）；为空回退 stdout 原文
        message = ""
        try:
            with open(out_path, "r", encoding="utf-8", errors="replace") as fh:
                message = fh.read()
        except OSError:
            message = ""
        if not message.strip():
            message = stdout or ""
        return message


def run_codex(
    prompt: str,
    *,
    schema_path: str | None = None,
    model: str = "",
    base_url: str = "",
    api_key: str = "",
    sandbox: str = "workspace-write",
    timeout: float = 1200.0,
    retries: int = 2,
    label: str = "codex",
) -> str | None:
    """以 codex exec 非交互模式执行一次推理，返回最终消息文本。

    - ``model`` / ``base_url`` / ``api_key`` 来自项目 .env（不是 ~/.codex/config.toml）；
      base_url 非空才覆盖 provider，否则沿用服务器 codex 配置。
    - ``timeout`` 是**包含所有重试的总墙钟预算**；每次尝试用剩余预算。
    - 失败/超时/空输出只对 codex 自身重试 ``retries`` 次，不回退 HTTP；
      全部失败返回 None。
    """
    if not prompt or not prompt.strip():
        logger.warning("{} 跳过: prompt 为空", label)
        return None
    if not codex_available():
        logger.warning("{} 跳过: 未找到可执行文件 ({})", label, settings.codex_bin)
        return None

    deadline = time.monotonic() + max(timeout, 1.0)
    attempts = max(retries, 0) + 1
    for attempt in range(1, attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning("{} 超出总超时预算 {}s，停止重试", label, round(timeout))
            return None
        try:
            message = _run_once(
                prompt, model=model, base_url=base_url, api_key=api_key,
                schema_path=schema_path, sandbox=sandbox,
                timeout=remaining, label=f"{label}(第{attempt}次)",
            )
        except TimeoutError:
            return None
        except CodexError:
            # 配置/环境错误（如 schema 缺失）重试无意义，直接上抛
            raise
        except Exception as exc:
            logger.warning("{} 第 {} 次执行异常: {}", label, attempt, exc)
            continue
        if message and message.strip():
            return message
        logger.warning("{} 第 {} 次未返回内容", label, attempt)
    return None
