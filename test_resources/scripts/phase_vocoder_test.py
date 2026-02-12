import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt

from time import time
from typing import Literal

import numpy as np
from numba import njit, prange
from scipy.fft import fft, ifft
from scipy.signal import ShortTimeFFT
from scipy.signal.windows import gaussian   #, hann
# from scipy.interpolate import interp1d, CubicSpline

from librosa import load
from pydub import AudioSegment
from pydub.playback import play

from pedalboard.io import AudioFile


def float_to_int16(x):
    x = np.clip(x, -1.0, 1.0)
    return (x * 32767).astype(np.int16)


def export_mp3(filename, audio_float, sample_rate):
    # Ensure shape = (samples, channels)
    if audio_float.ndim == 1:
        audio_float = audio_float[:, None]

    audio_int16 = float_to_int16(audio_float)

    segment = AudioSegment(
        audio_int16.tobytes(),
        frame_rate=sample_rate,
        sample_width=2,  # 2 bytes = 16-bit
        channels=audio_int16.shape[1]
    )

    segment.export(filename, format="mp3", bitrate="192k")


@njit(cache=True)
def hann(M, sym=True):
    if int(M) != M or M < 0:
        raise ValueError('Window length M must be a non-negative integer')

    if M <= 1:
        return np.ones(M)

    if not sym:
        M += 1

    fac = np.linspace(-np.pi, np.pi, M)
    w = np.zeros(M)
    for k in range(2):
        w += 0.5 * np.cos(k * fac)

    if sym:
        return w
    else:
        return w[:-1]


@njit(cache=True)
def generate_window(input_samples, k_window, window_weights, h_anl):
    n_samples, n_channels = input_samples.shape
    n_samples_win = window_weights.shape[0]
    n_diff = int(n_samples_win / 2)
    x_win = np.zeros(shape=(n_samples_win, n_channels), dtype="float")

    window_anl_center = int(k_window * h_anl)
    k_0 = window_anl_center - n_diff
    k_1 = window_anl_center + n_diff
    k_anl_min = max(0, k_0)
    k_anl_max = min(n_samples, k_1)
    window_samples = input_samples[k_anl_min:k_anl_max]
    if np.allclose(window_samples, np.zeros_like(window_samples)):
        return x_win

    k_w0 = 0
    k_w1 = n_samples_win
    if k_0 < k_anl_min:
        k_w0 = n_samples_win - k_1
        k_w1 = n_samples_win
    if k_1 > k_anl_max:
        k_w0 = 0
        k_w1 = k_anl_max - k_anl_min
    x_win[k_w0:k_w1] = window_samples
    x_win = (x_win * window_weights).astype("float")
    return x_win


@njit(cache=True)
def _overlap_add(input_samples, sample_rate=44100, rate=1.0, dt_anl=0.1, h_scale=2):
    n_samples, n_channels = input_samples.shape
    dtype = input_samples.dtype
    stretch_factor = 1 / rate

    n_samples_win = int(dt_anl * sample_rate)
    n_samples_syn = int(np.ceil(n_samples * stretch_factor)) - 1
    n_samples, n_channels = input_samples.shape

    h_syn = int(n_samples_win / h_scale)
    h_anl = int(h_syn / stretch_factor)
    n_windows = int(np.ceil((input_samples.shape[0] - 1) / h_anl))
    win_weights = hann(n_samples_win).reshape(-1, 1)

    n_diff = int(n_samples_win / 2)
    output_audio_array = np.zeros((n_samples_syn, n_channels), dtype=dtype)
    for k_w in range(n_windows):
        x_win = generate_window(input_samples, k_window=k_w, window_weights=win_weights, h_anl=h_anl)
        if np.allclose(x_win, np.zeros_like(x_win)):
            continue
        window_syn_center = int(k_w * h_syn)
        k_s0 = window_syn_center - n_diff
        if k_s0 > n_samples_syn:
            # Synthesis window is outside the audio file data support.
            continue
        k_s1 = window_syn_center + n_diff
        k_syn_min = max(0, k_s0)
        k_syn_max = min(n_samples_syn, k_s1)
        k_ws0 = 0
        k_ws1 = n_samples_win
        if k_s0 < k_syn_min:
            k_ws0 = n_samples_win - k_s1
            k_ws1 = n_samples_win
        if k_s1 > k_syn_max:
            k_ws0 = 0
            k_ws1 = k_syn_max - k_syn_min
        output_audio_array[k_syn_min:k_syn_max] += x_win[k_ws0:k_ws1]
    return output_audio_array


@njit(cache=True)
def _wrap_to_pi(x):
    # wrap to [-pi, pi]
    return (x + np.pi) % (2.0 * np.pi) - np.pi


@njit(cache=True)
def peak_normalize(y, headroom_db=1.0):
    # headroom_db=1 gives ~0.89 max
    peak = np.max(np.abs(y))
    if peak <= 0:
        return y
    target = 10 ** (-headroom_db / 20.0)
    return y * (target / peak)


def view_ffts(input_samples, output_audio_array, figure_number:int|None=None):
    X = np.fft.rfft(input_samples[:, 0])
    freqs = np.fft.rfftfreq(len(input_samples[:, 0]), d=1 / sample_rate)

    # Magnitude
    mag = np.abs(X)

    X1 = np.fft.rfft(output_audio_array[:, 0])
    freqs1 = np.fft.rfftfreq(len(output_audio_array[:, 0]), d=1 / sample_rate)

    # Magnitude
    mag1 = np.abs(X1)

    plt.figure(figure_number)
    plt.clf()
    plt.plot(freqs, mag)
    plt.plot(freqs1, mag1)
    plt.xlabel("Frequency (Hz)")
    plt.ylabel("Magnitude")
    plt.title("FFT Magnitude Spectrum")
    plt.xlim(0, 1000)
    plt.show()

# @njit(cache=True)
def _phase_vocoder(input_samples, sample_rate=44100, rate=1.0, dt_anl=0.1, h_scale=2):
    n_samples, n_channels = input_samples.shape
    stretch_factor = 1 / rate

    n_samples_win = int(dt_anl * sample_rate)
    n_samples_syn = int(np.ceil(n_samples * stretch_factor)) - 1
    n_samples, n_channels = input_samples.shape

    h_syn = int(n_samples_win / h_scale)
    h_anl = int(h_syn / stretch_factor)
    n_windows = int(np.ceil((input_samples.shape[0] - 1) / h_anl))
    win = np.sqrt(hann(n_samples_win))
    win_weights = win.reshape(-1, 1)
    win2 = (win * win).astype("float")
    n_diff = int(n_samples_win / 2)

    output_audio_array = np.zeros((n_samples_syn, n_channels), dtype="float")

    n_bins = n_samples_win // 2 + 1
    k = np.arange(n_bins)
    omega = (2 * np.pi * k / n_samples_win).reshape(-1, 1)   # rad/sample
    phase_advance = (omega * h_anl).reshape(-1, 1)

    prv_phi = None
    phi = None
    wsum = np.zeros((n_samples_syn, 1), dtype="float")
    for k_w in range(n_windows):
        x_win = generate_window(input_samples, k_window=k_w, window_weights=win_weights, h_anl=h_anl)
        x_fft = np.fft.rfft(x_win, axis=0)
        mag_fft = np.abs(x_fft)
        phi_fft = np.angle(x_fft)

        if k_w == 0:
            phi = phi_fft.copy()
            prv_phi = phi_fft.copy()
        else:
            delta = phi_fft - prv_phi - phase_advance   # (n_bins, n_channels)
            delta = _wrap_to_pi(delta)  # wrap deviation

            true_freq = omega + delta / h_anl
            phi += true_freq * h_syn
            phi = _wrap_to_pi(phi)

            prv_phi = phi_fft

        x_syn_fft = mag_fft * np.exp(1j * phi)
        x_syn = np.fft.irfft(x_syn_fft, axis=0)[:n_samples_win] * win_weights
        if np.allclose(x_syn, 0):
            continue
        window_syn_center = int(k_w * h_syn)
        k_s0 = window_syn_center - n_diff
        if k_s0 > n_samples_syn:
            # Synthesis window is outside the audio file data support.
            continue
        k_s1 = window_syn_center + n_diff
        k_syn_min = max(0, k_s0)
        k_syn_max = min(n_samples_syn, k_s1)
        k_ws0 = 0
        k_ws1 = n_samples_win
        if k_s0 < k_syn_min:
            k_ws0 = n_samples_win - k_s1
            k_ws1 = n_samples_win
        if k_s1 > k_syn_max:
            k_ws0 = 0
            k_ws1 = k_syn_max - k_syn_min
        output_audio_array[k_syn_min:k_syn_max] += x_syn[k_ws0:k_ws1]
        wsum[k_syn_min:k_syn_max, 0] += win2[k_ws0:k_ws1]

    # view_ffts(input_samples, output_audio_array, 10)
    # normalize to undo overlap windowing gain
    tiny = 1e-12
    output_audio_array = np.where(wsum > tiny, output_audio_array / (wsum + tiny), 0.0)
    output_audio_array = peak_normalize(output_audio_array)
    return output_audio_array


def time_stretch_audio_array(input_samples, sample_rate=44100, rate=1.0, dt_anl=0.1, h_scale=2, method:Literal["overlap_add", "phase_vocoder"] = "overlap_add"):
    """
    Stretches audio in input_samples by rate.

    :param input_samples:   input audio
    :param sample_rate:     input audio sample rate
    :param rate:            stretch rate
    :param dt_anl:          analysis window time length
    :param h_scale:         synthesis hopsize (amount of overlap between windows)
    :param method:          sets which method to use
    :return:
    """
    t_start = time()
    if method == "overlap_add":
        output_audio_array = _overlap_add(input_samples, sample_rate, rate, dt_anl, h_scale)
    elif method == "phase_vocoder":
        output_audio_array = _phase_vocoder(input_samples, sample_rate, rate, dt_anl, h_scale)
    else:
        raise ValueError(f"Method not recognized. ({method=})")
    print(f"New Audio is {(output_audio_array.shape[0] - 1) / sample_rate} seconds, "
          f"Processing time: {time() - t_start}")
    return output_audio_array

# TODO; First we should combine the window and overlap add into one function, or at least one that generates the windows
#       in real time, i.e. builds the signal up sequentially without allocating the full array

if __name__ == "__main__":
    plt.ion()

    file_path = r'E:\developer\repos\pdf_player\test_resources\files\03 Cognitive Contortions.mp3'

    raw_audio, sample_rate = load(file_path, sr=None, mono=False)
    raw_audio = raw_audio.T

    dtype = raw_audio.dtype
    t_end = raw_audio.shape[0] / sample_rate

    # sample_rate = 44100
    # t_end = 10
    # t_base = np.linspace(0, t_end, sample_rate*t_end)
    #
    # freqs = [261, 392, 440]
    # base_audio = np.sum(np.vstack([np.cos(f*2*np.pi*t_base) for f in freqs]), axis=0)
    # raw_audio = np.repeat(base_audio,2).reshape(-1, 2) / base_audio.max()

    rate = 0.75
    h_scale = 4
    dt_anl = 2048/sample_rate * 3
    output_audio_array = time_stretch_audio_array(input_samples=raw_audio,
                                                  sample_rate=sample_rate,
                                                  rate=rate,
                                                  h_scale=h_scale,
                                                  dt_anl=dt_anl,
                                                  method="phase_vocoder")
    # export_mp3("test.mp3", output_audio_array, sample_rate)
    pause=1

