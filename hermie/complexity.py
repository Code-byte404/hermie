"""RouteLLM's BERT router: loads the weights directly with transformers and runs fully locally.

We do not import routellm: its mf / sw_ranking routers call OpenAI to generate embeddings,
and routellm.routers creates an OpenAI client at import time. This only replicates the logic of
BERTRouter.calculate_strong_win_rate.

The weights were trained on mostly-English Chatbot Arena data; how well they discriminate on Chinese
needs to be measured. Set ROUTELLM_ENABLED=false if it performs poorly.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

log = logging.getLogger(__name__)


class RouteLLMScorer:
    def __init__(self, checkpoint: str = "routellm/bert_gpt4_augmented"):
        self.checkpoint = checkpoint
        self._model = None
        self._tok = None
        self._lock = threading.Lock()
        self._broken = False

    def _load(self):
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        from transformers.utils import logging as hf_logging
        hf_logging.disable_progress_bar()
        hf_logging.set_verbosity_error()
        self._model = AutoModelForSequenceClassification.from_pretrained(self.checkpoint, num_labels=3)
        self._model.eval()
        self._tok = AutoTokenizer.from_pretrained(self.checkpoint)

    def strong_win_rate(self, prompt: str) -> Optional[float]:
        """Probability that "the strong model is clearly better"; returns None on error (the policy ignores the signal)."""
        if self._broken:
            return None
        try:
            import numpy as np
            import torch
            with self._lock:
                if self._model is None:
                    self._load()
            inputs = self._tok(prompt, return_tensors="pt", padding=True, truncation=True)
            with torch.no_grad():
                logits = self._model(**inputs).logits.numpy()[0]
            p = np.exp(logits - logits.max())
            p /= p.sum()
            return float(1 - p[-2:].sum())  # same as the original RouteLLM implementation
        except Exception:
            log.exception("RouteLLM scoring failed; the signal is disabled for the rest of this process")
            self._broken = True
            return None
