"""JARVIS real-LLM smoke test. NEVER runs under pytest (testpaths=["tests"]).

Usage (real provider):
    JARVIS_LLM_PROVIDER=openai \\
    JARVIS_LLM_MODEL=gpt-4o-mini \\
    JARVIS_OPENAI_API_KEY=<redacted> \\
        .venv/bin/python scripts/smoke_llm.py

Stub mode (no key needed; validates the real HTTP wire path against a
local stub that mimics the chat-completions endpoint):
    .venv/bin/python scripts/smoke_llm.py --stub

Exit codes: 0 = provider returned a validated plan; 2 = misconfigured
(missing key/model) -- fails safe, no traceback, no secret in output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jarvis.adapters.llm.openai import OpenAIProvider  # noqa: E402
from jarvis.domain.contracts.common import new_id  # noqa: E402
from jarvis.domain.contracts.llm import LLMRequest  # noqa: E402
from jarvis.domain.contracts.nexus import CanonicalFact  # noqa: E402
from jarvis.ports.llm import LLMError  # noqa: E402

_STUB_PLAN = {
    "summary_key": "normal",
    "tone": "calm",
    "selected_fact_ids": ["FACT_NEXUS_AVAILABILITY"],
    "selected_recommendation_ids": [],
    "detail_level": "standard",
}


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the output clean
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth) < 12:
            self._send(401, {"error": {"message": "missing bearer"}})
            return
        if payload.get("model") != "stub-model":
            self._send(400, {"error": {"message": "unknown model"}})
            return
        body = {
            "choices": [{"message": {"content": json.dumps(_STUB_PLAN)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
        }
        self._send(200, body)

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _run_stub_server() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _request() -> LLMRequest:
    return LLMRequest(
        task_id=new_id(),
        facts=[
            CanonicalFact(
                id="FACT_NEXUS_AVAILABILITY",
                label="NEXUS reachability",
                value="available",
            )
        ],
        max_output_tokens=200,
    )


async def _smoke(provider: OpenAIProvider) -> int:
    try:
        resp = await provider.generate_structured(_request(), timeout_seconds=20.0)
    except LLMError as exc:
        print(f"SMOKE FAIL: {type(exc).__name__}: {exc}")
        return 1
    finally:
        await provider.close()
    plan = resp.structured_output
    print(
        f"SMOKE OK: provider={resp.provider} model={resp.model} "
        f"summary={plan.summary_key.value} facts={plan.selected_fact_ids} "
        f"latency_ms={resp.latency_ms}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="JARVIS real-LLM smoke test")
    parser.add_argument(
        "--stub",
        action="store_true",
        help="run against a local stub server instead of the real API (no key needed)",
    )
    args = parser.parse_args()

    if args.stub:
        server, base_url = _run_stub_server()
        provider = OpenAIProvider(model="stub-model", api_key="stub-key", base_url=base_url)
        try:
            return asyncio.run(_smoke(provider))
        finally:
            server.shutdown()

    provider_name = os.environ.get("JARVIS_LLM_PROVIDER", "fake")
    if provider_name != "openai":
        print(
            "SMOKE SKIP: set JARVIS_LLM_PROVIDER=openai to test the real provider "
            f"(current: {provider_name!r}). Nothing was called."
        )
        return 2
    model = os.environ.get("JARVIS_LLM_MODEL", "")
    api_key = os.environ.get("JARVIS_OPENAI_API_KEY", "")
    if not model or not api_key:
        print(
            "SMOKE SKIP: JARVIS_LLM_MODEL and JARVIS_OPENAI_API_KEY are required "
            "for the real-provider smoke. Nothing was called."
        )
        return 2
    provider = OpenAIProvider(model=model, api_key=api_key)
    return asyncio.run(_smoke(provider))


if __name__ == "__main__":
    raise SystemExit(main())
