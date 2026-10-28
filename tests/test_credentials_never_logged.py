"""凭据不许进日志 —— 回归判据。

为什么有这条：目标机 192.168.0.114 的 run.log 里出现过 27 次
``web_password`` 明文（文件权限 664）。根因在 config.py 的脱敏判据：

    ("key" in k or "secret" in k)

``web_password`` / ``external_api_token`` 里既没有 "key" 也没有 "secret"，
于是压根没进脱敏分支，直接明文打印。

这条测试同时钉住四件事：
  1. 覆盖判据必须包含 password / token / secret / credential / auth / pwd
  2. 掩码后不得出现任何原值子串（哪怕短于 8 个字符）
  3. 不得泄露首尾字符（旧实现留首 3 尾 3）
  4. 数字型凭据（config.py 自己承认存在 numeric password）也要遮蔽

判据本身要能证伪：用**修复前的旧实现**跑同一组用例必须 FAIL。
"""

import json

from config import drag_sensitive

# 这些键名在真实 config.json 里出现过（实测目标机）
SENSITIVE_KEYS = [
    "web_password",
    "minimax_api_key",
    "external_api_token",
    "api_secret",
    "auth_token",
    "pwd",
]

# 非凭据键 —— 不能被误伤遮蔽（可观测性不能一起丢光）
INNOCENT_KEYS = [
    "web_host",
    "web_port",
    "agent_workspace",
    "channel_type",
    "model",
    "agent_permission_mode",
]


def test_sensitive_keys_are_detected_by_the_recursive_masks():
    """敏感键的判定必须生效 —— 通过公开 API 的**行为**验证。

    刻意不直接 import 内部函数：那样在旧实现下会变成 ImportError，
    而 ImportError 是"没测起来"，不是"断言抓住了 bug"。
    判据必须能区分这两种情况。
    """
    for k in SENSITIVE_KEYS:
        masked = json.dumps(drag_sensitive({k: "PLACEHOLDER-SECRET-VALUE"}), ensure_ascii=False)
        assert "PLACEHOLDER-SECRET-VALUE" not in masked, f"{k} 的值没被遮蔽：{masked}"


def test_innocent_keys_are_not_treated_as_sensitive():
    """非凭据键不能被误伤遮蔽（可观测性不能一起丢光）。"""
    cfg = {"web_host": "0.0.0.0", "web_port": 9899, "model": "MiniMax-M3"}
    masked = json.dumps(drag_sensitive(cfg), ensure_ascii=False)
    for k, v in cfg.items():
        assert str(v) in masked, f"{k} 被误伤遮蔽了：{masked}"


def test_no_plaintext_survives_in_the_masked_config():
    cfg = {
        "web_password": "s3cr3t-console-password",
        "minimax_api_key": "sk-1234567890abcdef",
        "external_api_token": "tok-abcdefghijklmnop",
        "web_host": "0.0.0.0",
        "web_port": 9899,
    }
    masked = json.dumps(drag_sensitive(cfg), ensure_ascii=False)
    for k, v in cfg.items():
        if k in INNOCENT_KEYS:
            continue
        assert v not in masked, f"{k} 的明文出现在脱敏结果里：{masked}"


def test_short_secrets_are_masked_too():
    """旧实现对长度 <= 8 的值原样返回 —— 短密码等于没脱敏。"""
    masked = json.dumps(drag_sensitive({"web_password": "abc123"}), ensure_ascii=False)
    assert "abc123" not in masked, f"短密码没被遮蔽：{masked}"


def test_numeric_credentials_are_masked():
    """config.py 承认存在 numeric password，非 str 类型同样要遮蔽。"""
    masked = json.dumps(drag_sensitive({"web_password": 123456}), ensure_ascii=False)
    assert "123456" not in masked, f"数字密码没被遮蔽：{masked}"


def test_no_leading_or_trailing_characters_leak():
    """旧实现保留首 3 尾 3 —— 对密码而言那仍是 6 个字符的泄露。"""
    secret = "ABCDEFGHIJKLMNOP"
    masked = json.dumps(drag_sensitive({"web_password": secret}), ensure_ascii=False)
    assert secret[:3] not in masked, f"泄露了首 3 字符：{masked}"
    assert secret[-3:] not in masked, f"泄露了尾 3 字符：{masked}"


def test_nested_credentials_are_masked():
    cfg = {"custom_providers": [{"id": "x1", "api_key": "sk-nested-secret-long", "name": "test"}]}
    masked = json.dumps(drag_sensitive(cfg), ensure_ascii=False)
    assert "sk-nested-secret-long" not in masked, f"嵌套凭据没被遮蔽：{masked}"
    assert "x1" in masked, "非凭据字段被误伤了"


def test_string_config_is_masked_too():
    raw = json.dumps({"web_password": "s3cr3t-console-password"})
    masked = drag_sensitive(raw)
    assert "s3cr3t-console-password" not in masked, f"字符串 config 没被脱敏：{masked}"


def test_presence_is_still_observable():
    """遮蔽不能把可观测性一起弄丢：键名在、"已设置"可辨。"""
    masked = json.dumps(drag_sensitive({"web_password": "s3cr3t-console-password"}), ensure_ascii=False)
    assert "web_password" in masked, "键名消失了，没法判断是否已设置"
    assert "chars" in masked, "长度信息消失了，没法判断是否已设置"
