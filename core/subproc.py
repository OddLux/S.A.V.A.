"""Windows-safe subprocess launch flags for a *windowed* (no-console) app.

dp-216 Phase 6. Every ffmpeg/ffprobe launch in SAVA goes through here. Two
things go wrong otherwise, and both only manifest in the frozen PyInstaller
build -- running from source hides them completely, which is why they survived
Phases 1-5:

1. **Inherited stdin is an invalid handle.** A windowed build has no console,
   so the parent's std handles are not real files. `subprocess` on Windows
   switches to STARTF_USESTDHANDLES as soon as *any* of stdin/stdout/stderr is
   specified, and then passes the parent's broken stdin straight through.
   ffmpeg polls stdin for interactive commands, blocks on the bad handle and
   emits zero PCM -- the deck's frontier never moves, `play()` burns its whole
   prebuffer timeout, and playback is silent with a frozen position marker.
   Always pass `stdin=DEVNULL` (and `-nostdin` to ffmpeg for belt and braces).

2. **A console app launched from a GUI parent gets a console window.** Windows
   allocates one per launch, which flashes on screen. Track change, playlist
   reorder and the crossfade dialog each trigger a preload -> one ffprobe +
   one ffmpeg -> two flashes. CREATE_NO_WINDOW suppresses it.
"""

import subprocess

#: Suppress the console window Windows allocates for a console subprocess
#: launched from a GUI process. 0 on non-Windows, where the flag does not
#: exist and none is needed.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def popen_kwargs(**overrides):
    """Return the baseline kwargs every SAVA subprocess launch must use.

    Callers add their own `stdout`/`stderr`; `stdin` and `creationflags`
    should be left alone unless there is a specific reason.
    """
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "creationflags": CREATE_NO_WINDOW,
    }
    kwargs.update(overrides)
    return kwargs
