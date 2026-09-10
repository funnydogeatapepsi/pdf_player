import logging
from time import time
from typing import Literal

import numpy as np
from numba import njit, prange

log = logging.getLogger(__name__)


@njit(cache=True, nogil=True)
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


@njit(cache=True, parallel=True, nogil=True)
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


@njit(cache=True, nogil=True)
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


@njit(cache=True, nogil=True)
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


@njit(cache=True, nogil=True)
def _dot(a_arr, a, b_arr, b, length):
    acc = 0.0
    for i in range(length):
        acc += a_arr[a + i] * b_arr[b + i]
    return acc


@njit(cache=True, nogil=True)
def _prefix_energy(x):
    """csum[j] = sum(x[i]^2 for i < j), so the energy of x[a:b] is csum[b] - csum[a]."""
    csum = np.zeros(x.shape[0] + 1, dtype="float")
    for i in range(x.shape[0]):
        csum[i + 1] = csum[i] + x[i] * x[i]
    return csum


@njit(cache=True, nogil=True)
def _wsola(input_samples, rate=1.0, n_win=2048, search=512, decim=8):
    """
    Waveform Similarity Overlap-Add (Verhelst & Roelands, 1993).

    Same structure as _overlap_add (hann windows, 50% overlap), but each analysis frame is taken from the best
    matching position within +-search samples of its nominal position, where "best" means most similar
    (normalised cross-correlation) to the natural continuation of the previously selected frame. This removes
    the amplitude wobble / comb filtering of plain OLA (frames no longer overlap with arbitrary phase) without
    any phase vocoder artefacts.

    The similarity search is done coarsely on a `decim`-times decimated (block averaged) signal, then refined
    at full resolution. Window energies come from prefix sums, so each candidate costs one dot product.

    :param input_samples: (n_samples, n_channels) audio
    :param rate:          playback rate (0.5 = half speed)
    :param n_win:         frame length in samples
    :param search:        max deviation (samples) from the nominal frame position
    :param decim:         decimation factor for the coarse search
    """
    n_samples, n_channels = input_samples.shape
    h_syn = n_win // 2                  # synthesis hop (50% overlap)
    overlap = n_win - h_syn
    win = hann(n_win)
    n_out = int(np.ceil(n_samples / rate)) - 1
    n_frames = int(np.ceil(n_out / h_syn)) + 1

    x = input_samples.astype("float")
    mono = np.zeros(n_samples, dtype="float")
    for c in range(n_channels):
        mono += x[:, c]
    csum = _prefix_energy(mono)

    # Block-averaged (decimated) mono for the coarse search:
    n_dec = n_samples // decim
    mono_d = np.zeros(n_dec, dtype="float")
    for j in range(n_dec):
        acc = 0.0
        for i in range(decim):
            acc += mono[j * decim + i]
        mono_d[j] = acc / decim
    csum_d = _prefix_energy(mono_d)
    n_coarse = overlap // decim         # decimated samples in the overlap region

    output = np.zeros((n_out, n_channels), dtype="float")
    wsum = np.zeros(n_out, dtype="float")
    a_max = n_samples - n_win
    prev_a = 0
    for k in range(n_frames):
        s = k * h_syn                           # synthesis position
        a_nom = int(round(s * rate))            # nominal analysis position
        a = min(max(a_nom, 0), max(a_max, 0))

        if k > 0:
            ref = prev_a + h_syn                # natural continuation of the previous frame
            lo = max(a_nom - search, 0)
            hi = min(a_nom + search, a_max)
            if ref + overlap <= n_samples and hi > lo:
                # --- coarse search on the decimated signal
                ref_d = ref // decim
                e_ref = csum_d[ref_d + n_coarse] - csum_d[ref_d]
                best = -2.0
                best_a = a
                for cand_d in range(lo // decim, hi // decim + 1):
                    if cand_d + n_coarse > n_dec:
                        break
                    e_cand = csum_d[cand_d + n_coarse] - csum_d[cand_d]
                    score = _dot(mono_d, cand_d, mono_d, ref_d, n_coarse) / (np.sqrt(e_ref * e_cand) + 1e-9)
                    if score > best:
                        best = score
                        best_a = cand_d * decim
                # --- refine at full resolution around the coarse optimum
                e_ref = csum[ref + overlap] - csum[ref]
                best = -2.0
                a = min(max(best_a, lo), hi)
                for cand in range(max(best_a - decim, lo), min(best_a + decim, hi) + 1):
                    e_cand = csum[cand + overlap] - csum[cand]
                    score = _dot(mono, cand, mono, ref, overlap) / (np.sqrt(e_ref * e_cand) + 1e-9)
                    if score > best:
                        best = score
                        a = cand

        # overlap-add the selected frame
        n_avail = min(n_win, n_samples - a, n_out - s)
        if n_avail <= 0:
            break
        for i in range(n_avail):
            w = win[i]
            wsum[s + i] += w
            for c in range(n_channels):
                output[s + i, c] += w * x[a + i, c]
        prev_a = a

    # hann at 50% overlap sums to 1 except at the very edges; normalise those (with a floor).
    for i in range(n_out):
        if wsum[i] < 0.999:
            for c in range(n_channels):
                output[i, c] = output[i, c] / max(wsum[i], 1e-2)
    return output


@njit(cache=True, nogil=True)
def _wrap_to_pi(x):
    # wrap to [-pi, pi]
    return (x + np.pi) % (2.0 * np.pi) - np.pi


@njit(cache=True, nogil=True)
def peak_normalize(y, headroom_db=0.05):
    # headroom_db=1 gives ~0.89 max
    peak = np.max(np.abs(y))
    if peak <= 0:
        y_norm = y * 1.0
    else:
        target = 10 ** (-headroom_db / 20.0)
        y_norm = y * (target / peak)
    return y_norm


@njit(cache=True, nogil=True)
def _phase_vocoder_channel(x, sample_rate=44100, rate=1.0, dt_anl=0.1, h_scale=2, phase_lock=True):
    """
    Phase vocoder for a single channel x (1-D). Returns the stretched channel (1-D float).

    Uses identity phase locking (Laroche & Dolson, 1999): bins around each spectral peak inherit the peak's phase
    advance instead of accumulating their own. This keeps partials coherent, which removes most of the "phasy" /
    reverberant character and stops neighbouring bins from re-aligning into spurious amplitude spikes.
    """
    stretch_factor = 1 / rate

    n_samples = x.shape[0]
    n_samples_win = int(dt_anl * sample_rate)
    n_samples_syn = int(np.ceil(n_samples * stretch_factor)) - 1

    h_syn = int(n_samples_win / h_scale)
    h_anl = int(h_syn / stretch_factor)
    n_windows = int(np.ceil((n_samples - 1) / h_anl))
    win = np.sqrt(hann(n_samples_win))
    win2 = (win * win).astype("float")
    n_diff = int(n_samples_win / 2)
    n_bins = n_samples_win // 2 + 1

    omega = 2 * np.pi * np.arange(n_bins) / n_samples_win   # rad/sample
    phase_advance = omega * h_anl

    output = np.zeros(n_samples_syn, dtype="float")
    wsum = np.zeros(n_samples_syn, dtype="float")

    phi = np.zeros(n_bins, dtype="float")       # synthesis phases (previous frame)
    prv_phi = np.zeros(n_bins, dtype="float")   # analysis phases (previous frame)
    x_win = np.zeros(n_samples_win, dtype="float")
    for k_w in range(n_windows):
        # ---- analysis frame (zero padded at the edges)
        x_win[:] = 0.0
        window_anl_center = int(k_w * h_anl)
        k_0 = window_anl_center - n_diff
        k_1 = window_anl_center + n_diff
        k_anl_min = max(0, k_0)
        k_anl_max = min(n_samples, k_1)
        k_w0 = 0
        k_w1 = n_samples_win
        if k_0 < k_anl_min:
            k_w0 = n_samples_win - k_1
            k_w1 = n_samples_win
        if k_1 > k_anl_max:
            k_w0 = 0
            k_w1 = k_anl_max - k_anl_min
        x_win[k_w0:k_w1] = x[k_anl_min:k_anl_max]
        x_win *= win

        x_fft = np.fft.rfft(x_win)
        mag_fft = np.abs(x_fft)
        phi_fft = np.angle(x_fft)

        if k_w == 0:
            phi[:] = phi_fft
        else:
            delta = _wrap_to_pi(phi_fft - prv_phi - phase_advance)
            true_freq = omega + delta / h_anl
            phi_unlocked = _wrap_to_pi(phi + true_freq * h_syn)
            if phase_lock:
                # Assign every bin to the nearest spectral peak (region boundary = midpoint between peaks) and
                # lock its synthesis phase to that peak: phi[k] = phi[p] + (phi_fft[k] - phi_fft[p]).
                new_phi = np.empty(n_bins, dtype="float")
                prev_peak = -1
                prev_lo = 0
                for k in range(1, n_bins):
                    is_peak = (k == n_bins - 1) or (mag_fft[k] > mag_fft[k - 1] and mag_fft[k] >= mag_fft[k + 1])
                    if not is_peak:
                        continue
                    if prev_peak < 0:
                        lo = 0
                    else:
                        lo = (prev_peak + k) // 2 + 1
                        # finish the previous peak's region
                        for b in range(prev_lo, lo):
                            new_phi[b] = phi_unlocked[prev_peak] + (phi_fft[b] - phi_fft[prev_peak])
                    prev_peak = k
                    prev_lo = lo
                if prev_peak < 0:
                    new_phi[:] = phi_unlocked
                else:
                    for b in range(prev_lo, n_bins):
                        new_phi[b] = phi_unlocked[prev_peak] + (phi_fft[b] - phi_fft[prev_peak])
                phi[:] = _wrap_to_pi(new_phi)
            else:
                phi[:] = phi_unlocked
        prv_phi[:] = phi_fft

        x_syn = np.fft.irfft(mag_fft * np.exp(1j * phi))[:n_samples_win] * win

        # ---- overlap-add into the synthesis time base (zero padding handled by index logic)
        window_syn_center = int(k_w * h_syn)
        k_s0 = window_syn_center - n_diff
        if k_s0 > n_samples_syn:
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
        output[k_syn_min:k_syn_max] += x_syn[k_ws0:k_ws1]
        wsum[k_syn_min:k_syn_max] += win2[k_ws0:k_ws1]

    # Undo the overlap windowing gain. Floor wsum so the (single-window) edges are not amplified.
    for i in range(n_samples_syn):
        output[i] = output[i] / max(wsum[i], 1e-2)
    return output


@njit(cache=True, parallel=True, nogil=True)
def _phase_vocoder(input_samples, sample_rate=44100, rate=1.0, dt_anl=0.1, h_scale=2, phase_lock=True):
    """Phase vocoder over all channels; channels are independent and processed in parallel."""
    n_samples, n_channels = input_samples.shape
    n_samples_syn = int(np.ceil(n_samples / rate)) - 1
    output_audio_array = np.zeros((n_samples_syn, n_channels), dtype="float")
    for c in prange(n_channels):
        y = _phase_vocoder_channel(input_samples[:, c].astype("float"), sample_rate, rate, dt_anl, h_scale, phase_lock)
        output_audio_array[:, c] = y[:n_samples_syn]
    return output_audio_array


@njit(cache=True, nogil=True)
def _rms(x):
    acc = 0.0
    for i in range(x.shape[0]):
        for c in range(x.shape[1]):
            acc += x[i, c] * x[i, c]
    return np.sqrt(acc / max(x.shape[0] * x.shape[1], 1))


@njit(cache=True, nogil=True)
def match_level(y, x, limit=32767.0, knee=0.8):
    """
    Scale y (in place) so its RMS matches x's, then soft-limit anything above knee*limit so peaks stay within
    +-limit. Returns y.

    Peak-normalising the stretched audio (the old behaviour) made everything quieter, because time stretching
    (the phase vocoder in particular) raises the crest factor: RMS is preserved but a handful of samples spike.
    """
    rms_x = _rms(x)
    rms_y = _rms(y)
    if rms_y <= 0 or rms_x <= 0:
        return y
    gain = rms_x / rms_y
    thr = knee * limit
    span = limit - thr
    for i in range(y.shape[0]):
        for c in range(y.shape[1]):
            v = y[i, c] * gain
            if v > thr:
                v = thr + span * np.tanh((v - thr) / span)
            elif v < -thr:
                v = -thr - span * np.tanh((-v - thr) / span)
            y[i, c] = v
    return y


def float_to_int16(x):
    x = np.clip(x, -1.0, 1.0)
    return (x * 32767).astype(np.int16)


def time_stretch_audio_array(input_samples, sample_rate=44100, rate=1.0, h_scale=2, ndt=4, method: Literal["overlap_add", "wsola", "phase_vocoder", "legacy"] = "overlap_add", normalize=True):
    """
    Stretches audio in input_samples by rate.

    TODO; make phase vocoder better

    :param input_samples:   input audio
    :param sample_rate:     input audio sample rate
    :param rate:            stretch rate
    :param dt_anl:          analysis window time length
    :param h_scale:         synthesis hopsize (amount of overlap between windows)
    :param method:          "overlap_add" (fast, some amplitude wobble), "wsola" (overlap-add with similarity
                            alignment; recommended), "phase_vocoder" (frequency domain), "legacy"
    :param normalize:       match the output's RMS level to the input and soft-limit peaks (see match_level)
    :return:                stretched audio, float, same amplitude scale as the input
    """
    dt_anl = 2048/sample_rate * ndt
    t_start = time()
    if method == "overlap_add":
        output_audio_array = _overlap_add(input_samples, sample_rate, rate, dt_anl, h_scale)
    elif method == "wsola":
        n_win = int(round(2048 * sample_rate / 44100))      # ~46 ms frames, +-12 ms search
        output_audio_array = _wsola(input_samples, rate, n_win, n_win // 4, 8)
    elif method == "phase_vocoder":
        output_audio_array = _phase_vocoder(input_samples, sample_rate, rate, dt_anl, h_scale)
    elif method == "legacy":
        output_audio_array = _legacy_time_stretch_audio_array(input_samples, sample_rate, rate, dt_anl, h_scale)
    else:
        raise ValueError(f"Method not recognized. ({method=})")
    log.debug(f"New Audio is {(output_audio_array.shape[0] - 1) / sample_rate} seconds, Processing time: {time() - t_start}")
    if normalize:
        # Match loudness (RMS) to the input and soft-limit the few over-range peaks, so the output is at the
        # input's scale (int16 range for int16 input) instead of being peak-normalised to [-1, 1].
        output_audio_array = match_level(output_audio_array, input_samples, limit=float(np.iinfo(np.int16).max))
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
