import json
import re
import asyncio
from typing import Dict, Any, List, Optional
from src.utils.ai_client import call_llm
from src.utils.logger import logger

SYSTEM_PROMPT = """You are an expert competitive intelligence analyst. You compare two versions of marketing/promotional text from a competitor's webpage and determine if the change is meaningful for business monitoring purposes.

Return ONLY valid JSON in this exact format:
{"meaningful": boolean, "summary": string}

Rules:
- meaningful = true ONLY for actual business changes:
  - New offer, promotion, or discount
  - Changed price claim or pricing language
  - Changed guarantee or warranty terms
  - New value proposition or positioning
  - New product launch announcement
  - Changed shipping/delivery promises
  - Removed or changed key selling points
  
- meaningful = false for:
  - Whitespace or formatting changes
  - Punctuation differences
  - Minor rewording with identical meaning
  - Case changes
  - HTML/CSS class name changes
  - No actual content change
  - Seasonal date updates with same offer

- summary: If meaningful, write a concise 1-2 sentence summary of what changed from a business perspective. If not meaningful, write "No meaningful change".

Return ONLY the JSON object, no other text."""

async def analyze_messaging_change(old_text: str, new_text: str) -> Dict[str, Any]:
    # Quick pre-checks
    if old_text == new_text:
        return {"meaningful": False, "summary": "No change in text"}

    normalized_old = re.sub(r"\s+", " ", old_text).strip()
    normalized_new = re.sub(r"\s+", " ", new_text).strip()
    if normalized_old == normalized_new:
        return {"meaningful": False, "summary": "No meaningful change (whitespace only)"}

    prompt = f"""Compare these two versions of marketing text from a competitor website:

PREVIOUS VERSION:
\"\"\"
{old_text[:2000]}
\"\"\"

CURRENT VERSION:
\"\"\"
{new_text[:2000]}
\"\"\"

Is this a meaningful business change? Return ONLY the JSON response."""

    try:
        # call_llm prefers Anthropic/Claude (spec compliant) with fallback to OpenAI
        response_text = await call_llm(
            prompt=prompt,
            system_prompt=SYSTEM_PROMPT,
            json_mode=True,
            prefer_anthropic=True
        )
        
        json_match = re.search(r"\{[\s\S]*\}", response_text)
        if not json_match:
            logger.warning(f"[AI MESSAGING] Could not parse response as JSON: {response_text[:200]}")
            return {
                "meaningful": True, # Safer to alert than miss
                "summary": "AI analysis returned unparseable response. Text change was detected."
            }

        result = json.loads(json_match.group(0))
        return {
            "meaningful": bool(result.get("meaningful")),
            "summary": str(result.get("summary", "No summary provided"))
        }

    except Exception as err:
        logger.error(f"[AI MESSAGING ERROR] {err}")
        return {
            "meaningful": True,
            "summary": f"AI analysis failed ({err}). Text change was detected — manual review recommended."
        }

async def diff_messaging(old_text: str, new_text: str) -> Dict[str, Any]:
    # Add rate-limiting pause to avoid hitting rate limits on API providers
    await asyncio.sleep(1.0)
    return await analyze_messaging_change(old_text, new_text)

async def batch_analyze_changes(old_text: str, new_text: str, numeric_diffs: List[Dict[str, Any]]) -> Dict[str, Any]:
    if old_text == new_text:
        return {"meaningful": False, "summary": "No change in text"}

    normalized_old = re.sub(r"\s+", " ", old_text).strip()
    normalized_new = re.sub(r"\s+", " ", new_text).strip()
    if normalized_old == normalized_new:
        return {"meaningful": False, "summary": "No meaningful change (whitespace only)"}

    prompt = f"""Compare these two versions of marketing text from a competitor website:

PREVIOUS VERSION:
\"\"\"
{old_text[:2000]}
\"\"\"

CURRENT VERSION:
\"\"\"
{new_text[:2000]}
\"\"\"

Additionally, the following other numeric/product changes were detected on the same page update:
{json.dumps(numeric_diffs, indent=2)}

Is this marketing text change a meaningful business change? Return ONLY the JSON response."""

    try:
        response_text = await call_llm(
            prompt=prompt,
            system_prompt=SYSTEM_PROMPT,
            json_mode=True,
            prefer_anthropic=True
        )
        
        json_match = re.search(r"\{[\s\S]*\}", response_text)
        if not json_match:
            logger.warning(f"[AI MESSAGING BATCH] Could not parse response as JSON: {response_text[:200]}")
            return {
                "meaningful": True,
                "summary": "AI analysis returned unparseable response. Text change was detected."
            }

        result = json.loads(json_match.group(0))
        return {
            "meaningful": bool(result.get("meaningful")),
            "summary": str(result.get("summary", "No summary provided"))
        }

    except Exception as err:
        logger.error(f"[AI MESSAGING BATCH ERROR] {err}")
        return {
            "meaningful": True,
            "summary": f"AI analysis failed ({err}). Text change was detected."
        }
