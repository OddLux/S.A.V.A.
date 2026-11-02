"""dp-265: perceptual (dB taper) volume law for every 0-100 volume control.

Master volume, per-track volume and the ArtNet master fader all hand the
engine an int 0-100 "fader position". It used to be applied as a linear gain
(position / 100), which puts most of the audible range in the bottom 10% of
travel: 10% was already -20 dB, while 50% -> 100% was only +6 dB.

Now the position maps linearly onto decibels, like a mixing-desk fader:
100 = 0 dB (unity), 1 = just above MIN_DB, 0 = hard mute. Saved per-track
volumes are deliberately NOT migrated (user decision) - a stored 70 now means
-18 dB instead of -3 dB.
"""

MIN_DB = -60.0


def slider_to_gain(value):
    """Return the linear gain (0.0-1.0) for a 0-100 fader position."""
    position = max(0.0, min(float(value), 100.0))
    if position <= 0.0:
        return 0.0
    gain_db = MIN_DB * (1.0 - position / 100.0)
    return 10.0 ** (gain_db / 20.0)


def db_to_slider(gain_db):
    """Return the 0-100 fader position (float) that produces `gain_db`.

    Inverse of `slider_to_gain`. Anything at or below MIN_DB maps to 0
    (mute); anything at or above 0 dB maps to 100 (the fader cannot boost).
    """
    if gain_db >= 0.0:
        return 100.0
    if gain_db <= MIN_DB:
        return 0.0
    return 100.0 * (1.0 - gain_db / MIN_DB)
