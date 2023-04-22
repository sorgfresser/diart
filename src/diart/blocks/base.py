from dataclasses import dataclass
from typing import Any, Tuple, Sequence, Text, List, Union
from pathlib import Path

import numpy as np
from pyannote.core import SlidingWindowFeature

from .. import utils
from ..audio import FilePath, AudioLoader
from ..metrics import Metric


@dataclass
class HyperParameter:
    name: Text
    low: float
    high: float

    @staticmethod
    def from_name(name: Text) -> 'HyperParameter':
        if name == "tau_active":
            return TauActive
        if name == "rho_update":
            return RhoUpdate
        if name == "delta_new":
            return DeltaNew
        raise ValueError(f"Hyper-parameter '{name}' not recognized")


TauActive = HyperParameter("tau_active", low=0, high=1)
RhoUpdate = HyperParameter("rho_update", low=0, high=1)
DeltaNew = HyperParameter("delta_new", low=0, high=2)


class StreamingConfig:
    @property
    def duration(self) -> float:
        raise NotImplementedError

    @property
    def step(self) -> float:
        raise NotImplementedError

    @property
    def latency(self) -> float:
        raise NotImplementedError

    @property
    def sample_rate(self) -> int:
        raise NotImplementedError

    @staticmethod
    def from_dict(data: Any) -> 'StreamingConfig':
        raise NotImplementedError

    def get_file_padding(self, filepath: FilePath) -> Tuple[float, float]:
        file_duration = AudioLoader(self.sample_rate, mono=True).get_duration(filepath)
        right = utils.get_padding_right(self.latency, self.step)
        left = utils.get_padding_left(file_duration + right, self.duration)
        return left, right

    def optimal_block_size(self) -> int:
        return int(np.rint(self.step * self.sample_rate))


class StreamingPipeline:
    @staticmethod
    def get_config_class() -> type:
        raise NotImplementedError

    @staticmethod
    def suggest_metric() -> Metric:
        raise NotImplementedError

    @staticmethod
    def hyper_parameters() -> Sequence[HyperParameter]:
        raise NotImplementedError

    @property
    def config(self) -> StreamingConfig:
        raise NotImplementedError

    def reset(self):
        raise NotImplementedError

    def set_timestamp_shift(self, shift: float):
        raise NotImplementedError

    def join_predictions(self, predictions: List[Any]) -> Any:
        raise NotImplementedError

    def write_prediction(self, uri: Text, prediction: Any, dir_path: Union[Text, Path]):
        raise NotImplementedError

    def __call__(
        self,
        waveforms: Sequence[SlidingWindowFeature],
    ) -> Sequence[Tuple[Any, SlidingWindowFeature]]:
        raise NotImplementedError
