"""Central configuration. Reads .env from the project root at startup; every value can be overridden by an environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# No cloud observability (Logfire etc.), and no pydantic-ai promo banner (it corrupts the full-screen UI)
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
# Disable the Hugging Face / transformers tqdm progress bars: creating their multiprocessing lock fails inside a
# Textual background thread, and the progress output corrupts the full-screen UI
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TQDM_DISABLE", "1")

try:  # .env is read locally only and never seen by the executor (the environment inside the sandbox is cleared)
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> Optional[list[str]]:
    """Comma list; None when the variable is unset (callers treat None as "auto")."""
    v = os.environ.get(name)
    return None if v is None else [x.strip() for x in v.split(",") if x.strip()]


class RunMode(str, Enum):
    DEFAULT = "default"      # high-risk actions ask for confirmation
    AUTO = "auto"            # skip-permissions: only skips approvals, boundaries unchanged
    NO_SANDBOX = "no_sandbox"  # DANGEROUS: Seatbelt off, the executor can access the network

    @property
    def label(self) -> str:
        return {"default": "default mode", "auto": "auto mode", "no_sandbox": "no-sandbox mode"}[self.value]


CLOUD_PROVIDERS = {"deepseek": "DeepSeek", "openai": "OpenAI", "anthropic": "Anthropic",
                   "openai-compatible": "OpenAI-compatible"}
# (cloud direct model, planner model) when CLOUD_MODEL / CLOUD_PLAN_MODEL are not set; other providers must set them
CLOUD_DEFAULT_MODELS = {"deepseek": ("deepseek-v4-flash", "deepseek-v4-pro"),
                        "anthropic": ("claude-sonnet-5", "claude-opus-5-5")}


@dataclass
class Settings:
    # ---- Ollama (executor + judge model) ----
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434"))
    worker_model: str = field(default_factory=lambda: _env("WORKER_MODEL", "qwen3.8:27b-mlx"))
    judge_model: str = field(default_factory=lambda: _env("JUDGE_MODEL", "qwen3.8:27b-mlx"))
    # How many samples per judge question; the vote ratio approximates probability and confidence (1 = fastest, no confidence)
    judge_samples: int = field(default_factory=lambda: _env_int("JUDGE_SAMPLES", 3))
    judge_disable_thinking: bool = field(default_factory=lambda: _env_bool("JUDGE_DISABLE_THINKING", True))
    # Routing asks its three questions (task type, difficulty, needs workspace) in ONE judge request per sample instead
    # of one request each (the privacy gate's contextual question stays separate); false = one request per question
    judge_batch: bool = field(default_factory=lambda: _env_bool("JUDGE_BATCH", True))
    judge_max_chars: int = field(default_factory=lambda: _env_int("JUDGE_MAX_CHARS", 6000))
    judge_timeout_s: float = field(default_factory=lambda: _env_float("JUDGE_TIMEOUT", 120))
    # How long the judge model stays resident in Ollama: avoids reloading on every switch when it differs from the executor model
    judge_keep_alive: str = field(default_factory=lambda: _env("JUDGE_KEEP_ALIVE", "30m"))
    # Per-request model timeout (seconds). A timeout triggers the local/cloud fallback instead of waiting forever; the client does not retry
    worker_timeout_s: float = field(default_factory=lambda: _env_float("WORKER_TIMEOUT", 600))
    cloud_timeout_s: float = field(default_factory=lambda: _env_float("CLOUD_TIMEOUT", 300))
    # Time limit for one executor run (one delegation or one local-only task), not counting time spent waiting for the
    # user's approval or input: on timeout it reports "partially done" so the session never hangs. A 27B model on a
    # laptop needs minutes per reply, so this is generous; the UI shows the step's progress and warns at 80%
    executor_run_timeout_s: float = field(default_factory=lambda: _env_float("EXECUTOR_RUN_TIMEOUT", 3600))
    # How often a running executor step reports its progress to the UI (seconds)
    progress_every_s: float = field(default_factory=lambda: _env_float("PROGRESS_EVERY", 5))
    # Time limit for history compression (local model); on timeout fall back to deterministic truncation
    compress_timeout_s: float = field(default_factory=lambda: _env_float("COMPRESS_TIMEOUT", 300))
    # Whether the executor runs in thinking mode (more accurate but slower)
    worker_thinking: bool = field(default_factory=lambda: _env_bool("WORKER_THINKING", False))

    # ---- Cloud model (planner + cloud direct; receives CleanText only) ----
    # deepseek (default) / openai / anthropic / openai-compatible (any OpenAI-style endpoint via CLOUD_BASE_URL:
    # OpenRouter, Moonshot, Qwen, Gemini's compatible endpoint, ...). The legacy DEEPSEEK_* variables still work.
    cloud_provider: str = field(default_factory=lambda: _env("CLOUD_PROVIDER", "deepseek").strip().lower())
    cloud_api_key: str = field(default_factory=lambda: _env("CLOUD_API_KEY", "") or _env("DEEPSEEK_API_KEY", ""))
    cloud_base_url: str = field(default_factory=lambda: _env("CLOUD_BASE_URL", "").strip())
    cloud_model: str = field(default_factory=lambda: _env("CLOUD_MODEL", "") or _env("DEEPSEEK_MODEL", ""))
    cloud_plan_model: str = field(default_factory=lambda: _env("CLOUD_PLAN_MODEL", "") or _env("DEEPSEEK_PLAN_MODEL", ""))

    # ---- Controlled web access (runs in the main process, with the outbound check; commands in the sandbox can also reach the network) ----
    web_enabled: bool = field(default_factory=lambda: _env_bool("WEB_ENABLED", True))
    tavily_api_key: str = field(default_factory=lambda: _env("TAVILY_API_KEY", ""))
    web_allowed_domains: tuple = field(default_factory=lambda: tuple(
        d.strip().lower() for d in _env("WEB_ALLOWED_DOMAINS", "").split(",") if d.strip()))
    web_timeout_s: float = field(default_factory=lambda: _env_float("WEB_TIMEOUT", 30))
    web_fetch_max_chars: int = field(default_factory=lambda: _env_int("WEB_FETCH_MAX_CHARS", 12000))
    web_fetch_max_bytes: int = field(default_factory=lambda: _env_int("WEB_FETCH_MAX_BYTES", 3_000_000))

    # ---- Mac toolchain: Xcode / simctl / AXe run in the sandbox via run_command; the screenshot tool runs in the main process ----
    mac_tools: bool = field(default_factory=lambda: _env_bool("MAC_TOOLS", True))
    screenshot_mac: bool = field(default_factory=lambda: _env_bool("SCREENSHOT_MAC", True))   # whole-display capture (taints the task)
    screenshot_max_px: int = field(default_factory=lambda: _env_int("SCREENSHOT_MAX_PX", 1024))  # longest side shown to the model

    # ---- RouteLLM (local BERT router) ----
    routellm_enabled: bool = field(default_factory=lambda: _env_bool("ROUTELLM_ENABLED", True))
    routellm_checkpoint: str = field(default_factory=lambda: _env("ROUTELLM_CHECKPOINT", "routellm/bert_gpt4_augmented"))
    routellm_threshold: float = field(default_factory=lambda: _env_float("ROUTELLM_THRESHOLD", 0.5))

    # ---- Privacy ----
    presidio_score_threshold: float = field(default_factory=lambda: _env_float("PRESIDIO_THRESHOLD", 0.5))
    sensitive_entities: tuple = ("CN_ID_CARD", "CN_MOBILE", "BANK_CARD", "EMAIL_ADDRESS",
                                 "IP_ADDRESS", "PERSON", "CUSTOM_KEYWORD", "SECRET")
    custom_keywords: tuple = field(default_factory=lambda: tuple(
        k.strip() for k in _env("SENSITIVE_KEYWORDS", "").split(",") if k.strip()))
    contextual_privacy_threshold: float = field(default_factory=lambda: _env_float("CONTEXT_PRIVACY_THRESHOLD", 0.3))

    # ---- Routing policy ----
    min_confidence: float = field(default_factory=lambda: _env_float("MIN_CONFIDENCE", 0.6))
    verify_threshold: float = field(default_factory=lambda: _env_float("VERIFY_THRESHOLD", 0.6))
    # needs_workspace vote share above which a task counts as "must produce files" (see router); tuned by --calibrate
    needs_workspace_threshold: float = field(default_factory=lambda: _env_float("NEEDS_WORKSPACE_THRESHOLD", 0.3))
    # --calibrate --apply refuses to write .env below this many labelled tasks
    calibrate_min_tasks: int = field(default_factory=lambda: _env_int("CALIBRATE_MIN_TASKS", 30))

    # ---- Execution environment ----
    # Default = current directory (like Claude Code); the --workspace flag wins; the WORKSPACE env var is only a fallback for library use
    workspace: Path = field(default_factory=lambda: Path(_env("WORKSPACE", "") or Path.cwd()).expanduser())
    data_dir: Path = field(default_factory=lambda: Path(_env("HERMIE_DATA_DIR", "~/.hermie")).expanduser())
    mode: RunMode = field(default_factory=lambda: RunMode(_env("RUN_MODE", "default")))
    command_timeout_s: float = field(default_factory=lambda: _env_float("COMMAND_TIMEOUT", 120))
    # A command silent this long on what looks like a prompt is waiting for input: the user is asked (default mode),
    # otherwise its stdin is closed. Time spent waiting for the user does not count toward COMMAND_TIMEOUT
    command_idle_s: float = field(default_factory=lambda: _env_float("COMMAND_IDLE", 8))
    # Snapshots kept per workspace (pre-task snapshots + one before each delegation in plan mode); older ones are pruned automatically
    snapshot_keep: int = field(default_factory=lambda: _env_int("SNAPSHOT_KEEP", 20))
    # File names the executor may not read even inside the workspace (glob, matched on the file name): private keys, certificates, credential files.
    # Deliberately excludes .env: the project's own build/run commands need it; secrets in .env are caught at the outbound gate by the SECRET recognizer
    sandbox_deny_names: tuple = field(default_factory=lambda: tuple(
        n.strip() for n in _env("SANDBOX_DENY_NAMES",
                                "id_rsa,id_ed25519,id_ecdsa,id_dsa,.netrc,*.pem,*.key,*.p12,*.pfx,*.p8").split(",")
        if n.strip()))
    max_tool_calls: int = field(default_factory=lambda: _env_int("MAX_TOOL_CALLS", 30))
    max_requests: int = field(default_factory=lambda: _env_int("MAX_REQUESTS", 40))
    max_delegations: int = field(default_factory=lambda: _env_int("MAX_DELEGATIONS", 8))
    # ---- Plan mode design phase: questions, a detailed plan, approval before execution ----
    plan_design: bool = field(default_factory=lambda: _env_bool("PLAN_DESIGN", True))
    plan_max_steps: int = field(default_factory=lambda: _env_int("PLAN_MAX_STEPS", 20))
    plan_max_question_rounds: int = field(default_factory=lambda: _env_int("PLAN_MAX_QUESTION_ROUNDS", 3))
    plan_max_revisions: int = field(default_factory=lambda: _env_int("PLAN_MAX_REVISIONS", 3))
    # AUTO mode: seconds before the planner's questions are answered with the recommended options
    plan_auto_answer_s: float = field(default_factory=lambda: _env_float("PLAN_AUTO_ANSWER_S", 120))
    plan_file: str = field(default_factory=lambda: _env("PLAN_FILE", "PLAN.md"))
    # Upper bound on delegations for a large plan (the budget is max(MAX_DELEGATIONS, 2 x steps), capped here)
    plan_delegation_cap: int = field(default_factory=lambda: _env_int("PLAN_DELEGATION_CAP", 40))
    report_retries: int = field(default_factory=lambda: _env_int("REPORT_RETRIES", 3))
    stuck_check_every: int = field(default_factory=lambda: _env_int("STUCK_CHECK_EVERY", 6))
    tool_output_max_chars: int = field(default_factory=lambda: _env_int("TOOL_OUTPUT_MAX_CHARS", 8000))
    # When the executor's conversation history exceeds this many characters, compress it with the local model (never the cloud)
    history_compress_chars: int = field(default_factory=lambda: _env_int("HISTORY_COMPRESS_CHARS", 24000))
    # Files dropped into the input box are attached as material: per-file and total character caps (directories attach a file tree only)
    attach_max_file_chars: int = field(default_factory=lambda: _env_int("ATTACH_MAX_FILE_CHARS", 200_000))
    attach_max_total_chars: int = field(default_factory=lambda: _env_int("ATTACH_MAX_TOTAL_CHARS", 400_000))

    # ---- Voice (all local: sounddevice recording, mlx-whisper transcription, macOS say for speech) ----
    voice_output: bool = field(default_factory=lambda: _env_bool("VOICE_OUTPUT", False))
    voice_name: str = field(default_factory=lambda: _env("VOICE_NAME", ""))       # say -v; empty = system default
    voice_rate: int = field(default_factory=lambda: _env_int("VOICE_RATE", 0))    # say -r; 0 = not passed
    whisper_model: str = field(default_factory=lambda: _env("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo"))
    voice_max_seconds: float = field(default_factory=lambda: _env_float("VOICE_MAX_SECONDS", 60))
    voice_key: str = field(default_factory=lambda: _env("VOICE_KEY", "f5").lower())  # record key: f3-f12 or ctrl+letter
    # /model and /voice changes made in the UI are written back to this file
    env_path: Path = field(default_factory=lambda: PROJECT_ROOT / ".env")

    # ---- Self-verification loop (all local) ----
    # Reporting done after writing files without any verification step (no command run, no file read back afterwards): require verification first
    verify_required: bool = field(default_factory=lambda: _env_bool("VERIFY_REQUIRED", True))
    # Max rounds the executor gets to fix things when the local reviewer (sees the workspace diff and command output) fails it; 0 = review off
    verify_rounds: int = field(default_factory=lambda: _env_int("VERIFY_ROUNDS", 2))
    review_timeout_s: float = field(default_factory=lambda: _env_float("REVIEW_TIMEOUT", 300))
    review_diff_chars: int = field(default_factory=lambda: _env_int("REVIEW_DIFF_CHARS", 12000))
    # Before plan mode starts, run a deterministic local recon (directory, toolchain, AGENT.md) and tell the planner after the gate
    recon_enabled: bool = field(default_factory=lambda: _env_bool("RECON_ENABLED", True))
    # When a task has a "review failed, then fixed successfully" episode, have the local model write a lesson into AGENT.md
    lessons_enabled: bool = field(default_factory=lambda: _env_bool("LESSONS_ENABLED", True))
    # Lesson memory (all local): lessons are embedded by this Ollama model and the top-k most relevant are injected
    # before each executor run; lessons from the same workspace always qualify, others need LESSONS_MIN_SIM
    lesson_embed_model: str = field(default_factory=lambda: _env("LESSON_EMBED_MODEL", "nomic-embed-text"))
    lessons_top_k: int = field(default_factory=lambda: _env_int("LESSONS_TOP_K", 5))
    lessons_min_sim: float = field(default_factory=lambda: _env_float("LESSONS_MIN_SIM", 0.55))
    # Skill library (all local): Markdown playbooks distilled from reviewed multi-step successes, confirmed by a second
    # similar success or /skills approve, recalled for similar steps, retired when they stop helping
    skills_enabled: bool = field(default_factory=lambda: _env_bool("SKILLS_ENABLED", True))
    skills_top_k: int = field(default_factory=lambda: _env_int("SKILLS_TOP_K", 2))
    skills_min_sim: float = field(default_factory=lambda: _env_float("SKILLS_MIN_SIM", 0.6))
    skill_min_tool_calls: int = field(default_factory=lambda: _env_int("SKILL_MIN_TOOL_CALLS", 4))
    skill_merge_sim: float = field(default_factory=lambda: _env_float("SKILL_MERGE_SIM", 0.8))
    skill_retire_uses: int = field(default_factory=lambda: _env_int("SKILL_RETIRE_USES", 5))
    skill_retire_rate: float = field(default_factory=lambda: _env_float("SKILL_RETIRE_RATE", 0.3))

    # Data connectors (read-only business data, processed by local models only; see hermie/connectors/).
    # CONNECTORS unset = auto: asc when the asc CLI is installed. An empty value turns connectors off.
    connectors: Optional[list[str]] = field(default_factory=lambda: _env_list("CONNECTORS"))
    asc_path: str = field(default_factory=lambda: _env("ASC_PATH", ""))
    connector_timeout: float = field(default_factory=lambda: _env_float("CONNECTOR_TIMEOUT", 180))
    connector_preview_chars: int = field(default_factory=lambda: _env_int("CONNECTOR_PREVIEW_CHARS", 4000))
    connector_data_keep_days: int = field(default_factory=lambda: _env_int("CONNECTOR_DATA_KEEP_DAYS", 7))

    def __post_init__(self) -> None:
        if self.cloud_provider not in CLOUD_PROVIDERS:
            raise ValueError(f"CLOUD_PROVIDER must be one of {', '.join(CLOUD_PROVIDERS)}, not {self.cloud_provider!r}")
        default_model, default_plan = CLOUD_DEFAULT_MODELS.get(self.cloud_provider, ("", ""))
        self.cloud_model = self.cloud_model or default_model
        self.cloud_plan_model = self.cloud_plan_model or default_plan

    @property
    def cloud_label(self) -> str:
        """How the cloud side is named in the UI: the provider, or the host for an OpenAI-compatible endpoint."""
        if self.cloud_provider == "openai-compatible":
            from urllib.parse import urlparse
            return urlparse(self.cloud_base_url).hostname or "OpenAI-compatible"
        return CLOUD_PROVIDERS[self.cloud_provider]

    @property
    def audit_log_path(self) -> Path:
        return self.data_dir / "audit.jsonl"

    @property
    def outbound_log_path(self) -> Path:
        return self.data_dir / "outbound.jsonl"

    @property
    def command_log_path(self) -> Path:
        return self.data_dir / "commands.jsonl"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def review_log_path(self) -> Path:
        return self.data_dir / "reviews.jsonl"

    @property
    def trajectory_log_path(self) -> Path:
        return self.data_dir / "trajectories.jsonl"

    @property
    def lessons_path(self) -> Path:
        return self.data_dir / "lessons.jsonl"

    @property
    def skills_dir(self) -> Path:
        return self.data_dir / "skills"

    @property
    def connector_dir(self) -> Path:
        return self.data_dir / "connectors"

    @property
    def connector_rooms_dir(self) -> Path:
        return self.connector_dir / "rooms"

    @property
    def connector_log_path(self) -> Path:
        return self.data_dir / "connectors.jsonl"

    def ensure_dirs(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)


def update_env(path: Path, values: dict[str, str]) -> None:
    """Write KEY=value pairs back to .env: existing lines are replaced in place (comments and order untouched),
    missing keys are appended at the end."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    out = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in remaining.items()]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
