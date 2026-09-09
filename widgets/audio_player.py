import logging

from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtCore import QUrl, QBuffer, Signal

from tinytag import TinyTag
import io
from scipy.io import wavfile
import librosa
import numpy as np

from audio_effects.time_stretch import time_stretch_audio_array, float_to_int16

log = logging.getLogger(__name__)


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

    def modify_rate(self, rate=1.0):
        """
        Set audio playback rate. Return modified audio.

        :param rate:
        :return:
        """
        if self.raw_audio is None:
            return

        if abs(rate - 1.0) < 1e-5:
            modified_audio = self.raw_audio
        else:
            modified_audio = time_stretch_audio_array(self.raw_audio, self.sample_rate, rate) # , ndt=2, method='phase_vocoder'
            modified_audio = (modified_audio * self.raw_audio.max()).astype(np.int16)

        self.metadata.duration = (modified_audio.shape[0] - 1) / self.sample_rate
        return modified_audio


class AudioPlayer(QMediaPlayer):
    audio_ready_signal = Signal(bool)

    def __init__(self, parent):
        super().__init__(parent=parent)

        self.audio_output = QAudioOutput()
        self.setAudioOutput(self.audio_output)
        self._current_song = Song()
        self.buffer = QBuffer()
        self._volume = 1.0
        self._pending_position = None   # A seek requested before the new stream was loaded.
        self._stream_ready = False      # False from setSourceDevice() until the backend reports the media loaded.
        self.mediaStatusChanged.connect(self._on_media_status_changed)
        self.durationChanged.connect(lambda duration: self._apply_pending_position())

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
        self.create_audio_stream(song.raw_audio)

    def setVolume(self, volume: float):
        """
        Set the output volume (0.0 - 1.0). Remembered so it can be re-applied when a new stream is created.
        """
        self._volume = max(0.0, min(1.0, float(volume)))
        self.audio_output.setVolume(self._volume)

    def volume(self) -> float:
        return self._volume

    def setPlaybackRate(self, rate, preserve_position=True):
        """
        Set song playback rate

        :param rate:
        :param preserve_position:
        :return:
        """
        if self._current_song.raw_audio is None:
            log.info("No audio currently imported.")
            return
        log.info(f"set playback {rate=}")
        was_playing = self.isPlaying()
        self.pause()
        time_ms = self.position()
        # metadata.duration always reflects the duration of the stream currently loaded (modify_rate updates it)
        current_audio_duration = self._current_song.metadata.duration * 1000
        normalized_pos = time_ms / current_audio_duration if current_audio_duration > 0 else 0

        new_audio = self._current_song.modify_rate(rate=rate)
        self.create_audio_stream(new_audio, emit_audio_ready_signal=False)
        new_duration = self._current_song.metadata.duration * 1000

        if preserve_position:
            self.setPosition(int(new_duration * normalized_pos))
            if was_playing:
                self.play()

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

    def _on_media_status_changed(self, status):
        log.debug(f"media status: {status}")
        if status in self._READY_STATUSES:
            self._stream_ready = True
            self._apply_pending_position()
        elif status in (QMediaPlayer.MediaStatus.NoMedia, QMediaPlayer.MediaStatus.InvalidMedia):
            self._stream_ready = False

    def _apply_pending_position(self):
        """Apply a seek that was requested while the stream was still loading (see setPosition)."""
        if self._pending_position is None or not self._stream_ready or self.duration() <= 0:
            return
        position, self._pending_position = self._pending_position, None
        self.setPosition(position)

    def setPosition(self, position):
        """
        Set position of the scrubber.

        Seeks issued while a freshly created stream is still loading are silently dropped by the media backend,
        so those are queued and applied once the backend reports the media as loaded.

        :param position:
        :return:
        """
        position = max(int(position), 0)
        if not self._stream_ready or self.duration() <= 0:
            log.info(f"stream not ready; deferring set position: {position}")
            self._pending_position = position
            return
        position = min(position, self.duration())
        log.info(f"set position: {position}")
        super().setPosition(position)

