from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

import numpy as np
import librosa
import torch

from lhotse.features.base import FeatureExtractor, register_extractor

from lhotse.utils import (
    EPSILON,
    Seconds,
    asdict_nonull,
)

def log_mel_spectrogram(
    audio: Union[np.ndarray, torch.Tensor],
    n_mels: int = 80,
    n_fft: int = 400,
    hop_length: int = 160,
    sampling_rate: int = 16000,
    padding: int = 0,
    device: Optional[Union[str, torch.device]] = None,
):
    """
    From https://github.com/openai/whisper/blob/main/whisper/audio.py

    Compute the log-Mel spectrogram of

    Parameters
    ----------
    audio: Union[str, np.ndarray, torch.Tensor], shape = (*)
        The path to audio or either a NumPy array or Tensor containing the audio waveform in 16 kHz

    n_mels: int
        The number of Mel-frequency filters, only 80 is supported

    padding: int
        Number of zero samples to pad to the right

    device: Optional[Union[str, torch.device]]
        If given, the audio tensor is moved to this device before STFT

    Returns
    -------
    torch.Tensor, shape = (80, n_frames)
        A Tensor that contains the Mel spectrogram
    """
    if not torch.is_tensor(audio):
        audio = torch.from_numpy(audio)

    if device is not None:
        audio = audio.to(device)
    if padding > 0:
        audio = F.pad(audio, (0, padding))
    window = torch.hann_window(n_fft).to(audio.device)
    stft = torch.stft(audio, n_fft, hop_length, window=window, return_complex=True)
    magnitudes = stft[..., :-1].abs() ** 2

    filters = librosa.filters.mel(sr=sampling_rate, n_fft=n_fft, n_mels=n_mels)
    filters = torch.from_numpy(filters).to(device)
    mel_spec = filters @ magnitudes

    log_spec = torch.clamp(mel_spec, min=1e-10).log10()
    log_spec = torch.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0
    return log_spec

@dataclass
class WhisperFbankConfig:
    sampling_rate: int = 16000
    num_filters: int = 80
    hop_length: int = 160
    n_fft: int = 400
    device: str = "cpu"

    def to_dict(self) -> Dict[str, Any]:
        return asdict_nonull(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "WhisperFbankConfig":
        return WhisperFbankConfig(**data)


@register_extractor
class WhisperFbank(FeatureExtractor):
    name = "whisper-fbank"
    config_type = WhisperFbankConfig

    def __init__(self, config: Optional[WhisperFbankConfig] = None):
        super().__init__(config=config)
        config_dict = self.config.to_dict()
        config_dict.pop("device")

    @property
    def device(self) -> Union[str, torch.device]:
        return self.config.device

    @property
    def frame_shift(self) -> Seconds:
        return self.config.hop_length / self.config.sampling_rate

    def to(self, device: str):
        self.config.device = device

    def feature_dim(self, sampling_rate: int) -> int:
        return self.config.num_filters

    def extract(
        self, samples: Union[np.ndarray, torch.Tensor], sampling_rate: int
    ) -> Union[np.ndarray, torch.Tensor]:
        assert sampling_rate == self.config.sampling_rate, (
            f"Fbank was instantiated for sampling_rate "
            f"{self.config.sampling_rate}, but "
            f"sampling_rate={sampling_rate} was passed to extract(). "
            "Note you can use CutSet/RecordingSet.resample() to change the audio sampling rate."
        )

        is_numpy = False
        if not isinstance(samples, torch.Tensor):
            samples = torch.from_numpy(samples)
            is_numpy = True

        feats = log_mel_spectrogram(
            samples,
            n_mels=self.config.num_filters,
            n_fft=self.config.n_fft,
            hop_length=self.config.hop_length,
            sampling_rate=self.config.sampling_rate,
            device=self.device,
        )

        if is_numpy:
            return feats.cpu().numpy()
        else:
            return feats

    @staticmethod
    def mix(
        features_a: np.ndarray, features_b: np.ndarray, energy_scaling_factor_b: float
    ) -> np.ndarray:
        return np.log(
            np.maximum(
                # protection against log(0); max with EPSILON is adequate since these are energies (always >= 0)
                EPSILON,
                np.exp(features_a) + energy_scaling_factor_b * np.exp(features_b),
            )
        )

    @staticmethod
    def compute_energy(features: np.ndarray) -> float:
        return float(np.sum(np.exp(features)))
