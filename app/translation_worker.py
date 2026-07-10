from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def translate_single_text_openai(text: str, payload: dict[str, Any]) -> tuple[str, dict[str, int]]:
    endpoint = str(
        payload.get("llm_endpoint")
        or os.getenv("REMOTE_LLM_ENDPOINT")
        or os.getenv("LOCAL_LLM_ENDPOINT")
        or ""
    )
    if not endpoint:
        raise RuntimeError("REMOTE_LLM_ENDPOINT is not configured")
    model = str(payload.get("model_name") or os.getenv("LOCAL_LLM_MODEL", "qwen2.5:3b-instruct-q4_K_M"))
    timeout = int(payload.get("llm_timeout_seconds") or os.getenv("LOCAL_LLM_TIMEOUT_SECONDS", "300"))
    temperature = float(os.getenv("LOCAL_LLM_TEMPERATURE", "0"))
    max_tokens = int(os.getenv("LOCAL_LLM_MAX_OUTPUT_TOKENS", "2048"))
    api_key = str(payload.get("llm_api_key") or os.getenv("REMOTE_LLM_API_KEY") or os.getenv("LOCAL_LLM_API_KEY", ""))

    prompt = str(payload.get("prompt") or f"これを訳せ（字幕１つだけ）\n\n{text}")

    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
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

    content = data["choices"][0]["message"]["content"].strip()
    if content.startswith('"') and content.endswith('"'):
        content = content[1:-1].strip()
    elif content.startswith("'") and content.endswith("'"):
        content = content[1:-1].strip()

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    usage_dict = {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }
    return content, usage_dict


def translate_single_text_gemini(text: str, payload: dict[str, Any]) -> tuple[str, dict[str, int]]:
    model = str(payload.get("model_name") or os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    timeout = int(os.getenv("LOCAL_LLM_TIMEOUT_SECONDS", "300"))
    temperature = float(os.getenv("LOCAL_LLM_TEMPERATURE", "0"))
    max_tokens = int(os.getenv("LOCAL_LLM_MAX_OUTPUT_TOKENS", "2048"))
    endpoint = os.getenv(
        "GEMINI_API_ENDPOINT",
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    ).format(model=urllib.parse.quote(model, safe=""))
    url = f"{endpoint}?key={urllib.parse.quote(api_key, safe='')}"

    prompt = f"この字幕の一部を翻訳せよ。訳文以外は一文字も入れるな\n\n{text}"

    body = json.dumps(
        {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        message = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"gemini api http error {error.code}: {message}") from error

    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise RuntimeError("gemini api returned no candidates")
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict) or not parts[0].get("text"):
        raise RuntimeError("gemini api returned no text part")
    
    translated_text = str(parts[0]["text"]).strip()
    if translated_text.startswith('"') and translated_text.endswith('"'):
        translated_text = translated_text[1:-1].strip()
    elif translated_text.startswith("'") and translated_text.endswith("'"):
        translated_text = translated_text[1:-1].strip()

    usage = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
    usage_dict = {
        "input_tokens": int(usage.get("promptTokenCount") or 0),
        "output_tokens": int(usage.get("candidatesTokenCount") or 0),
        "total_tokens": int(usage.get("totalTokenCount") or 0),
    }
    return translated_text, usage_dict


def format_context_lines(items: list[dict[str, Any]], *, include_translation: bool = False) -> str:
    lines: list[str] = []
    for item in items[-5:]:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if include_translation:
            translated = str(item.get("translated_text") or item.get("ja") or "").strip()
            suffix = f" / {translated}" if translated else ""
            lines.append(f"- {text}{suffix}")
        else:
            lines.append(f"- {text}")
    return "\n".join(lines) if lines else "なし"


def build_single_subtitle_prompt(item: dict[str, Any], payload: dict[str, Any]) -> str:
    previous_japanese = payload.get("previous_japanese")
    translated_by_id = {}
    if isinstance(previous_japanese, list):
        translated_by_id = {
            str(prev.get("id")): str(prev.get("text") or "")
            for prev in previous_japanese
            if isinstance(prev, dict)
        }
    before = []
    context_before = payload.get("context_before")
    if isinstance(context_before, list):
        for entry in context_before[-5:]:
            if isinstance(entry, dict):
                copy = dict(entry)
                copy["translated_text"] = translated_by_id.get(str(entry.get("id")), "")
                before.append(copy)

    after = []
    context_after = payload.get("context_after")
    if isinstance(context_after, list):
        after = [entry for entry in context_after[:5] if isinstance(entry, dict)]

    title = str(payload.get("video_title") or "").strip() or "不明"
    text = str(item.get("text") or "")
    return (
        f"動画タイトル（原語で）\n{title}\n\n"
        f"前5つの字幕（原語）（日本語）\n{format_context_lines(before, include_translation=True)}\n\n"
        f"これを訳せ（字幕１つだけ）\n{text}\n\n"
        f"後5つの字幕（原語）\n{format_context_lines(after)}\n\n"
        "出力は日本語訳だけ。説明、引用符、番号、前後の字幕の訳は出さない。"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as file:
        payload = json.load(file)

    provider = str(payload.get("translation_provider") or os.getenv("LOCAL_LLM_ENGINE", "openai_compatible")).strip().lower()
    target = payload.get("target", [])
    
    translations = []
    usage_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }

    def process_item(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
        text = item.get("text", "")
        if not text or not text.strip():
            return {"id": item.get("id"), "text": text}, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        item_payload = dict(payload)
        item_payload["prompt"] = build_single_subtitle_prompt(item, payload)
        if provider == "gemini_api":
            translated_text, usage = translate_single_text_gemini(text, item_payload)
        else:
            translated_text, usage = translate_single_text_openai(text, item_payload)
        
        return {"id": item.get("id"), "text": translated_text}, usage

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(process_item, target))

    for trans, usage in results:
        translations.append(trans)
        for key in usage_totals:
            usage_totals[key] += usage.get(key, 0)

    result = {
        "translations": translations,
        "_usage": usage_totals,
    }

    with open(args.output, "w", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
