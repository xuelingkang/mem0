"""探测本机 LLM 网关对 Graphiti 三种结构化输出客户端的兼容性。

结论用于方案：Graphiti 的写入管道对结构化输出有硬依赖（每次 add_episode 有多次
带 response_model 的调用），网关能力决定 Graphiti 能否就地跑。
"""

import asyncio
import json
import os

from pydantic import BaseModel

ENV = {}
for line in open("/Users/xuelingkang/Documents/Containers/mem0/server/.env"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    ENV[k.strip()] = v.strip()

os.environ.setdefault("OPENAI_API_KEY", ENV["LLM_API_KEY"])

from graphiti_core.llm_client.config import LLMConfig  # noqa: E402
from graphiti_core.llm_client.openai_client import OpenAIClient  # noqa: E402
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient  # noqa: E402
from graphiti_core.prompts.models import Message  # noqa: E402


class EdgeDuplicate(BaseModel):
    duplicate_facts: list[int]
    contradicted_facts: list[int]


MESSAGES = [
    Message(role="system", content="You compare facts and report duplicate/contradicting indices. Respond in json."),
    Message(role="user", content='New fact: "Xue lives in Beijing."\nExisting facts:\n0: "Xue lives in Beijing, China."\n1: "Xue works at XKW."'),
]


async def probe(name, client):
    print(f"=== {name} ===")
    try:
        out = await client.generate_response(MESSAGES, response_model=EdgeDuplicate, group_id="probe", prompt_name="probe")
        print("  OK ->", json.dumps(out)[:300])
    except Exception as e:
        print(f"  FAIL {type(e).__name__}: {str(e)[:500]}")


async def main():
    cfg = LLMConfig(api_key=ENV["LLM_API_KEY"], base_url=ENV["LLM_BASE_URL"], model=ENV["MEM0_DEFAULT_LLM_MODEL"])
    await probe("OpenAIGenericClient(json_schema)", OpenAIGenericClient(config=cfg, structured_output_mode="json_schema"))
    await probe("OpenAIGenericClient(json_object)", OpenAIGenericClient(config=cfg, structured_output_mode="json_object"))
    await probe("OpenAIClient(responses.parse)", OpenAIClient(config=cfg))


asyncio.run(main())
