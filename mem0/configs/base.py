import os
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from mem0.configs.rerankers.config import RerankerConfig
from mem0.embeddings.configs import EmbedderConfig
from mem0.llms.configs import LlmConfig
from mem0.vector_stores.configs import VectorStoreConfig

# Set up the directory path
home_dir = os.path.expanduser("~")
mem0_dir = os.environ.get("MEM0_DIR") or os.path.join(home_dir, ".mem0")


class MemoryItem(BaseModel):
    id: str = Field(..., description="The unique identifier for the text data")
    memory: str = Field(
        ..., description="The memory deduced from the text data"
    )  # TODO After prompt changes from platform, update this
    hash: Optional[str] = Field(None, description="The hash of the memory")
    # The metadata value can be anything and not just string. Fix it
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata for the text data")
    score: Optional[float] = Field(None, description="The score associated with the text data")
    created_at: Optional[str] = Field(None, description="The timestamp when the memory was created")
    updated_at: Optional[str] = Field(None, description="The timestamp when the memory was updated")


class DecayConfig(BaseModel):
    """检索衰减（memory decay）配置：Ebbinghaus 遗忘曲线 + 访问强化。

    设计见 `docs/design/memory-decay.md`。时间因子
    `decay_weight = floor + (1 - floor) * exp(-Δt / S)`，`S = halflife_days +
    strength_step_days * min(access_count, access_cap)`，乘进混合分后得到
    `final_score = similarity * decay_weight`。

    默认 `enabled=False`：关闭时时间因子恒为 1，且不产生任何足迹写入，检索行为与
    引入本机制之前逐位一致。
    """

    enabled: bool = Field(
        description="衰减总开关。关闭时时间因子恒为 1，且不产生任何足迹写入。",
        default=False,
    )
    halflife_days: float = Field(
        description="记忆强度基线 S₀（天），即首次出现时的遗忘曲线时间常数。",
        default=12.0,
        gt=0,
    )
    strength_step_days: float = Field(
        description="每次被召回所增加的记忆强度 ΔS（天）。",
        default=3.0,
        ge=0,
    )
    access_cap: int = Field(
        description="计入强度增长的召回次数上限 N_cap，越界后行为与恰好 N_cap 次一致。",
        default=20,
        ge=0,
    )
    floor: float = Field(
        description="时间因子下界 FLOOR，保证任何候选都不会因衰减被排除或清零。",
        default=0.90,
        gt=0,
        le=1.0,
    )
    cooldown_seconds: float = Field(
        description="同一记忆的强化冷却窗口（秒）；窗口内的重复命中不累加计数。",
        default=300.0,
        ge=0,
    )


class MemoryConfig(BaseModel):
    vector_store: VectorStoreConfig = Field(
        description="Configuration for the vector store",
        default_factory=VectorStoreConfig,
    )
    llm: LlmConfig = Field(
        description="Configuration for the language model",
        default_factory=LlmConfig,
    )
    embedder: EmbedderConfig = Field(
        description="Configuration for the embedding model",
        default_factory=EmbedderConfig,
    )
    history_db_path: str = Field(
        description="Path to the history database",
        default=os.path.join(mem0_dir, "history.db"),
    )
    reranker: Optional[RerankerConfig] = Field(
        description="Configuration for the reranker",
        default=None,
    )
    version: str = Field(
        description="The version of the API",
        default="v1.1",
    )
    custom_instructions: Optional[str] = Field(
        description="Custom instructions for fact extraction",
        default=None,
    )
    decay: DecayConfig = Field(
        description="Retrieval decay: Ebbinghaus forgetting curve plus access reinforcement.",
        default_factory=DecayConfig,
    )


class AzureConfig(BaseModel):
    """
    Configuration settings for Azure.

    Args:
        api_key (str): The API key used for authenticating with the Azure service.
        azure_deployment (str): The name of the Azure deployment.
        azure_endpoint (str): The endpoint URL for the Azure service.
        api_version (str): The version of the Azure API being used.
        default_headers (Dict[str, str]): Headers to include in requests to the Azure API.
    """

    api_key: str = Field(
        description="The API key used for authenticating with the Azure service.",
        default=None,
    )
    azure_deployment: str = Field(description="The name of the Azure deployment.", default=None)
    azure_endpoint: str = Field(description="The endpoint URL for the Azure service.", default=None)
    api_version: str = Field(description="The version of the Azure API being used.", default=None)
    default_headers: Optional[Dict[str, str]] = Field(
        description="Headers to include in requests to the Azure API.", default=None
    )
