"""
src/audio/audio_engine.py

Lightweight synthesised audio feedback for trading events.

Sound categories
----------------
  tape     – public market trades (subtle, short beep)
  passive  – our own passive maker fills (chime / bell)
  hedge    – our own aggressive hedge orders (sharp click / alert)

Each category has a BUY variant (brighter, higher pitch) and a
SELL variant (darker, lower pitch).

Architecture
------------
  All waveforms are pre-synthesised at startup as numpy float32 arrays.
  A single daemon worker thread drains a bounded queue and plays sounds
  sequentially via sounddevice, so the caller (Qt main thread) never
  blocks.  If sounddevice or numpy are not installed the engine silently
  does nothing.

Throttle
--------
  Per-category minimum gap prevents audio flooding during high-activity
  periods.  Sounds that arrive faster than the threshold are dropped
  (latest wins if several stack up before the worker can drain).

Sound parameter table
---------------------
  key             freq (Hz)  dur (s)  waveform  envelope  vol
  tape_buy          880      0.040    sine      soft      0.28
  tape_sell         590      0.040    sine      soft      0.28
  passive_buy      1320      0.090    chime     bell      0.55
  passive_sell      880      0.090    chime     bell      0.55
  hedge_buy        1600      0.065    square    click     0.75
  hedge_sell        800      0.065    square    click     0.75
"""

import queue
import threading
import time
import logging
from typing import Optional

log = logging.getLogger("mm.audio")

_SAMPLE_RATE = 44100  # Hz

# ── optional dependencies ─────────────────────────────────────────────────────
try:
    import numpy as np
    _NP_OK = True
except ImportError:                       # pragma: no cover
    _NP_OK = False
    log.warning("numpy not installed – audio engine disabled")

try:
    import sounddevice as sd
    _SD_OK = True
except ImportError:                       # pragma: no cover
    _SD_OK = False
    if _NP_OK:
        log.warning("sounddevice not installed – audio engine disabled  "
                    "(pip install sounddevice)")


# ── waveform / envelope synthesis ────────────────────────────────────────────

def _synthesise(
    freq: float,
    duration: float,
    waveform: str,
    envelope: str,
    volume: float,
    sr: int = _SAMPLE_RATE,
) -> "np.ndarray":
    """Return a mono float32 PCM array for one sound event."""
    n = int(sr * duration)
    t = np.linspace(0.0, duration, n, endpoint=False)

    # ── waveform ──────────────────────────────────────────────────────────────
    if waveform == "sine":
        wave = np.sin(2.0 * np.pi * freq * t)

    elif waveform == "chime":
        # Inharmonic bell-like partial stack: fundamental + two stretched partials.
        # The stretched ratios (2.756, 5.404) mimic the inharmonicity of a real bell.
        partials = [
            (1.000, 1.00),
            (2.756, 0.50),
            (5.404, 0.25),
        ]
        wave = np.zeros(n)
        for ratio, amp in partials:
            wave += amp * np.sin(2.0 * np.pi * freq * ratio * t)
        mx = np.max(np.abs(wave))
        if mx > 0:
            wave /= mx

    elif waveform == "square":
        # Band-limited square (odd harmonics 1..15) — far less harsh than np.sign().
        wave = np.zeros(n)
        for k in range(1, 16, 2):
            wave += np.sin(2.0 * np.pi * freq * k * t) / k
        mx = np.max(np.abs(wave))
        if mx > 0:
            wave /= mx

    else:
        wave = np.sin(2.0 * np.pi * freq * t)

    # ── amplitude envelope ────────────────────────────────────────────────────
    if envelope == "soft":
        # Short linear attack + exponential decay → smooth beep
        env = np.exp(-t * 55.0)
        atk = max(1, int(n * 0.04))
        env[:atk] = np.linspace(0.0, 1.0, atk)

    elif envelope == "bell":
        # Very brief attack + moderate exponential decay → recognisable chime tail
        env = np.exp(-t * 22.0)
        atk = max(1, int(n * 0.01))
        env[:atk] = np.linspace(0.0, 1.0, atk)

    elif envelope == "click":
        # Near-instant attack + very fast decay → punchy alert click
        env = np.exp(-t * 90.0)
        atk = max(1, int(n * 0.005))
        env[:atk] = np.linspace(0.0, 1.0, atk)

    else:
        env = np.ones(n)

    return (wave * env * volume).astype(np.float32)


# ── sound parameter definitions ───────────────────────────────────────────────
#
#   key            ( freq,  dur,    waveform,  envelope, base_vol )
#
_SOUND_DEFS: dict = {
    # ── Public tape trades (subtle – can fire many times per second) ──────────
    "tape_buy":      (  880, 0.040, "sine",    "soft",   0.28),
    "tape_sell":     (  590, 0.040, "sine",    "soft",   0.28),

    # ── Our passive maker fills (chime – clearly ours) ────────────────────────
    "passive_buy":   ( 1320, 0.090, "chime",   "bell",   0.55),
    "passive_sell":  (  880, 0.090, "chime",   "bell",   0.55),

    # ── Our aggressive hedge orders (alert – highest priority) ────────────────
    "hedge_buy":     ( 1600, 0.065, "square",  "click",  0.75),
    "hedge_sell":    (  800, 0.065, "square",  "click",  0.75),
}

# Minimum milliseconds between successive plays within the same category.
# Prevents flooding; hedge is never throttled so urgent alerts always fire.
_THROTTLE_MS: dict = {
    "tape":    80.0,   # ≤ ~12 tape sounds/sec regardless of market activity
    "passive": 40.0,   # fills are rare; tiny guard in case of burst
    "hedge":    0.0,   # critical risk event – always fire
}


# ── AudioEngine ───────────────────────────────────────────────────────────────

class AudioEngine:
    """
    Thread-safe, non-blocking synthesised audio engine.

    Instantiate once at application start-up::

        engine = AudioEngine(enabled=True, volume=0.6)

    Then call the play_* methods from any thread (including the Qt main thread)::

        engine.play_tape_buy()
        engine.play_passive_sell()
        engine.play_hedge_buy()

    Call ``shutdown()`` when the application exits to cleanly stop the worker.
    """

    def __init__(self, enabled: bool = True, volume: float = 0.6) -> None:
        self._enabled = enabled and _NP_OK and _SD_OK
        self._volume  = max(0.0, min(1.0, float(volume)))

        if not self._enabled:
            log.info(
                "AudioEngine: disabled  (enabled=%s  numpy=%s  sounddevice=%s)",
                enabled, _NP_OK, _SD_OK,
            )
            return

        # Pre-synthesise all sounds scaled by the master volume.
        self._sounds: dict = {}
        for key, (freq, dur, waveform, envelope, base_vol) in _SOUND_DEFS.items():
            self._sounds[key] = _synthesise(
                freq, dur, waveform, envelope, base_vol * self._volume,
            )

        # Per-category last-played timestamp (ms, monotonic) for throttling.
        self._last_ms: dict = {}

        # Bounded playback queue.  Old items are silently dropped when full so
        # the caller never blocks even during a trade burst.
        self._queue: queue.Queue = queue.Queue(maxsize=8)

        self._worker = threading.Thread(
            target=self._drain,
            name="audio-worker",
            daemon=True,
        )
        self._worker.start()
        log.info("AudioEngine: ready  (volume=%.2f)", self._volume)

    # ── Public API ─────────────────────────────────────────────────────────────

    def play_tape_buy(self)     -> None: self._play("tape_buy",     "tape")
    def play_tape_sell(self)    -> None: self._play("tape_sell",    "tape")
    def play_passive_buy(self)  -> None: self._play("passive_buy",  "passive")
    def play_passive_sell(self) -> None: self._play("passive_sell", "passive")
    def play_hedge_buy(self)    -> None: self._play("hedge_buy",    "hedge")
    def play_hedge_sell(self)   -> None: self._play("hedge_sell",   "hedge")

    def shutdown(self) -> None:
        """Signal the worker thread to exit cleanly (call on application exit)."""
        if self._enabled:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _play(self, key: str, category: str) -> None:
        if not self._enabled:
            return

        # Throttle check
        now = time.monotonic() * 1000.0
        min_gap = _THROTTLE_MS.get(category, 50.0)
        if now - self._last_ms.get(category, 0.0) < min_gap:
            return
        self._last_ms[category] = now

        data = self._sounds.get(key)
        if data is None:
            return

        try:
            self._queue.put_nowait(data)
        except queue.Full:
            pass  # drop; prefer low latency over completeness

    def _drain(self) -> None:
        """Worker thread: plays one sound at a time, blocks between each.

        Uses sd.OutputStream.write() instead of sd.play()/sd.wait() because
        the global sd.play() is not reliably thread-safe on Windows (PortAudio
        WDM/WASAPI).  OutputStream is explicitly per-thread and safe.
        """
        while True:
            item = self._queue.get()
            if item is None:
                break
            try:
                # reshape to (n_frames, 1) – OutputStream always wants 2-D for mono
                data = item.reshape(-1, 1)
                with sd.OutputStream(
                    samplerate=_SAMPLE_RATE,
                    channels=1,
                    dtype="float32",
                ) as stream:
                    stream.write(data)
                    # stream.close() (on __exit__) drains the hardware buffer
            except Exception as exc:
                log.warning("AudioEngine: playback error: %s", exc)
