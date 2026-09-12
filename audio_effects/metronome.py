"""
Tempo map + click track rendering.

A tempo map is a list of tempo changes: (time_s, bpm, beats_per_bar). Each entry holds until the next one
(or the end of the song). Beats before the first entry are undefined (no clicks, no grid).

Because the app pre-renders the audio that QMediaPlayer plays, the metronome is simply mixed into that buffer
at the beat sample positions - it can never drift from the music, at any playback rate.
"""
from dataclasses import dataclass
import numpy as np


@dataclass(order=True)
class TempoChange:
    time_s: float
    bpm: float
    beats_per_bar: int

    def to_dict(self):
        return {"time_s": float(self.time_s), "bpm": float(self.bpm), "beats_per_bar": int(self.beats_per_bar)}

    @classmethod
    def from_dict(cls, d):
        return cls(float(d["time_s"]), float(d["bpm"]), int(d["beats_per_bar"]))


def beat_times(tempo_map, duration_s):
    """
    All beat positions implied by a tempo map.

    :param tempo_map:   iterable of TempoChange (any order)
    :param duration_s:  song length in seconds
    :return: (times_s, is_downbeat, bar_number, beat_in_bar) as numpy arrays (bar/beat numbers are 1-based)
    """
    changes = sorted((c for c in tempo_map if c.bpm > 0 and c.beats_per_bar > 0), key=lambda c: c.time_s)
    times, downbeat, bars, beats = [], [], [], []
    bar = 1
    for k, change in enumerate(changes):
        t_end = changes[k + 1].time_s if k + 1 < len(changes) else duration_s
        if t_end <= change.time_s:
            continue
        period = 60.0 / change.bpm
        n_beats = int(np.floor((t_end - change.time_s) / period - 1e-9)) + 1
        t = change.time_s + np.arange(n_beats) * period
        beat_idx = np.arange(n_beats) % change.beats_per_bar
        times.append(t)
        downbeat.append(beat_idx == 0)
        bars.append(bar + np.arange(n_beats) // change.beats_per_bar)
        beats.append(beat_idx + 1)
        bar += int(np.ceil(n_beats / change.beats_per_bar))      # a new tempo change always starts a new bar
    if not times:
        return (np.zeros(0), np.zeros(0, dtype=bool), np.zeros(0, dtype=int), np.zeros(0, dtype=int))
    return (np.concatenate(times), np.concatenate(downbeat), np.concatenate(bars), np.concatenate(beats))


def bar_beat_at(tempo_map, time_s):
    """(bar, beat) at a song time, or None when before the first tempo change / no tempo map."""
    changes = sorted((c for c in tempo_map if c.bpm > 0 and c.beats_per_bar > 0), key=lambda c: c.time_s)
    if not changes or time_s < changes[0].time_s:
        return None
    bar = 1
    for k, change in enumerate(changes):
        t_end = changes[k + 1].time_s if k + 1 < len(changes) else float("inf")
        if time_s < t_end:
            n = int(np.floor((time_s - change.time_s) * change.bpm / 60.0 + 1e-6))
            return bar + n // change.beats_per_bar, n % change.beats_per_bar + 1
        n_beats = int(np.floor((t_end - change.time_s) * change.bpm / 60.0 - 1e-9)) + 1
        bar += int(np.ceil(n_beats / change.beats_per_bar))
    return None


def _click(sample_rate, freq_hz, length_s=0.03, decay_s=0.008):
    n = int(sample_rate * length_s)
    t = np.arange(n) / sample_rate
    return np.sin(2 * np.pi * freq_hz * t) * np.exp(-t / decay_s)


def render_click_track(n_samples, sample_rate, beat_times_s, is_downbeat, gain=0.5,
                       accent_hz=1600.0, beat_hz=1000.0, full_scale=32767.0):
    """
    Mono float click track of length n_samples: an accented (higher) click on downbeats, a lower one otherwise.

    :param gain: click peak as a fraction of full scale
    """
    track = np.zeros(n_samples, dtype=np.float64)
    accent = _click(sample_rate, accent_hz) * gain * full_scale
    beat = _click(sample_rate, beat_hz) * gain * full_scale * 0.8
    starts = np.round(np.asarray(beat_times_s) * sample_rate).astype(np.int64)
    for start, down in zip(starts, is_downbeat):
        if start < 0 or start >= n_samples:
            continue
        click = accent if down else beat
        end = min(n_samples, start + click.shape[0])
        track[start:end] += click[:end - start]
    return track


def mix_click_track(audio, sample_rate, tempo_map, duration_s, rate=1.0, gain=0.5):
    """
    Return a copy of `audio` (int16, (n, ch)) with the metronome mixed in.

    :param tempo_map:   TempoChange list in *original* song time
    :param duration_s:  original song duration
    :param rate:        playback rate the audio was stretched to (beat times are scaled by 1/rate)
    :param gain:        click level as a fraction of full scale (0 disables)
    """
    if gain <= 0 or not tempo_map:
        return audio
    times, downbeat, _, _ = beat_times(tempo_map, duration_s)
    if times.shape[0] == 0:
        return audio
    track = render_click_track(audio.shape[0], sample_rate, times / rate, downbeat, gain=gain,
                               full_scale=float(np.iinfo(np.int16).max))
    mixed = audio.astype(np.float64) + track[:, None]
    return np.clip(mixed, np.iinfo(np.int16).min, np.iinfo(np.int16).max).astype(np.int16)
