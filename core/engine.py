"""AudioEngine singleton entry point.

dp-216 Phase 5: the pygame.mixer.music single-stream engine is retired.
`engine` is now a `core.deck_engine.DeckEngine` -- a sounddevice + numpy
dual-deck streaming mixer. This module stays as the stable import surface
(`from core.engine import engine, STATE_PLAYING, STATE_PAUSED,
STATE_STOPPED`) so `ui/main_window.py` and the ArtNet dispatch table needed
no import-path change for the swap.
"""

from core.deck_engine import (
    DeckEngine,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_STOPPED,
)

engine = DeckEngine()
