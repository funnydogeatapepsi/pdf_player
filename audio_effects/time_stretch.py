import logging
from time import time
from typing import Literal

import numpy as np
from numba import njit, prange

log = logging.getLogger(__name__)


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


@njit(cache=True, parallel=True)
def _legacy_time_stretch_audio_array(input_samples, sample_rate=44100, rate=1.0, dt_anl=0.1, h_scale=2):
    """ Overlay Offset Add algorithm from review paper """
    n_samples, n_channels = input_samples.shape
    dtype = input_samples.dtype
    stretch_factor = 1 / rate

    n_samples_win = int(dt_anl * sample_rate)
    win_weights = hann(n_samples_win).reshape(-1, 1)
    ndiff = int(n_samples_win / 2)
    h_syn = int(n_samples_win / h_scale)
    h_anl = int(h_syn / stretch_factor)
    n_windows = int(np.ceil((n_samples - 1) / h_anl))

    n_samples_syn = int(np.ceil(n_samples * stretch_factor)) - 1
    win_shape = (n_windows, n_samples_win, n_channels)
    x_wins = np.zeros(shape=win_shape, dtype=dtype)
    for k_w in prange(n_windows):
        window_anl_center = int(k_w * h_anl)
        k_0 = window_anl_center - ndiff
        k_1 = window_anl_center + ndiff
        k_anl_min = max(0, k_0)
        k_anl_max = min(n_samples, k_1)
        window_samples = input_samples[k_anl_min:k_anl_max]
        if np.allclose(window_samples, np.zeros_like(window_samples)):
            continue

        k_w0 = 0
        k_w1 = n_samples_win
        if k_0 < k_anl_min:
            k_w0 = n_samples_win - k_1
            k_w1 = n_samples_win
        if k_1 > k_anl_max:
            k_w0 = 0
            k_w1 = k_anl_max - k_anl_min
        x_wins[k_w, k_w0:k_w1] = window_samples
        x_wins[k_w] = (x_wins[k_w] * win_weights).astype(dtype)

    output_audio_array = np.zeros((n_samples_syn, n_channels), dtype=dtype)
    for k_w in range(n_windows):
        x_win = x_wins[k_w]
        if np.allclose(x_win, np.zeros_like(x_win)):
            continue
        window_syn_center = int(k_w * h_syn)
        k_s0 = window_syn_center - ndiff
        if k_s0 > n_samples_syn:
            # Synthesis window is outside the audio file data support.
            continue
        k_s1 = window_syn_center + ndiff
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
    stretch_factor = 1 / rate

    n_samples_win = int(dt_anl * sample_rate)
    n_samples_syn = int(np.ceil(n_samples * stretch_factor)) - 1

    h_syn = int(n_samples_win / h_scale)
    h_anl = int(h_syn / stretch_factor)
    n_windows = int(np.ceil((input_samples.shape[0] - 1) / h_anl))
    win_weights = hann(n_samples_win).reshape(-1, 1)

    n_diff = int(n_samples_win / 2)
    output_audio_array = np.zeros((n_samples_syn, n_channels), dtype='float')
    for k_w in range(n_windows):
        x_win = generate_window(input_samples, k_window=k_w, window_weights=win_weights, h_anl=h_anl)
        if np.allclose(x_win, 0):
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
def peak_normalize(y, headroom_db=0.05):
    # headroom_db=1 gives ~0.89 max
    peak = np.max(np.abs(y))
    if peak <= 0:
        y_norm = y * 1.0
    else:
        target = 10 ** (-headroom_db / 20.0)
        y_norm = y * (target / peak)
    return y_norm


@njit(cache=True)
def _phase_vocoder(input_samples, sample_rate=44100, rate=1.0, dt_anl=0.1, h_scale=2):
    stretch_factor = 1 / rate

    n_samples = input_samples.shape[0]
    n_channels = input_samples.shape[1]
    n_samples_win = int(dt_anl * sample_rate)
    n_samples_syn = int(np.ceil(n_samples * stretch_factor)) - 1

    h_syn = int(n_samples_win / h_scale)
    h_anl = int(h_syn / stretch_factor)
    n_windows = int(np.ceil((input_samples.shape[0] - 1) / h_anl))
    win = np.sqrt(hann(n_samples_win))
    win_weights = win.reshape((-1, 1))
    win2 = (win * win).astype("float")
    n_diff = int(n_samples_win / 2)
    n_bins = n_samples_win // 2 + 1

    k = np.arange(n_bins)
    omega = (2 * np.pi * k / n_samples_win).reshape(-1, 1)   # rad/sample
    phase_advance = (omega * h_anl).reshape(-1, 1)
    output_audio_array = np.zeros((n_samples_syn, n_channels), dtype="float")
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

        # Need to place this in the output's time base:
        window_syn_center = int(k_w * h_syn)    # Center of window is here
        k_s0 = window_syn_center - n_diff       # ideal left boundary
        if k_s0 > n_samples_syn:
            # Synthesis window is outside the synthesis time base.
            continue
        k_s1 = window_syn_center + n_diff      # ideal right boundary
        k_syn_min = max(0, k_s0)               # snapped left boundary if less than 0
        k_syn_max = min(n_samples_syn, k_s1)   # snapped right boundary if greater than syn time base

        # This is my way to 0 pad using logic instead of padding the data:
        k_ws0 = 0                              # How much of the synthesis do we use?
        k_ws1 = n_samples_win                  # Ideally all of them
        if k_s0 < k_syn_min:
            # The left boundary was snapped, so we use only the data at the end of
            # the window (valid data from the input)
            k_ws0 = n_samples_win - k_s1
            k_ws1 = n_samples_win
        if k_s1 > k_syn_max:
            # The right boundary was snapped, so we use only the data at the beginning of
            # the window (valid data from the input)
            k_ws0 = 0
            k_ws1 = k_syn_max - k_syn_min
        output_audio_array[k_syn_min:k_syn_max] += x_syn[k_ws0:k_ws1]
        wsum[k_syn_min:k_syn_max, 0] += win2[k_ws0:k_ws1]
    # view_ffts(input_samples, output_audio_array, 10)

    # Normalize to undo overlap windowing gain
    tiny = 1e-12
    output_audio_array = np.where(wsum > tiny, output_audio_array / (wsum + tiny), 0.0)
    return output_audio_array

def float_to_int16(x):
    x = np.clip(x, -1.0, 1.0)
    return (x * 32767).astype(np.int16)


def time_stretch_audio_array(input_samples, sample_rate=44100, rate=1.0, h_scale=2, ndt=4, method:Literal["overlap_add", "phase_vocoder", "legacy"] = "overlap_add", normalize=True):
    """
    Stretches audio in input_samples by rate.

    TODO; make phase vocoder better

    :param input_samples:   input audio
    :param sample_rate:     input audio sample rate
    :param rate:            stretch rate
    :param dt_anl:          analysis window time length
    :param h_scale:         synthesis hopsize (amount of overlap between windows)
    :param method:          sets which method to use
    :return:
    """
    dt_anl = 2048/sample_rate * ndt
    t_start = time()
    if method == "overlap_add":
        output_audio_array = _overlap_add(input_samples, sample_rate, rate, dt_anl, h_scale)
    elif method == "phase_vocoder":
        output_audio_array = _phase_vocoder(input_samples, sample_rate, rate, dt_anl, h_scale)
    elif method == "legacy":
        output_audio_array = _legacy_time_stretch_audio_array(input_samples, sample_rate, rate, dt_anl, h_scale)
    else:
        raise ValueError(f"Method not recognized. ({method=})")
    log.debug(f"New Audio is {(output_audio_array.shape[0] - 1) / sample_rate} seconds, Processing time: {time() - t_start}")
    if normalize:
        output_audio_array = peak_normalize(output_audio_array)
    return output_audio_array


if __name__ == "__main__":
    pass
else:
    # initialize numba
    sample_rate = 44100
    t_end = 1
    n_channels = 1
    test_freq = 440
    n_samples = int(sample_rate*t_end+1)
    t_samples = np.linspace(0, t_end, n_samples)
    dtype = np.int16
    max_val = np.iinfo(dtype).max
    channel_samples = np.column_stack([(np.sin(2*np.pi*test_freq*t_samples)*max_val).astype(dtype)
                                       for _ in range(n_channels)])
    output_audio_array = time_stretch_audio_array(input_samples=channel_samples,
                                                  sample_rate=sample_rate,
                                                  rate=0.9)
    # ,
    # h_scale = 2,
    # method = "phase_vocoder"
    #
    # removing compile w/ rocket fft since its having issues with pyinstaller
    # TODO; fix rocket-fft and pyinstaller interaction
