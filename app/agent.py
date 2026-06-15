from __future__ import annotations

import time
from dataclasses import dataclass

from structlog.contextvars import bind_contextvars
from . import metrics
from .mock_llm import FakeLLM
from .mock_rag import retrieve
from .pii import hash_user_id, summarize_text
from .tracing import langfuse_context, observe


@dataclass
class AgentResult:
    answer: str
    latency_ms: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    quality_score: float


class LabAgent:
    def __init__(self, model: str = "claude-sonnet-4-5") -> None:
        self.model = model
        self.llm_expensive = FakeLLM(model="claude-sonnet-4-5")
        self.llm_cheap = FakeLLM(model="gpt-4o-mini")

    @observe()
    def run(self, user_id: str, feature: str, session_id: str, message: str) -> AgentResult:
        started = time.perf_counter()
        docs = retrieve(message)
        
        # Simple routing logic for cost optimization:
        # Route simple QA queries under 30 characters to cheaper model 'gpt-4o-mini',
        # route complex summary and longer QA queries to premium model 'claude-sonnet-4-5'.
        if feature == "qa" and len(message) < 30:
            model_used = "gpt-4o-mini"
            llm = self.llm_cheap
        else:
            model_used = "claude-sonnet-4-5"
            llm = self.llm_expensive
            
        bind_contextvars(model=model_used)
        
        prompt = f"Feature={feature}\nDocs={docs}\nQuestion={message}"
        response = llm.generate(prompt)
        quality_score = self._heuristic_quality(message, response.text, docs)
        latency_ms = int((time.perf_counter() - started) * 1000)
        cost_usd = self._estimate_cost(response.usage.input_tokens, response.usage.output_tokens, model_used)

        langfuse_context.update_current_trace(
            user_id=hash_user_id(user_id),
            session_id=session_id,
            tags=["lab", feature, model_used],
        )
        langfuse_context.update_current_observation(
            metadata={"doc_count": len(docs), "query_preview": summarize_text(message)},
            usage_details={"input": response.usage.input_tokens, "output": response.usage.output_tokens},
        )

        metrics.record_request(
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            quality_score=quality_score,
        )

        return AgentResult(
            answer=response.text,
            latency_ms=latency_ms,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            cost_usd=cost_usd,
            quality_score=quality_score,
        )

    def _estimate_cost(self, tokens_in: int, tokens_out: int, model: str) -> float:
        if model == "gpt-4o-mini":
            input_cost = (tokens_in / 1_000_000) * 0.15
            output_cost = (tokens_out / 1_000_000) * 0.60
        else:
            input_cost = (tokens_in / 1_000_000) * 3
            output_cost = (tokens_out / 1_000_000) * 15
        return round(input_cost + output_cost, 6)

    def _heuristic_quality(self, question: str, answer: str, docs: list[str]) -> float:
        score = 0.5
        if docs:
            score += 0.2
        if len(answer) > 40:
            score += 0.1
        if question.lower().split()[0:1] and any(token in answer.lower() for token in question.lower().split()[:3]):
            score += 0.1
        if "[REDACTED" in answer:
            score -= 0.2
        return round(max(0.0, min(1.0, score)), 2)
