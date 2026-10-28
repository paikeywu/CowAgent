import json
import os
import subprocess
import sys
import pytest
from agent.tools.read.read import Read


def _make_symlink(link, target):
    """建符号链接；平台没有这个能力时，明确地 skip 掉**这一条**。

    Windows 上非管理员/未开开发者模式时，建 symlink 会报
    ``OSError: [WinError 1314] 客户端没有所需的特权``
    （symlink 要 SeCreateSymbolicLinkPrivilege）。这是平台能力限制，
    不是被测代码的问题 —— 所以只 skip 依赖 symlink 的那部分断言，
    其余验证照跑；Linux CI 上这条 symlink 覆盖是实打实跑过的。
    """
    try:
        link.symlink_to(target)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314 or "privilege" in str(exc).lower():
            pytest.skip("本机没有创建符号链接的权限（Windows 需开发者模式或管理员）")
        raise


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO fixture")
@pytest.mark.parametrize("alias", [False, True])
def test_read_fifo_returns_error_without_waiting_for_writer(tmp_path, alias):
    fifo = tmp_path / "pipe.txt"
    os.mkfifo(fifo)
    path = fifo
    if alias:
        path = tmp_path / "alias.txt"
        _make_symlink(path, fifo)
    # 'ready' 必须走 stderr —— 原来它走 stdout，而父进程用
    # `for line in process.stdout` 读到 'ready' 就 break，
    # **缓冲把后面的 JSON 行一起吞掉了**，output 变成空串，
    # json.loads('') 报 "Expecting value: line 1 column 1"。
    # 那不是产品的问题，是这个测试自己把自己的输出吃了。
    code = "from agent.tools.read.read import Read; import json,sys; print('ready', file=sys.stderr, flush=True); r=Read({'cwd':sys.argv[1]}).execute({'path':sys.argv[2]}); print(json.dumps({'status':r.status,'error':r.result}), flush=True)"
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # 超时放宽到 15s：验的是「不等待写端就返回错误」，不是「多快返回」。
        # 原来 2s 在负载高的机器上会假红（子进程要先 import 整个 agent 包）。
        output, error = process.communicate(timeout=15)
        assert process.returncode == 0, error
        # stdout 里可能混着 import 期的日志行，取最后一个非空行当 JSON
        lines = [line for line in output.splitlines() if line.strip()]
        assert lines, "子进程没有输出 JSON：stdout=%r stderr=%r" % (output, error)
        payload = json.loads(lines[-1])
        assert payload["status"] == "error", payload
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_regular_text_and_symlink_remain_readable(tmp_path):
    source = tmp_path / "text.txt"
    source.write_text("hello", encoding="utf-8")
    link = tmp_path / "alias.txt"
    _make_symlink(link, source)
    assert Read({"cwd": str(tmp_path)}).execute({"path": str(link)}).status == "success"
