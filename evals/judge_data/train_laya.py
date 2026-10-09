"""Fine-tune a Laya checkpoint on the judge data (Apple Silicon: PyTorch MPS; needs `pip install laya`, which pulls torch).

    python evals/judge_data/convert.py train --out /tmp/train.jsonl
    python evals/judge_data/convert.py dev --out /tmp/dev.jsonl
    huggingface-cli download convaiinnovations/laya --local-dir laya_base
    python evals/judge_data/train_laya.py --base laya_base --data /tmp/train.jsonl --eval /tmp/dev.jsonl --out laya_judge

About 70 minutes for 3 epochs on an M2 Pro (3,700 rows, 16 GB of unified memory is enough with the defaults). The result loads with
`laya_mlx.load(OUT)`, so `hermie serve --judge laya:OUT` can use it. Check the licence of the base checkpoint before sharing the weights.
"""
from __future__ import annotations

import argparse
import json
import time


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="laya_base", help="local directory of the base checkpoint (convaiinnovations/laya)")
    p.add_argument("--data", required=True, help="training rows, from convert.py")
    p.add_argument("--eval", help="held-out rows for a before / after table")
    p.add_argument("--out", required=True)
    p.add_argument("--epochs", type=int, default=3)
    p.add_argument("--micro-batch", type=int, default=2)
    p.add_argument("--grad-accum", type=int, default=16)
    p.add_argument("--max-len", type=int, default=1024)
    p.add_argument("--device", default="mps")
    a = p.parse_args(argv)
    from laya.train import TrainConfig, finetune
    cfg = TrainConfig(epochs=a.epochs, micro_batch=a.micro_batch, grad_accum=a.grad_accum, max_len=a.max_len, head_max_len=256,
                      gradient_checkpointing=True, eval_data=a.eval, log_every=10, seed=42)
    t = time.time()
    summary = finetune(a.data, a.base, a.out, cfg, device=a.device)
    print("seconds", round(time.time() - t))
    print(json.dumps({k: summary[k] for k in ("train_items", "calibration_items", "eval_items", "epoch_loss", "temperature") if k in summary}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
