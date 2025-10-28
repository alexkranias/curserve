#!/usr/bin/env python3
import json
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List


# === Tunable constants ===
PREFILL_TPS = 8000  # tokens/sec for prompt prefill (controls TTFT)
DECODE_TPS = 50     # tokens/sec for generation (controls decode time)
DEFAULT_COMPLETION_TOKENS = 128
THINKING_TOKENS = 32  # tokens worth of "thinking" time before tool calls


def approx_num_tokens_from_text(text: str) -> int:
    """Very rough token estimate: ~4 chars per token."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def iter_message_texts(messages: List[Dict[str, Any]]) -> List[str]:
    for msg in messages or []:
        content = msg.get("content")
        if isinstance(content, str):
            yield content
        elif isinstance(content, list):
            for part in content:
                # OpenAI style: {"type": "text", "text": "..."}
                if isinstance(part, dict):
                    txt = part.get("text")
                    if isinstance(txt, str):
                        yield txt


def approx_prompt_tokens(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for text in iter_message_texts(messages):
        total += approx_num_tokens_from_text(text)
    return total


def approx_response_tokens(req: Dict[str, Any]) -> int:
    max_tokens = req.get("max_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        return max_tokens
    return DEFAULT_COMPLETION_TOKENS


def get_last_user_text(messages: List[Dict[str, Any]]) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict):
                        txt = part.get("text")
                        if isinstance(txt, str):
                            parts.append(txt)
                if parts:
                    return "\n".join(parts)
    return ""


# Common patterns that trigger search_file_content tool calls
SEARCH_PATTERNS = [
    "def ", "class ", "import ", "from ", "async def ", "await ",
    "__init__", "if __name__ == '__main__'", "template<", "std::vector",
    "#include <iostream>", "int main(", "constexpr", "nullptr",
    "using namespace std", "std::move", "printf(", "struct ", "TODO:",
    "FIXME:", "http://", "https://", "0x", "lambda ", "console.log(",
    "function(", "SELECT ", "className=", "useState(", "pytest",
    "#define ", "std::unique_ptr", "raise ", "return "
]


def generate_tool_call_response(user_text: str, completion_tokens: int) -> Dict[str, Any]:
    """Generate a response with tool calls based on user input."""
    tool_calls = []

    # Look for search patterns in user text
    for pattern in SEARCH_PATTERNS:
        if pattern in user_text:
            call_id = f"call_{random.randint(1000, 9999)}"
            tool_calls.append({
                "index": 0,
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "search_file_content",
                    "arguments": json.dumps({
                        "pattern": pattern,
                        "path": "."
                    })
                }
            })
            break  # Only generate one tool call per response

    if tool_calls:
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": tool_calls
        }

    # Fallback: regular text response
    return {
        "role": "assistant",
        "content": f"Simulated response ({completion_tokens} tok)"
    }


def build_chat_completion(
    model: str,
    messages: List[Dict[str, Any]],
    prompt_tokens: int,
    completion_tokens: int,
) -> Dict[str, Any]:
    created = int(time.time())
    user_text = get_last_user_text(messages)
    message = generate_tool_call_response(user_text, completion_tokens)

    return {
        "id": f"chatcmpl-{created}-{random.randint(1000, 9999)}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def build_chunk(
    chunk_id: str,
    model: str,
    delta: Dict[str, Any],
    finish_reason: str | None,
) -> Dict[str, Any]:
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "vLLM-Emulator/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Minimal logging
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, obj: Dict[str, Any], status: int = 200) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if self.path == "/v1/chat/completions":
            body = self._read_json()
            model = body.get("model", "emulated-vllm")
            messages = body.get("messages", [])
            stream = bool(body.get("stream", False))

            prompt_tokens = approx_prompt_tokens(messages)
            completion_tokens = approx_response_tokens(body)

            # Simulate TTFT (prefill latency)
            ttft_sec = prompt_tokens / max(1, PREFILL_TPS)

            # Simulate decode time
            decode_sec = completion_tokens / max(1, DECODE_TPS)

            user_text = get_last_user_text(messages)

            if stream:
                # SSE stream
                chunk_id = f"chatcmpl-{int(time.time())}-{random.randint(1000, 9999)}"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                # TTFT: send role delta after prefill delay
                time.sleep(ttft_sec)
                first = build_chunk(chunk_id, model, {"role": "assistant"}, None)
                self.wfile.write(f"data: {json.dumps(first)}\n\n".encode("utf-8"))
                self.wfile.flush()

                # Check for tool call patterns
                tool_calls = []
                for pattern in SEARCH_PATTERNS:
                    if pattern in user_text:
                        call_id = f"call_{random.randint(1000, 9999)}"
                        tool_calls.append({
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": "search_file_content",
                                "arguments": json.dumps({
                                    "pattern": pattern,
                                    "path": "."
                                })
                            }
                        })
                        break

                if tool_calls:
                    # Send tool call deltas
                    time.sleep(THINKING_TOKENS / DECODE_TPS)
                    tool_call_chunk = build_chunk(
                        chunk_id, model,
                        {"tool_calls": tool_calls},
                        None
                    )
                    self.wfile.write(
                        f"data: {json.dumps(tool_call_chunk)}\n\n".encode("utf-8")
                    )
                    self.wfile.flush()

                    # Finish with tool_calls reason
                    done_chunk = build_chunk(chunk_id, model, {}, "tool_calls")
                    self.wfile.write(
                        f"data: {json.dumps(done_chunk)}\n\n".encode("utf-8")
                    )
                else:
                    # Send content delta
                    time.sleep(decode_sec)
                    content_chunk = build_chunk(
                        chunk_id, model,
                        {"content": f"Simulated response ({completion_tokens} tok)"},
                        None
                    )
                    self.wfile.write(
                        f"data: {json.dumps(content_chunk)}\n\n".encode("utf-8")
                    )
                    self.wfile.flush()

                    # Finish normally
                    done_chunk = build_chunk(chunk_id, model, {}, "stop")
                    self.wfile.write(
                        f"data: {json.dumps(done_chunk)}\n\n".encode("utf-8")
                    )

                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            else:
                # Blocking response: sleep TTFT + decode, then return final JSON
                time.sleep(ttft_sec + decode_sec)
                obj = build_chat_completion(
                    model, messages, prompt_tokens, completion_tokens
                )
                self._send_json(obj, 200)
                return

        # Fallback: 404
        self.send_response(404)
        self.end_headers()


def main(host: str = "127.0.0.1", port: int = 8000) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"vLLM emulator listening on http://{host}:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
