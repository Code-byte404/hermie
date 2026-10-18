# CredData benchmark

Scores Hermie's secret detection and four other scanners (gitleaks, trufflehog, detect-secrets, CredSweeper) on
[Samsung CredData](https://github.com/Samsung/CredData), with one scorer for all of them. This method was written and
committed before the first scoring run; a change to it after that is a new commit with the reason.

## Data

- CredData at commit `3ed5169f9d565b0bb6a90ede35006491b34b781f` (2026-10-17): 337 repos, 11,030 labelled data files
  (plus 535 license files the other tools also scan; their hits are ignored), 67,896 labelled rows (15,753 T,
  48,061 F, 4,082 X).
- Built with CredData's own `download_data.py` functions; kept outside this repo (`~/hermie-bench.noindex/`).
- Labels: T = credential (positive). F = not a credential. X = example or template value: negative, shown in its
  own column and also in a variant that counts it as a false positive (CredData's published convention).

## Split

`test` if `zlib.crc32(RepoName) % 2 == 1`, else `dev`, written once to `split.json`. Misses and false alarms are
read only on dev; rules may later be tuned only on dev; test numbers are reported, their rows are not read. One repo
(`2ba83c6a`, 5,165 T rows) falls in dev, so T rows are 12,054 dev / 3,699 test.

## Systems under test

- Hermie: the gate the proxy runs (`Gate.scan(text, Origin.TOOL)`, Presidio + rules, default config, judge off
  unless `--judge`), over each whole file as one tool-result string, a fresh empty mapping per file. A file the gate
  holds (judge, smuggling) or fails on (`detector_error`, fail closed) counts as flagged on every line.
- Other tools: current versions, default rules, no verification, no network; exact argv and versions in
  `tools_manifest.json`. trufflehog runs with deduplication off so repeated values are all reported.

## Scoring

- Unit: one labelled row. A tool flags a row when one of its findings overlaps it: column overlap when both the row
  (`ValueStart` / `ValueEnd`) and the finding have columns, line overlap otherwise. A line-only table is reported
  too. gitleaks' and trufflehog's columns come from finding their reported secret on the reported line.
- Headline: every CredData category; for Hermie only `SECRET` findings (plus held / detector-error files).
- Second lines: Hermie with every finding (any entity, what a user gets); Hermie's scope, which drops rows whose
  categories are all in `UUID`, `AWS S3 Bucket`, `Firebase Domain`, `Tencent WeChat API App ID`, `Nonce`, `Salt`.
- Precision = TP / (TP + FP), recall = TP / (TP + FN), F1; per split and per category.
- Flags on unlabelled lines are counted but are not false positives (see biases).
- Hermie value coverage: for each T row, is the value fully inside Hermie's placeholder spans (covered), partly
  (partial: a fragment would still leave), or not at all (missed). Exact spans (one line, both columns) are
  checked character by character. Inexact spans (no ValueEnd, several lines, or no columns, mostly PEM and JWK
  blocks) include quotes, indentation and key names, so only their 4+ character secret-like runs
  (`[A-Za-z0-9+/=_-]{4,}`) must be covered; the two kinds are reported apart.
- Hermie merges overlapping findings and keeps the entity of the one that starts first, so a PII finding that
  starts before a secret (for example an email match on `user:pass@host`) hides the `SECRET` from the headline;
  the every-finding line shows what is hidden in total.
- A run that failed, or a Hermie run that did not finish (files missing from `hermie_done.txt`), is named in a
  warning at the top of `summary.md`; Hermie hits of unfinished files are ignored.
- Line convention: lines split on `\n` (a trailing `\r` dropped). Checked on the data: for 99.31% of single-line T
  rows with columns, `line[ValueStart:ValueEnd]` is a non-empty value without whitespace; the rest are values that
  contain spaces (byte arrays, passphrases).

## Manual check of unlabelled flags

A seeded random sample (seed `20261017`, test split only) of unlabelled flagged lines: 100 for Hermie (`SECRET`
findings), 25 for each other tool. Pre-labelled by Claude, decided by a person; gives an estimate of precision on
lines CredData never labelled.

## Known biases

- Lines were labelled from CredSweeper's (and a few other tools') candidates; recall is measured only against
  secrets some tool once found, and unlabelled flags are unknown.
- CredSweeper's ML model is trained on CredData: its numbers are a ceiling, not a fair peer.
- Values are obfuscated (random strings of the original character set and length, example values for fixed
  formats, example key files).
- Source code only, not agent traffic.

## Commands

```bash
B=~/hermie-bench.noindex; R=$B/results/$(date +%F)
conda run -n hermie python -m evals.creddata.labels --meta $B/CredData/meta           # counts
conda run -n hermie python -m evals.creddata.run_tools --cred-root $B/CredData --out $R
conda run -n hermie python -m evals.creddata.run_hermie --cred-root $B/CredData --out $R --workers 8
conda run -n hermie python -m evals.creddata.score --cred-root $B/CredData --run $R
```

Outputs in `$R`: `summary.md`, `dev_misses.md`, `dev_false_alarms.md`, `manual_sample.csv`, raw tool output,
normalized hits, manifests. They quote third-party code and are not committed.
