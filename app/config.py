# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from typing import Literal
from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 必填,故意不给默认值。模型名是跟着账号走的,给一个默认值等于替学员猜:猜错不会在
    # 启动时报错,而是在第一次调模型时抛一个看不懂的上游 401。没有默认值,缺了就在启动
    # 时报 chat_model Field required,直接指向 .env。调优常量(top_k、步数、阈值这些)
    # 是另一类,它们该留在代码里带默认值。
    chat_model: str
    # 三组上游各自直连,不经网关。base_url 和 key 跟着账号走,所以和 chat_model 一样必填;
    # Embedding/Rerank 可分别指定上游；当前 .env 使用智谱共用 COMMERCEFLOW_RAG_API_KEY。
    chat_base_url: str
    chat_api_key: str
    embed_base_url: str = "https://api.siliconflow.cn/v1"
    embed_api_key: str = Field(validation_alias=AliasChoices("COMMERCEFLOW_RAG_API_KEY", "EMBED_API_KEY"))
    rerank_base_url: str = "https://api.siliconflow.cn/v1"
    rerank_api_key: str = Field(validation_alias=AliasChoices("COMMERCEFLOW_RAG_API_KEY", "RERANK_API_KEY"))
    # 思考链控制。三项都留空就用上游默认,填了才传。
    # 关不关是上游的能力、不是参数写法:同一个 thinking.type,硅基流动的 DeepSeek 和
    # MiniMax-M3 上能关(实测 22.4s/318 token → 1.4s/16),MiniMax-M2.x 上会被静默忽略。
    chat_thinking: str = ""           # disabled 关 / adaptive 开
    chat_reasoning_effort: str = ""   # 开着时的强度:low / high / max
    chat_reasoning_split: str = ""    # MiniMax 专属:true 让思考进 reasoning_content 独立字段,
                                      # 而不是混在 content 里的 <think>。关不掉思考链时靠它兜底
    token_budget: int = 2000
    # Request-wide operating cap, not a claim about a provider's model capacity.
    # Set to <= the deployed model's documented context limit; overrides use exact names.
    chat_context_limit: int = Field(default=32768, gt=0)
    chat_output_reserve: int = Field(default=2048, gt=0)
    chat_output_token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    chat_context_safety_margin: int = Field(default=1024, ge=0)
    chat_model_context_limits: dict[str, int] = {}

    @model_validator(mode="after")
    def validate_context_budgets(self):
        reserved = self.chat_output_reserve + self.chat_context_safety_margin
        if any(limit <= reserved for limit in
               [self.chat_context_limit, *self.chat_model_context_limits.values()]):
            raise ValueError("context limits must exceed output reserve plus safety margin")
        return self

    database_url: str = "mysql+asyncmy://root:root@localhost:3306/commerceflow"
    test_database_url: str = "mysql+asyncmy://root:root@localhost:3306/commerceflow_ch02_test"
    # ch03 知识库检索
    embed_model: str = "BAAI/bge-m3"          # 上游真实名(无网关别名可用)
    embed_dim: int = 1024                    # 与 Milvus dense 向量维度一致;智谱 embedding-3 为 2048
    milvus_uri: str = "http://localhost:19530"   # ch04: Lite → Standalone
    kb_read_model: Literal["legacy", "v2"] = "legacy"
    kb_v2_dual_write: bool = False
    kb_v2_collection: str = Field(default="knowledge_v2_test", pattern=r"^knowledge_v2_[A-Za-z0-9_]+$")
    # ch04 混合检索 + 重排
    rerank_model: str = "BAAI/bge-reranker-v2-m3"   # 上游真实名
    recall_top_k: int = 50        # dense/BM25 各召回 Top-50
    rerank_top_k: int = 10        # 精排出 Top-10
    subquery_split: bool = True   # 一问多意图时按子句拆开各检索一遍再轮转合并(见 retrieval)
    rerank_min_score: float = 0.3 # 重排最高分低于此 → 检索证据低,拒答
    # ch05 图编排
    max_agent_steps: int = 6              # ReAct 环最大步数(封顶,超则兜底)
    # token 花销不在环里卡:那是成本控制,归 ch09 跟 Langfuse 的账一起看。
    # tokens_used 仍逐步累加,进 trace 供排障和统计,只是不再当停止条件。
    checkpointer_db_path: str = "data/ch05_checkpoints.sqlite"  # LangGraph checkpointer(data/*.db* 已 gitignore)
    # ch06 意图识别模型配置(先求准:默认大模型;降级路仅种子,本章不实现运行时)
    intent_model: str = ""            # 意图识别模型名;空=回落 chat_model
    intent_small_model: str = ""      # 降级路小模型(种子,未接运行时)
    intent_mode: str = "accuracy"     # accuracy=只用大模型;cost=小模型判→低置信升级大模型(未实现)
    intent_conf_threshold: float = 0.6  # cost 模式升级阈值(种子)
    # ch07 会话上下文管理(滑窗 + 异步摘要)
    summary_trigger_messages: int = 30    # 距上次摘要新增满多少条触发后台任务
    context_window_turns: int = 8         # 摘要后保留原文的最近轮数(需求 5-10 取中)
    context_window_max_tokens: int = 3000 # 滑窗 token 上限(trim_messages 兜底)
    summary_model: str = ""               # 摘要模型,空=回落 chat_model
    # ch08 工具系统(注册中心 + 执行引擎 + MCP 接入)
    mcp_logistics_url: str = "http://127.0.0.1:8101/mcp"    # 物流 MCP Server
    mcp_aftersales_url: str = "http://127.0.0.1:8102/mcp"   # 售后 MCP Server
    tool_default_timeout: float = 5.0    # 内置工具默认超时(秒)
    mcp_tool_timeout: float = 10.0       # MCP 工具默认超时(走 HTTP,放宽)
    tool_max_retries: int = 2            # 只读工具暂时性故障最大重试次数
    demo_ticket_delay_seconds: float = 0.0  # 验收 6:>0 时 create_ticket 人为变慢(写超时演示)
    # ch09 可观测(Langfuse 自部署;三者齐全才挂回调,缺省时系统照常跑、测试环境不依赖)
    # 环境变量名与课程 README/SDK 一致:LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = ""     # 如 http://localhost:3000(自部署地址,数据不出门)
    # ch09 置信度闸(正式版):阈值由 make calibrate-confidence 在 ch04 评估集上校准回填,不拍脑袋
    # 实测:可答桶 p50=0.715 vs 应拒桶 p50=0.105/max=0.251,YoudenJ 最优点 0.26
    # (可答通过率 85%、应拒放行率 0%,报告见 data/ch09/reports/confidence_calibration.txt,
    #  「观测与成本」页 /observability 上也能看,推荐值与这里不一致时页面会标出来)
    evidence_confidence_threshold: float = 0.26

    # ch11: no fallback key and no public registration by default.
    auth_signing_key: SecretStr = SecretStr("")
    auth_issuer: str = "commerceflow"
    auth_audience: str = "commerceflow-web"
    auth_access_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    auth_refresh_ttl_seconds: int = Field(default=604800, ge=300, le=2592000)
    auth_registration_enabled: bool = False
    auth_cookie_secure: bool = True
    auth_allowed_origins: list[str] = []
    auth_rate_limit: int = Field(default=10, ge=1, le=100)
    auth_rate_window_seconds: int = Field(default=300, ge=30)
    approval_ttl_seconds: int = Field(default=300, ge=30, le=900)
    tool_security_mode: Literal["plan", "default", "auto", "bypass"] = "default"
    tool_allow_bypass: bool = False
    mcp_allow_local_http: bool = False
    mcp_logistics_token: SecretStr = SecretStr("")
    mcp_aftersales_token: SecretStr = SecretStr("")
    # Approved MCP contracts, keyed by server/tool, are SHA256 of canonical input schema.
    mcp_schema_pins: dict[str, str] = {}


settings = Settings()
