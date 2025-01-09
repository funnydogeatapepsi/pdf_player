import logging
from time import time
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
def _time_stretch_audio_array(input_samples, sample_rate=44100, rate=1.0, dt_anl=0.1, h_scale=2):
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


def time_stretch_audio_array(input_samples, sample_rate=44100, rate=1.0, dt_anl=0.1, h_scale=2):
    t_start = time()
    output_audio_array = _time_stretch_audio_array(input_samples,
                                                   sample_rate=sample_rate,
                                                   rate=rate,
                                                   dt_anl=dt_anl,
                                                   h_scale=h_scale)
    log.debug(f"New Audio is {(output_audio_array.shape[0] - 1) / sample_rate} seconds, "
              f"Processing time: {time() - t_start}")
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
                                                  rate=0.9,
                                                  h_scale=2,
                                                  dt_anl=0.2)
