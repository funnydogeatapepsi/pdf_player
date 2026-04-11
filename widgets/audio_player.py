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

        self.file_path = file_url.path()[1:]
        self.metadata = TinyTag.get(self.file_path)
        self.__has_metadata = True

        self.read_data()

    @property
    def has_metadata(self):
        return self.__has_metadata

    def read_data(self):
        """
        Read audio file

        :return:
        """
        raw_audio, sample_rate = librosa.load(self.file_path, sr=None, mono=False)
        self.raw_audio = (raw_audio.T/raw_audio.max() * np.iinfo(np.int16).max).astype(np.int16)
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
        self.pause()
        time_ms = self.position()
        original_audio_duration = self._current_song.metadata.duration * 1000
        normalized_pos = time_ms / original_audio_duration

        new_audio = self._current_song.modify_rate(rate=rate)
        self.create_audio_stream(new_audio, emit_audio_ready_signal=False)
        new_duration = self._current_song.metadata.duration * 1000

        if preserve_position:
            self.parent().graphics_scene.scrubber_time_ms = int(new_duration * normalized_pos)

    def create_audio_stream(self, audio_data, emit_audio_ready_signal=True):
        """
        Creates audio stream from audio_data.

        :param audio_data:
        :param emit_audio_ready_signal:
        :return:
        """
        self.stop()
        self.buffer.close()

        # Write to an IO Stream as a wav file:
        f = io.BytesIO()
        wavfile.write(f, self._current_song.sample_rate, audio_data)
        self.buffer = QBuffer()
        self.buffer.setData(f.getvalue())
        self.setSourceDevice(self.buffer)
        self.play()
        self.pause()    # Play/pause to force a buffer of the media

        if emit_audio_ready_signal:
            self.audio_ready_signal.emit(True)

    def setPosition(self, position):
        """
        Set position of the scrubber.

        :param position:
        :return:
        """
        position = max(min(position, self.duration()), 0)
        log.info(f"set position: {position}")
        super().setPosition(position)

