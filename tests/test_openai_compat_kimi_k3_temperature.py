# encoding:utf-8
"""Custom-provider (OpenAICompatibleBot) Kimi K3 temperature stripping.

A custom provider pointing at the Kimi Coding Plan endpoint
(api.kimi.com/coding) with model ``k3`` routes through
``OpenAICompatibleBot`` — not ``MoonshotBot`` — so the temperature=1-only
rule must live here too. The endpoint answers any other temperature with
HTTP 400 "invalid temperature: only 1 is allowed for this model".
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.openai_compatible_bot import OpenAICompatibleBot


class _KimiCustomBot(OpenAICompatibleBot):
    def __init__(self, model):
        self._model = model

    def get_api_config(self):
        return {
            "api_key": "sk-test",
            "api_base": "https://api.kimi.com/coding/v1",
            "model": self._model,
            "default_temperature": 0.9,
        }


def _capture(monkeypatch, model):
    bot = _KimiCustomBot(model)
    captured = {}

    def fake_sync(request_params, api_key, api_base):
        captured["params"] = request_params
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(bot, "_handle_sync_response", fake_sync)
    bot.call_with_tools([{"role": "user", "content": "hi"}], tools=[], stream=False)
    return captured["params"]


def test_bare_k3_strips_sampling_params(monkeypatch):
    params = _capture(monkeypatch, "k3")
    for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
        assert key not in params, f"{key} must be omitted for k3, got {params.get(key)}"


def test_k3_variants_strip_sampling_params(monkeypatch):
    for model in ("k3.1-pro", "k3-thinking", "K3", " kimi-k3 ", "kimi-k2.7-code",
                  "kimi-k2-thinking", "kimi-k1.5", "kimi-for-coding"):
        params = _capture(monkeypatch, model)
        assert "temperature" not in params, f"{model}: temperature must be omitted"


def test_k30_prefix_does_not_match(monkeypatch):
    params = _capture(monkeypatch, "k30-something")
    assert params.get("temperature") == 0.9, "k30-* is not a K3 model; temperature stays"


def test_non_kimi_model_keeps_temperature(monkeypatch):
    params = _capture(monkeypatch, "MiniMax-M3")
    assert params.get("temperature") == 0.9


def test_predicate_word_boundaries():
    pred = OpenAICompatibleBot._is_kimi_reasoning_model
    assert pred("k3") and pred("k3.1-pro") and pred("k3-0711")
    assert pred("kimi-k3") and pred("kimi-k2-thinking")
    assert not pred("k30-something")
    assert not pred("MiniMax-M3")
    assert not pred("") and not pred(None)
