"""LLM decision layer — strategy optimization via OpenAI proxy."""

from __future__ import annotations

import json
import os

import openai

from agents.system_prompt import SYSTEM_PROMPT

_client = openai.OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url="http://litellm-production.eba-pvykax23.eu-west-1.elasticbeanstalk.com",
)


def get_llm_actions(compressed_state: str, observation: dict, day: int) -> list[dict]:
    user_msg = f"Day {day}/30.\n\n{compressed_state}"

    try:
        response = _client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=600,
        )
        content = response.choices[0].message.content.strip()

        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

        tool_calls = json.loads(content)
        if not isinstance(tool_calls, list):
            return []
        return tool_calls

    except Exception as e:
        print(f"  LLM error on day {day}: {e}")
        return []