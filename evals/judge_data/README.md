# Judge data and evaluation

Synthetic labelled texts and a scoring script for deciding whether a small local decision model (Laya, fine-tuned) can replace the
Ollama judge. Everything is fake: invented people and companies, 555-01xx phones, example.com emails, no credential shapes.

Pipeline (run from the repository root):

```bash
python evals/judge_data/plan.py                 # plan/batches.jsonl + one generation prompt per batch (taxonomy.py is the grid)
# generation: an agent per group of prompts writes raw/<batch>.jsonl (see the prompt's delivery section)
python evals/judge_data/merge.py                # validate, drop rule-breakers, dedupe, drop broken pairs -> data/all.jsonl
python evals/judge_data/split.py                # by scenario, stratified -> data/{train,dev,test}.jsonl
python evals/judge_data/label.py prepare        # independent second-pass labelling of dev, test and a train sample
python evals/judge_data/label.py collect        # disagreements are marked borderline (left out of training and of pass/fail)
python evals/judge_data/convert.py train        # Laya / Unsloth rows: {state, questions, expected}
python evals/judge_data/eval_judge.py score --backend ollama:gemma4:e4b-mlx --split dev --out scores/ollama-dev.json
python evals/judge_data/eval_judge.py score --backend laya:aac6fef/laya-mlx --split dev --out scores/laya-dev.json   # needs laya-mlx
python evals/judge_data/eval_judge.py report --split dev --baseline scores/ollama-dev.json --candidate scores/laya-dev.json
```

The test split is written once. `data/test.FROZEN` stops `split.py` from rebuilding it; score it only after the candidate threshold is
fixed on dev (`--candidate-threshold`). The hand-written cases in `evals/privacy_cases*.jsonl` are never trained on.

Files: `taxonomy.py` (question, categories, forms, lengths, batch counts), `plan.py` (prompts), `merge.py`, `split.py`, `label.py`,
`convert.py`, `eval_judge.py` (metrics, threshold choice, adoption criteria). `raw/`, `label/`, `scores/` and the training data are
regenerable and not committed.
