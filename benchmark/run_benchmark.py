"""
Raven LoCoMo Benchmark — local HF inference, no Ollama/NIM required.

Usage:
    python3 -m benchmark.run_benchmark --mode standard --max-examples 3
    python3 -m benchmark.run_benchmark --mode both --max-examples 10
    python3 -m benchmark.run_benchmark --mode standard --max-examples 10 --judge kimi --kimi-api-key YOUR_KEY
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime


# ── inference backends ────────────────────────────────────────────────────────

def build_hf_fn(model_name: str = "Qwen/Qwen3.5-4B"):
    """Local HuggingFace model — no network needed after initial download."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    print(f"Loading {model_name}...")
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForCausalLM.from_pretrained(
        model_name, dtype=torch.bfloat16, device_map="auto"
    )
    model.eval()
    print("Model ready.")

    def fn(prompt: str) -> str:
        inputs = tokenizer(
            prompt, return_tensors="pt", truncation=True, max_length=4096
        )
        inputs = {k: v.to(model.device) for k, v in inputs.items()}
        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_new_tokens=256,
                pad_token_id=tokenizer.eos_token_id,
                do_sample=False,
            )
        text = tokenizer.decode(
            outputs[0][inputs["input_ids"].shape[1]:],
            skip_special_tokens=True,
        )
        return text.strip() or ""

    return fn


def build_kimi_fn(api_key: str):
    """Kimi K2.5 API — recommended for publishable judge results."""
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url="https://api.moonshot.ai/v1")

    def fn(prompt: str) -> str:
        import time
        for attempt in range(3):
            try:
                resp = client.chat.completions.create(
                    model="kimi-k2.5",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=512,
                    temperature=0.6,
                    extra_body={"chat_template_kwargs": {"thinking": False}},
                )
                return resp.choices[0].message.content or ""
            except Exception as e:
                if attempt < 2:
                    time.sleep((attempt + 1) * 5)
                else:
                    raise
        return ""

    return fn


# ── result persistence ────────────────────────────────────────────────────────

def save_results(result, output_dir: str):
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(output_dir, f"locomo_{result.mode}_{timestamp}.json")
    data = {
        "mode": result.mode,
        "model": result.model_name,
        "n_examples": result.n_examples,
        "overall_score": result.overall_score,
        "score_by_category": result.score_by_category,
        "n_questions": len(result.results),
        "results": [
            {
                "question": r.question,
                "answer": r.answer,
                "predicted": r.predicted,
                "category": r.category,
                "score": r.score,
                "latency_ms": r.latency_ms,
            }
            for r in result.results
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Results saved to {path}")
    return path


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Raven LoCoMo Benchmark")
    parser.add_argument("--mode", default="standard",
                        choices=["standard", "adversarial", "both"])
    parser.add_argument("--max-examples", type=int, default=3)
    parser.add_argument("--n-recent", type=int, default=10)
    parser.add_argument("--noise-ratio", type=float, default=0.2)
    parser.add_argument("--output", default="benchmark/results")
    parser.add_argument("--judge", default="local",
                        choices=["local", "kimi"],
                        help="local = same HF model; kimi = Kimi K2.5 API")
    parser.add_argument("--kimi-api-key",
                        default=os.environ.get("KIMI_API_KEY"))
    args = parser.parse_args()

    # Build inference function (always local HF)
    inference_fn = build_hf_fn("Qwen/Qwen3.5-4B")
    model_name = "Qwen3.5-4B-local"

    # Build judge function
    if args.judge == "kimi":
        if not args.kimi_api_key:
            print("Error: --kimi-api-key or KIMI_API_KEY env required for kimi judge")
            sys.exit(1)
        judge_fn = build_kimi_fn(args.kimi_api_key)
        print("Judge: Kimi K2.5 API (comparable to Mem0 methodology)")
    else:
        judge_fn = inference_fn  # same model, loaded once
        print("Judge: Qwen3.5-4B local (development mode)")

    # Load dataset
    print(f"\nLoading LoCoMo dataset (max {args.max_examples} examples)...")
    from benchmark.locomo_loader import load_locomo
    from benchmark.evaluator import run_evaluation

    examples = load_locomo(max_examples=args.max_examples)
    print(f"Loaded {len(examples)} examples")

    modes = ["standard", "adversarial"] if args.mode == "both" else [args.mode]

    for mode in modes:
        print(f"\n{'='*60}")
        print(f"Running {mode.upper()} evaluation")
        print(f"{'='*60}")

        n_recent = 3 if mode == "adversarial" else args.n_recent

        with tempfile.TemporaryDirectory() as tmp_dir:
            result = run_evaluation(
                examples=examples,
                mode=mode,
                inference_fn=inference_fn,
                judge_fn=judge_fn,
                model_name=model_name,
                tmp_dir=tmp_dir,
                n_recent=n_recent,
                noise_ratio=args.noise_ratio,
                verbose=True,
            )

        print(f"\n{result.summary()}")
        save_results(result, args.output)

    if args.mode == "both":
        print(f"\n{'='*60}")
        print("COMPARISON vs PUBLISHED RESULTS")
        print(f"{'='*60}")
        print(f"{'System':<25} {'Overall':>10} {'Temporal':>10}")
        print(f"{'-'*45}")
        print(f"{'OpenAI Memory':<25} {'52.9%':>10} {'21.7%':>10}")
        print(f"{'Zep':<25} {'66.0%':>10} {'-':>10}")
        print(f"{'Mem0':<25} {'67.1%':>10} {'58.1%':>10}")
        print(f"{'Raven (standard)':<25} {'see above':>10}")
        print(f"{'Raven (adversarial)':<25} {'see above':>10}")


if __name__ == "__main__":
    main()
