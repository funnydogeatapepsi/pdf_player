import logging

from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtCore import QUrl, QBuffer, Signal, QObject, QRunnable, QThreadPool

from tinytag import TinyTag
import io
from scipy.io import wavfile
import librosa
import numpy as np

from audio_effects.time_stretch import time_stretch_audio_array, float_to_int16

log = logging.getLogger(__name__)

# Time stretch algorithms the user can pick from: key -> label shown in the UI.
STRETCH_METHODS = {
    "wsola": "WSOLA (overlap-add with waveform alignment, recommended)",
    "overlap_add": "Overlap-Add (fastest, some amplitude wobble)",
    "phase_vocoder": "Phase Vocoder (frequency domain)",
}
DEFAULT_STRETCH_METHOD = "wsola"


class Song:
    file_path = None
    metadata = None
    is_empty = False
    file_url = None
    sample_rate = None
    raw_audio = None

    def __init__(self, file_url: QUrl = None):
        if file_url is not None:
            self.file_url = file_url
        else:
            self.is_empty = True
            self.__has_metadata = False
            return

        self.file_path = file_url.toLocalFile()
        self.metadata = TinyTag.get(self.file_path)
        self.__has_metadata = True

        self.read_data()

    @property
    def has_metadata(self):
        return self.__has_metadata

    # Peak level (as a fraction of int16 full scale) the loaded audio is normalized to.
    # Leaves a little headroom so the time-stretched output does not clip.
    PEAK_LEVEL = 0.9

    def read_data(self):
        """
        Read audio file. Audio is always stored as (n_samples, n_channels) int16 with at least 2 channels;
        mono files are duplicated into both channels.

        :return:
        """
        raw_audio, sample_rate = librosa.load(self.file_path, sr=None, mono=False)
        raw_audio = np.atleast_2d(raw_audio).T          # -> (n_samples, n_channels)
        if raw_audio.shape[1] == 1:
            raw_audio = np.repeat(raw_audio, 2, axis=1)  # mono -> stereo
        peak = np.abs(raw_audio).max()
        if peak > 0:
            raw_audio = raw_audio / peak * self.PEAK_LEVEL
        self.raw_audio = (raw_audio * np.iinfo(np.int16).max).astype(np.int16)
        self.sample_rate = sample_rate

    @staticmethod
    def stretch(raw_audio, sample_rate, rate=1.0, method="wsola"):
        """
        Pure function: time stretch raw_audio (int16, (n, ch)) by `rate`. Safe to call from a worker thread.

        :return: int16 audio at the same loudness as the input
        """
        if abs(rate - 1.0) < 1e-5:
            return raw_audio
        # Output comes back at the same scale/loudness as the input (RMS matched + soft limited), see time_stretch.
        modified_audio = time_stretch_audio_array(raw_audio, sample_rate, rate, method=method)
        return np.clip(modified_audio, np.iinfo(np.int16).min, np.iinfo(np.int16).max).astype(np.int16)

    def set_stretched(self, modified_audio):
        """Record the duration of the audio currently loaded in the player (main thread)."""
        self.metadata.duration = (modified_audio.shape[0] - 1) / self.sample_rate
        return modified_audio

    def modify_rate(self, rate=1.0, method="wsola"):
        """
        Set audio playback rate synchronously. Return modified audio.

        :param rate:    playback rate (1.0 = original speed)
        :param method:  time stretch algorithm, see STRETCH_METHODS
        :return:
        """
        if self.raw_audio is None:
            return
        return self.set_stretched(self.stretch(self.raw_audio, self.sample_rate, rate, method))


class StretchWorker(QRunnable):
    """
    Runs Song.stretch on a thread pool thread and reports the result through `signals`.
    The numba kernels release the GIL, so the UI keeps repainting while this runs.
    """
    class Signals(QObject):
        finished = Signal(int, object)      # job id, int16 audio
        failed = Signal(int, str)           # job id, error text

    def __init__(self, job_id, raw_audio, sample_rate, rate, method):
        super().__init__()
        self.job_id = job_id
        self.raw_audio = raw_audio
        self.sample_rate = sample_rate
        self.rate = rate
        self.method = method
        self.signals = self.Signals()

    def run(self):
        try:
            audio = Song.stretch(self.raw_audio, self.sample_rate, self.rate, self.method)
        except Exception as err:      # noqa: BLE001 - report anything to the UI thread
            log.exception("time stretch failed")
            self.signals.failed.emit(self.job_id, str(err))
            return
        self.signals.finished.emit(self.job_id, audio)


class AudioPlayer(QMediaPlayer):
    audio_ready_signal = Signal(bool)
    stretch_busy_signal = Signal(bool)      # True while a time stretch is running in the background
    stretch_failed_signal = Signal(str)

    def __init__(self, parent):
        super().__init__(parent=parent)

        self.audio_output = QAudioOutput()
        self.setAudioOutput(self.audio_output)
        self._current_song = Song()
        self.buffer = QBuffer()
        self._volume = 1.0
        self._playback_rate = 1.0
        self._stretch_method = DEFAULT_STRETCH_METHOD
        self._thread_pool = QThreadPool(self)
        self._thread_pool.setMaxThreadCount(1)   # stretches are serialised; only the newest result is applied
        self._stretch_job_id = 0
        self._stretch_pending = None            # (rate, method) of the job whose result we are waiting for
        self._loaded_rate = 1.0                 # rate of the stream currently loaded in the player
        self._pending_position = None   # Seek target not yet confirmed by the backend (see setPosition).
        self._pending_attempts = 0
        self._stream_ready = False      # False from setSourceDevice() until the backend reports the media loaded.
        self.mediaStatusChanged.connect(self._on_media_status_changed)
        self.durationChanged.connect(lambda duration: self._apply_pending_position("durationChanged"))
        self.positionChanged.connect(lambda position: self._apply_pending_position("positionChanged"))

    @property
    def current_song(self):
        return self._current_song

    @current_song.setter
    def current_song(self, song: Song):
        """
        Set song and create audio stream

        :param song:
        :return:
        """
        self._current_song = song
        self._playback_rate = 1.0
        self._loaded_rate = 1.0
        self._stretch_job_id += 1           # any in-flight stretch belongs to the old song; drop its result
        if self._stretch_pending is not None:
            self._stretch_pending = None
            self.stretch_busy_signal.emit(False)
        self.create_audio_stream(song.raw_audio)

    def setVolume(self, volume: float):
        """
        Set the output volume (0.0 - 1.0). Remembered so it can be re-applied when a new stream is created.
        """
        self._volume = max(0.0, min(1.0, float(volume)))
        self.audio_output.setVolume(self._volume)

    def volume(self) -> float:
        return self._volume

    @property
    def stretch_method(self) -> str:
        return self._stretch_method

    def setStretchMethod(self, method: str):
        """
        Choose the time stretch algorithm (see STRETCH_METHODS). If a rate other than 1.0 is active, the current
        song is re-stretched with the new method.
        """
        if method not in STRETCH_METHODS:
            raise ValueError(f"Unknown stretch method {method!r}; expected one of {list(STRETCH_METHODS)}")
        if method == self._stretch_method:
            return
        self._stretch_method = method
        log.info(f"stretch method: {method}")
        if abs(self._playback_rate - 1.0) > 1e-5 and self._current_song.raw_audio is not None:
            self.setPlaybackRate(self._playback_rate, force=True)

    def playbackRate(self) -> float:
        return self._playback_rate

    def setPlaybackRate(self, rate, preserve_position=True, force=False):
        """
        Set song playback rate. The stretch runs on a background thread; the new stream is swapped in when it is
        done, keeping the musical position (and playback state) the user had.

        :param rate:
        :param preserve_position:
        :return:
        """
        if self._current_song.raw_audio is None:
            log.info("No audio currently imported.")
            return
        self._playback_rate = rate
        job = (rate, self._stretch_method)
        if job == self._stretch_pending:
            return
        if not force and self._stretch_pending is None and abs(rate - self._loaded_rate) < 1e-5:
            return      # already playing at this rate
        log.info(f"set playback {rate=} ({self._stretch_method})")

        self._stretch_job_id += 1
        self._stretch_pending = job
        worker = StretchWorker(self._stretch_job_id, self._current_song.raw_audio, self._current_song.sample_rate,
                               rate, self._stretch_method)
        worker.signals.finished.connect(lambda job_id, audio: self._on_stretch_finished(job_id, audio, preserve_position))
        worker.signals.failed.connect(self._on_stretch_failed)
        self.stretch_busy_signal.emit(True)
        self._thread_pool.start(worker)

    def _on_stretch_finished(self, job_id, audio, preserve_position):
        if job_id != self._stretch_job_id:
            log.debug(f"discarding stale stretch result {job_id}")
            return
        self._stretch_pending = None
        self.stretch_busy_signal.emit(False)

        was_playing = self.isPlaying()
        time_ms = self.position()
        # metadata.duration reflects the stream currently loaded
        current_audio_duration = self._current_song.metadata.duration * 1000
        normalized_pos = time_ms / current_audio_duration if current_audio_duration > 0 else 0

        self._current_song.set_stretched(audio)
        self._loaded_rate = self._playback_rate
        self.create_audio_stream(audio, emit_audio_ready_signal=False)
        new_duration = self._current_song.metadata.duration * 1000

        if preserve_position:
            self.setPosition(int(new_duration * normalized_pos))
        if was_playing:
            self.play()

    def _on_stretch_failed(self, job_id, message):
        if job_id != self._stretch_job_id:
            return
        self._stretch_pending = None
        self.stretch_busy_signal.emit(False)
        self.stretch_failed_signal.emit(message)

    def create_audio_stream(self, audio_data, emit_audio_ready_signal=True):
        """
        Creates audio stream from audio_data.

        :param audio_data:
        :param emit_audio_ready_signal:
        :return:
        """
        self.stop()
        self.buffer.close()
        self._pending_position = None
        self._stream_ready = False

        # Write to an IO Stream as a wav file:
        f = io.BytesIO()
        wavfile.write(f, self._current_song.sample_rate, audio_data)
        self.buffer = QBuffer()
        self.buffer.setData(f.getvalue())
        self.setSourceDevice(self.buffer)
        self.audio_output.setVolume(self._volume)   # Volume persists across streams/songs
        self.play()
        self.pause()    # Play/pause to force a buffer of the media

        if emit_audio_ready_signal:
            self.audio_ready_signal.emit(True)

    _READY_STATUSES = (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferingMedia,
                       QMediaPlayer.MediaStatus.BufferedMedia, QMediaPlayer.MediaStatus.EndOfMedia)
    SEEK_TOLERANCE_MS = 150     # A confirmed position within this of the target counts as "landed".
    MAX_SEEK_ATTEMPTS = 25

    def _on_media_status_changed(self, status):
        log.info(f"media status: {status}, duration={self.duration()}, position={self.position()}")
        if status in self._READY_STATUSES:
            self._stream_ready = True
            self._apply_pending_position("mediaStatusChanged")
        elif status in (QMediaPlayer.MediaStatus.NoMedia, QMediaPlayer.MediaStatus.InvalidMedia):
            self._stream_ready = False

    def _apply_pending_position(self, trigger=""):
        """
        (Re-)issue a pending seek until the backend confirms the position, then clear it.

        The media backend silently drops seeks issued while a freshly created stream is still loading/starting,
        and may rewind to 0 when playback is first primed, so a single setPosition() call is not enough.
        """
        if self._pending_position is None:
            return
        if not self._stream_ready or self.duration() <= 0:
            return

        target = min(self._pending_position, self.duration())
        # Landed if we are at the target, or slightly past it (playback may have advanced since the seek).
        if -self.SEEK_TOLERANCE_MS <= self.position() - target <= 1000:
            log.info(f"seek to {target} confirmed ({trigger})")
            self._pending_position = None
            return

        if self._pending_attempts >= self.MAX_SEEK_ATTEMPTS:
            log.warning(f"giving up seeking to {target}; player reports {self.position()}")
            self._pending_position = None
            return

        self._pending_attempts += 1
        log.info(f"seek attempt {self._pending_attempts} to {target} ({trigger}); player at {self.position()}")
        super().setPosition(target)

    def setPosition(self, position):
        """
        Set position of the scrubber.

        The seek is remembered as pending until the backend reports a position at the target
        (see _apply_pending_position), which covers seeks issued right after a new stream was created.

        :param position:
        :return:
        """
        position = max(int(position), 0)
        log.info(f"set position: {position} (ready={self._stream_ready}, duration={self.duration()})")
        self._pending_position = position
        self._pending_attempts = 0
        self._apply_pending_position("setPosition")
