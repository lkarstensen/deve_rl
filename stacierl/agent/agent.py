from abc import ABC, abstractmethod
from typing import Dict, Tuple
import numpy as np
from dataclasses import dataclass


def dict_state_to_flat_np_state(state: Dict[str, np.ndarray]) -> np.ndarray:
    keys = sorted(state.keys())

    flat_state = []
    for key in keys:
        flat_state.append(state[key].flatten())
    flat_state = np.array(flat_state).flatten()
    return flat_state


@dataclass
class EpisodeCounter:
    exploration: int = 0
    eval: int = 0

    def __iadd__(self, other):
        self.exploration += other.exploration
        self.eval += other.eval
        return self


@dataclass
class StepCounter:
    exploration: int = 0
    eval: int = 0
    update: int = 0

    def __iadd__(self, other):
        self.exploration += other.exploration
        self.eval += other.eval
        self.update += other.update
        return self


class Agent(ABC):
    @abstractmethod
    def heatup(self, steps: int = None, episodes: int = None) -> Tuple[float, float]:
        ...

    @abstractmethod
    def explore(self, steps: int = None, episodes: int = None) -> Tuple[float, float]:
        ...

    @abstractmethod
    def update(self, steps) -> None:
        ...

    @abstractmethod
    def evaluate(self, steps: int = None, episodes: int = None) -> Tuple[float, float]:
        ...

    @abstractmethod
    def close(self) -> None:
        ...

    @property
    @abstractmethod
    def step_counter(self) -> StepCounter:
        ...

    @property
    @abstractmethod
    def episode_counter(self) -> EpisodeCounter:
        ...
