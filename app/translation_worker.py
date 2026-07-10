from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


def build_prompt(payload: dict[str, Any]) -> str:
    strict = payload.get("strict")
    extra = (
        "\nThis is a retry. Be stricter: preserve every id and output exactly one item per TARGET."
        if strict
        else ""
    )
    return f"""You are translating subtitles into natural Japanese.

Rules:
- Translate TARGET only.
- CONTEXT_BEFORE and CONTEXT_AFTER are reference only.
- PREVIOUS_JAPANESE is reference only for terminology and style.
- Prefer the original source text over PREVIOUS_JAPANESE.
- Do not add information not present in the source.
- Do not change numbers, URLs, names, product names, or technical terms without reason.
- Preserve subtitle ids.
- Output count must match TARGET count.
- Output JSON only. No markdown.
- Use concise Japanese suitable for subtitles.
{extra}

Return this JSON shape:
{{"translations":[{{"id":"...","text":"..."}}]}}

VIDEO_TITLE:
{payload.get("video_title") or ""}

TOPIC:
{payload.get("topic") or ""}

GLOSSARY:
{payload.get("glossary") or ""}

SOURCE_LANGUAGE:
{payload.get("source_language")}

TARGET_LANGUAGE:
{payload.get("target_language")}

CONTEXT_BEFORE:
{json.dumps(payload.get("context_before", []), ensure_ascii=False)}

TARGET:
{json.dumps(payload.get("target", []), ensure_ascii=False)}

CONTEXT_AFTER:
{json.dumps(payload.get("context_after", []), ensure_ascii=False)}

PREVIOUS_JAPANESE:
{json.dumps(payload.get("previous_japanese", []), ensure_ascii=False)}
"""


def call_openai_compatible(prompt: str) -> dict[str, Any]:
    endpoint = os.getenv("LOCAL_LLM_ENDPOINT", "http://127.0.0.1:11434/v1/chat/completions")
    model = os.getenv("LOCAL_LLM_MODEL", "qwen2.5:3b-instruct-q4_K_M")
    timeout = int(os.getenv("LOCAL_LLM_TIMEOUT_SECONDS", "300"))
    temperature = float(os.getenv("LOCAL_LLM_TEMPERATURE", "0"))
    max_tokens = int(os.getenv("LOCAL_LLM_MAX_OUTPUT_TOKENS", "2048"))
    api_key = os.getenv("LOCAL_LLM_API_KEY", "")

    body = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a precise subtitle translation engine. Return JSON only.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        message = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"local llm http error {error.code}: {message}") from error

    content = data["choices"][0]["message"]["content"]
    return json.loads(content)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as file:
        payload = json.load(file)

    result = call_openai_compatible(build_prompt(payload))

    with open(args.output, "w", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
