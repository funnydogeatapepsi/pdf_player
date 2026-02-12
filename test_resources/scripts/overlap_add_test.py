# import matplotlib
# matplotlib.use("TkAgg")
# from matplotlib.pyplot import *

from time import time

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


def samples_to_audio_segment(samples, dtype=np.int16, sample_rate=44100):
    n_samples, n_channels = samples.shape
    data_shape = (n_samples * n_channels, )
    raw_data_fixed = np.zeros(data_shape, dtype=dtype)
    for k_channel in range(n_channels):
        raw_data_fixed[k_channel::2] = samples[:, k_channel]
    return AudioSegment(raw_data_fixed.tobytes(), frame_rate=sample_rate,
                        sample_width=raw_data_fixed.dtype.itemsize, channels=n_channels)


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


def time_stretch_audio_array(input_samples, sample_rate=44100, rate=1.0, dt_anl=0.1, h_scale=2):
    _t_start = time()
    output_audio_array = _time_stretch_audio_array(input_samples,
                                                   sample_rate=sample_rate,
                                                   rate=rate,
                                                   dt_anl=dt_anl,
                                                   h_scale=h_scale)
    print(f"New Audio is {(output_audio_array.shape[0] - 1) / sample_rate} seconds, "
          f"Processing time: {time() - _t_start}")
    return output_audio_array


@njit(cache=True)
def _get_windows(input_samples, n_samples_win, stretch_factor, h_syn, dtype):
    n_samples, n_channels = input_samples.shape
    h_anl = int(h_syn / stretch_factor)
    n_windows = int(np.ceil((input_samples.shape[0] - 1) / h_anl))
    win_shape = (n_windows, n_samples_win, n_channels)
    win_weights = hann(n_samples_win).reshape(-1, 1)

    n_windows = win_shape[0]
    n_samples_win = win_shape[1]
    n_diff = int(n_samples_win / 2)
    x_wins = np.zeros(shape=win_shape, dtype=dtype)
    for k_w in prange(n_windows):
        window_anl_center = int(k_w * h_anl)
        k_0 = window_anl_center - n_diff
        k_1 = window_anl_center + n_diff
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
    return x_wins


@njit(cache=True)
def _overlap_add(x_wins, output_shape, h_syn, dtype):
    n_windows = x_wins.shape[0]
    n_samples_win = x_wins.shape[1]
    n_diff = int(n_samples_win / 2)
    n_samples_syn, n_channels = output_shape
    output_audio_array = np.zeros((n_samples_syn, n_channels), dtype=dtype)
    for k_w in range(n_windows):
        x_win = x_wins[k_w]
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
def _time_stretch_audio_array(input_samples, sample_rate=44100, rate=1.0, dt_anl=0.1, h_scale=2):
    _n_samples, _n_channels = input_samples.shape
    _dtype = input_samples.dtype
    stretch_factor = 1 / rate

    n_samples_win = int(dt_anl * sample_rate)
    n_samples_syn = int(np.ceil(_n_samples * stretch_factor)) - 1

    h_syn = int(n_samples_win / h_scale)
    data_windows = _get_windows(input_samples, n_samples_win, stretch_factor, h_syn, _dtype)
    return _overlap_add(data_windows, (n_samples_syn, _n_channels), h_syn, _dtype)


def time_stretch_audio(input_audio: AudioSegment, rate=1.0, return_data=True):
    sample_rate = input_audio.frame_rate
    input_sounds = input_audio.split_to_mono()
    input_samples = np.column_stack([s.get_array_of_samples() for s in input_sounds])

    output_audio_array = time_stretch_audio_array(input_samples, sample_rate, rate=rate)
    if return_data:
        return output_audio_array

    return samples_to_audio_segment(output_audio_array, dtype=dtype)


if __name__ == "__main__":
    # ion()

    file_path = "/test_resources/03 Cognitive Contortions.mp3"

    with AudioFile(file_path) as f:
        sample_rate = f.samplerate
        raw_audio = f.read(f.samplerate * f.duration)

    pause=1

    raw_audio, sample_rate = load(file_path, sr=None, mono=False)
    raw_audio = raw_audio.T

    raw_audio = AudioSegment.from_file(file_path)

    t_start = time()
    sample_rate = raw_audio.frame_rate
    channel_sounds = raw_audio.split_to_mono()
    channel_samples = np.column_stack([s.get_array_of_samples() for s in channel_sounds])
    n_samples, n_channels = channel_samples.shape
    dtype = channel_samples.dtype
    t_end = raw_audio.duration_seconds
    # t_samples = np.linspace(0, t_end, n_samples)

    # # Method 1: Do FFT
    # fft_samples = [fft(x) for x in channel_samples]
    # ifft_samples = [ifft(x) for x in fft_samples]
    # abs_samples = [np.abs(x) for x in ifft_samples]
    # slow_audio = samples_to_audio_segment(abs_samples, dtype=dtype)
    # # play(slow_audio)
    #
    # sample_rate = 44100
    # t_end = 2
    # n_channels = 2
    # test_freq = 2
    # n_samples = sample_rate*t_end+1
    # t_samples = np.linspace(0, t_end, n_samples)
    # dtype = np.int16
    # max_val = np.iinfo(dtype).max
    # channel_samples = np.column_stack([(np.sin(2*np.pi*test_freq*t_samples)*max_val).astype(dtype) for _ in range(n_channels)])
    # test_audio = samples_to_audio_segment(channel_samples, dtype=dtype)
    # play(test_audio)

    rate = 0.75
    h_scale = 2
    dt_anl = 0.1
    output_audio_array = time_stretch_audio_array(input_samples=channel_samples,
                                                  sample_rate=sample_rate,
                                                  rate=rate,
                                                  h_scale=h_scale,
                                                  dt_anl=dt_anl)

    mod_audio = samples_to_audio_segment(output_audio_array, dtype=dtype)
    play(mod_audio)

    # mod_audio = time_stretch_audio(raw_audio)
    # play(mod_audio)

    # window_time = 0.1
    # window_std = 100
    # hop_size = 1000
    # window_samples = window_time*sample_rate
    # window = gaussian(window_samples, std=window_std, sym=True)
    # STF = ShortTimeFFT(win=window, hop=hop_size, fs=sample_rate, mfft=hop_size*2, scale_to='magnitude')

    pause=1

