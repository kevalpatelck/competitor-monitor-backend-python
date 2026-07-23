import asyncio
import json
import re
from typing import Optional, Dict, Any
from src.config.env import config
from src.utils.logger import logger


def _call_llm_sync(
    prompt: str,
    system_prompt: Optional[str] = None,
    json_mode: bool = False,
    prefer_anthropic: bool = True
) -> str:
    """Synchronous LLM caller — internal implementation."""
    # 1. Try Anthropic/Claude first if preferred and API key is set
    if prefer_anthropic and config["anthropic_api_key"]:
        try:
            import anthropic
            logger.info("[AI] Calling Anthropic/Claude API...")
            client = anthropic.Anthropic(api_key=config["anthropic_api_key"])
            
            system_arg = system_prompt if system_prompt else ""
            if json_mode:
                prompt += "\n\nIMPORTANT: Return ONLY a valid raw JSON object. Do not wrap in markdown or add explanations."
            
            message = client.messages.create(
                model="claude-3-5-sonnet-20240620",
                max_tokens=4000,
                temperature=0.1,
                system=system_arg if system_arg else None,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            
            content = ""
            if message.content:
                content = message.content[0].text.strip()
            
            if json_mode and content:
                content = re.sub(r"^```[\w]*\n?", "", content)
                content = re.sub(r"\n?```$", "", content).strip()
                
            return content
        except Exception as anth_err:
            logger.warning(f"[AI WARNING] Anthropic call failed, falling back to OpenAI: {anth_err}")

    # 2. Fallback or explicit call to OpenAI
    if config["openai_api_key"]:
        try:
            import openai
            logger.info("[AI] Calling OpenAI API...")
            
            client_args = {"api_key": config["openai_api_key"]}
            if config["openai_base_url"]:
                client_args["base_url"] = config["openai_base_url"]
                
            client = openai.OpenAI(**client_args)
            
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            
            completion_args = {
                "model": config["openai_model_name"] or "gpt-4o-mini",
                "messages": messages,
                "temperature": 0.1,
            }
            
            if json_mode:
                completion_args["response_format"] = {"type": "json_object"}
                
            completion = client.chat.completions.create(**completion_args)
            content = completion.choices[0].message.content.strip()
            
            if json_mode and content:
                content = re.sub(r"^```[\w]*\n?", "", content)
                content = re.sub(r"\n?```$", "", content).strip()
                
            return content
        except Exception as openai_err:
            logger.error(f"[AI ERROR] OpenAI call failed: {openai_err}")
            raise openai_err
            
    raise ValueError("Neither Anthropic nor OpenAI keys are configured or succeeded.")


async def call_llm(
    prompt: str,
    system_prompt: Optional[str] = None,
    json_mode: bool = False,
    prefer_anthropic: bool = True
) -> str:
    """Async LLM caller — runs synchronous implementation in a thread to avoid blocking the event loop."""
    return await asyncio.to_thread(_call_llm_sync, prompt, system_prompt, json_mode, prefer_anthropic)
