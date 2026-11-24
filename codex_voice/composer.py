"""Text generators share one interface; only the selected provider is started."""
from .harness import Harness
from .codex import CodexComposer


def create_composer(cfg):
    return CodexComposer(cfg) if cfg["text_provider"] == "codex" else Harness(cfg)
