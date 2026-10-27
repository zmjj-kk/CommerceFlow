# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
import pytest
from pydantic import ValidationError

from app.config import Settings


_REQUIRED = {
    "CHAT_MODEL": "deepseek-ai/DeepSeek-V4-Flash",
    "CHAT_BASE_URL": "https://api.siliconflow.cn/v1",
    "CHAT_API_KEY": "sk-test",
    "EMBED_API_KEY": "sk-test",
    "RERANK_API_KEY": "sk-test",
}


def _fill(monkeypatch, skip=None):
    # 隔离开发机已有 .env 导出的环境变量,检验代码默认值与必填项
    for k in ("COMMERCEFLOW_RAG_API_KEY", "EMBED_BASE_URL", "RERANK_BASE_URL", "EMBED_MODEL", "RERANK_MODEL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in _REQUIRED.items():
        if k == skip:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


@pytest.mark.parametrize("missing", ("CHAT_MODEL", "CHAT_BASE_URL", "CHAT_API_KEY"))
def test_账号相关的配置都必填(monkeypatch, missing):
    # 这几项都跟着账号走,给默认值等于替学员猜:猜错不在启动时报错,而是第一次调上游时
    # 抛一个看不懂的 401。缺了就在启动报 Field required,直接指向 .env
    _fill(monkeypatch, skip=missing)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_缺少所有检索密钥时校验失败(monkeypatch):
    _fill(monkeypatch)
    monkeypatch.delenv("EMBED_API_KEY", raising=False)
    monkeypatch.delenv("RERANK_API_KEY", raising=False)
    monkeypatch.delenv("COMMERCEFLOW_RAG_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_嵌入与重排的地址有默认值_只要填_key(monkeypatch):
    # 这两个上游地址是固定的(硅基流动),不该逼学员填
    _fill(monkeypatch)
    s = Settings(_env_file=None)
    assert s.embed_base_url == "https://api.siliconflow.cn/v1"
    assert s.rerank_base_url == "https://api.siliconflow.cn/v1"


def test_settings_defaults(monkeypatch):
    # 隔离 .env 与其他测试可能泄漏进 os.environ 的值,测的是代码默认值
    _fill(monkeypatch)
    s = Settings(_env_file=None)
    assert s.token_budget == 2000
    assert s.rerank_model == "BAAI/bge-reranker-v2-m3"


def test_settings_env_override(monkeypatch):
    _fill(monkeypatch)
    monkeypatch.setenv("TOKEN_BUDGET", "500")
    assert Settings(_env_file=None).token_budget == 500


def test_shared_zhipu_key_alias(monkeypatch):
    _fill(monkeypatch)
    monkeypatch.delenv("EMBED_API_KEY", raising=False)
    monkeypatch.delenv("RERANK_API_KEY", raising=False)
    monkeypatch.setenv("COMMERCEFLOW_RAG_API_KEY", "test-shared-key")
    s = Settings(_env_file=None)
    assert s.embed_api_key == "test-shared-key"
    assert s.rerank_api_key == "test-shared-key"
