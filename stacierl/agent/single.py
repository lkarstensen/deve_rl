from time import perf_counter
from typing import Any, Callable, Dict, List, Optional, Tuple
import logging
import torch
import numpy as np
import gymnasium as gym

from stacierl.replaybuffer.replaybuffer import Episode
from .agent import Agent, StepCounter, EpisodeCounter
from ..algo import Algo
from ..replaybuffer import ReplayBuffer, Episode


class Single(Agent):
    def __init__(
        self,
        algo: Algo,
        env_train: gym.Env,
        env_eval: gym.Env,
        replay_buffer: ReplayBuffer,
        device: torch.device = torch.device("cpu"),
        consecutive_action_steps: int = 1,
        normalize_actions: bool = True,
    ) -> None:
        self.logger = logging.getLogger(self.__module__)
        self.device = device
        self.algo = algo
        self.env_train = env_train
        self.env_eval = env_eval
        self.replay_buffer = replay_buffer
        self.consecutive_action_steps = consecutive_action_steps
        self.normalize_actions = normalize_actions

        self.update_error = False

        self.step_counter = StepCounter()
        self.episode_counter = EpisodeCounter()
        self.to(device)
        self._next_batch = None
        self._replay_too_small = True
        self.logger.info("Single agent initialized")

    def heatup(
        self,
        *,
        steps: Optional[int] = None,
        episodes: Optional[int] = None,
        step_limit: Optional[int] = None,
        episode_limit: Optional[int] = None,
        custom_action_low: Optional[List[float]] = None,
        custom_action_high: Optional[List[float]] = None,
    ) -> List[Episode]:
        self._log_task(
            "heatup",
            steps,
            step_limit,
            episodes,
            episode_limit,
            custom_action_low=custom_action_low,
            custom_action_high=custom_action_high,
        )
        step_limit, episode_limit = self._log_and_convert_limits(
            "heatup", steps, step_limit, episodes, episode_limit
        )

        episodes_data = []

        def random_action(*args, **kwargs):  # pylint: disable=unused-argument
            env_low = self.env_train.action_space.low.reshape(-1)
            env_high = self.env_train.action_space.high.reshape(-1)

            if custom_action_low is not None:
                action_low = np.array(custom_action_low).reshape(-1)
            else:
                action_low = env_low.reshape(-1)
            if custom_action_high is not None:
                action_high = np.array(custom_action_high).reshape(-1)
            else:
                action_high = env_high.reshape(-1)
            action = np.random.uniform(action_low, action_high)

            if self.normalize_actions:
                action = 2 * (action - env_low) / (env_high - env_low) - 1

            return action

        n_episodes = 0
        n_steps = 0
        t_start = perf_counter()
        while (
            self.step_counter.heatup < step_limit
            and self.episode_counter.heatup < episode_limit
        ):
            with self.episode_counter.lock:
                self.episode_counter.heatup += 1

            episode, n_steps_episode = self._play_episode(
                env=self.env_train,
                action_function=random_action,
                consecutive_actions=self.consecutive_action_steps,
            )

            with self.step_counter.lock:
                self.step_counter.heatup += n_steps_episode
            n_steps += n_steps_episode
            n_episodes += 1
            self.replay_buffer.push(episode)
            episodes_data.append(episode)

        t_duration = perf_counter() - t_start
        log_info = f"Heatup Steps Total: {self.step_counter.heatup}, Steps this Heatup: {n_steps}, Steps per Second: {n_steps/t_duration:.2f}"
        self.logger.info(log_info)
        return episodes_data

    def explore(
        self,
        *,
        steps: Optional[int] = None,
        episodes: Optional[int] = None,
        step_limit: Optional[int] = None,
        episode_limit: Optional[int] = None,
    ) -> List[Episode]:
        self._log_task("explore", steps, step_limit, episodes, episode_limit)
        step_limit, episode_limit = self._log_and_convert_limits(
            "exploration", steps, step_limit, episodes, episode_limit
        )

        episodes_data = []
        n_episodes = 0
        n_steps = 0
        t_start = perf_counter()

        while (
            self.step_counter.exploration < step_limit
            and self.episode_counter.exploration < episode_limit
        ):
            with self.episode_counter.lock:
                self.episode_counter.exploration += 1

            episode, n_steps_episode = self._play_episode(
                env=self.env_train,
                action_function=self.algo.get_exploration_action,
                consecutive_actions=self.consecutive_action_steps,
            )

            with self.step_counter.lock:
                self.step_counter.exploration += n_steps_episode

            n_episodes += 1
            n_steps += n_steps_episode

            self.replay_buffer.push(episode)
            episodes_data.append(episode)

        t_duration = perf_counter() - t_start
        log_text = f"Exploration Steps Total: {self.step_counter.exploration}, Steps this Exploration: {n_steps}, Steps per Second: {n_steps/t_duration:.2f}"
        self.logger.info(log_text)
        return episodes_data

    def update(
        self, *, steps: Optional[int] = None, step_limit: Optional[int] = None
    ) -> List[List[float]]:
        self._log_task("update", steps, step_limit)
        step_limit, _ = self._log_and_convert_limits("update", steps, step_limit)
        results = []
        if self._replay_too_small:
            replay_len = len(self.replay_buffer)
            batch_size = self.replay_buffer.batch_size
            self._replay_too_small = replay_len <= batch_size
        if self._replay_too_small or step_limit == 0 or steps == 0:
            return []

        n_steps = 0
        t_start = perf_counter()
        while self.step_counter.update < step_limit:
            with self.step_counter.lock:
                self.step_counter.update += 1
            batch = self.replay_buffer.sample()
            result = self.algo.update(batch)
            results.append(result)
            n_steps += 1
            while self.algo.lr_scheduler_step_counter < self.step_counter.exploration:
                self.algo.lr_scheduler_step()

        t_duration = perf_counter() - t_start
        log_text = f"Update Steps Total: {self.step_counter.update}, Steps this update: {n_steps}, Steps per Second: {n_steps/t_duration:.2f}"
        self.logger.info(log_text)

        return results

    def evaluate(
        self,
        *,
        steps: Optional[int] = None,
        episodes: Optional[int] = None,
        step_limit: Optional[int] = None,
        episode_limit: Optional[int] = None,
        seeds: Optional[List[int]] = None,
        options: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Episode]:
        self._log_task(
            "evaluate", steps, step_limit, episodes, episode_limit, seeds, options
        )
        step_limit, episode_limit = self._log_and_convert_limits(
            "evaluation", steps, step_limit, episodes, episode_limit, seeds, options
        )
        episodes_data = []
        n_episodes = 0
        n_steps = 0
        t_start = perf_counter()

        while True:
            with self.episode_counter.lock:
                self.episode_counter.evaluation += 1

            next_seed = seeds.pop(-1) if seeds is not None else None
            next_options = options.pop(-1) if options is not None else None

            episode, n_steps_episode = self._play_episode(
                env=self.env_eval,
                action_function=self.algo.get_eval_action,
                consecutive_actions=self.consecutive_action_steps,
                seed=next_seed,
                options=next_options,
            )

            with self.step_counter.lock:
                self.step_counter.evaluation += n_steps_episode

            n_episodes += 1
            n_steps += n_steps_episode
            episodes_data.append(episode)

            if (
                (not seeds and not options)
                or self.step_counter.evaluation > step_limit
                or self.episode_counter.evaluation > episode_limit
            ):
                break

        t_duration = perf_counter() - t_start
        log_text = f"Evaluation Steps Total: {self.step_counter.evaluation}, Steps this Evaluation: {n_steps}, Steps per Second: {n_steps/t_duration:.2f}"
        self.logger.info(log_text)
        return episodes_data

    def _play_episode(
        self,
        env: gym.Env,
        action_function: Callable[[np.ndarray], np.ndarray],
        consecutive_actions: int,
        seed: Optional[int] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Episode, int]:
        terminal = False
        truncation = False
        step_counter = 0

        self.algo.reset()
        obs = env.reset(seed=seed, options=options)
        flat_obs = self._flatten_obs(obs)
        episode = Episode(obs, flat_obs)

        while not (terminal or truncation):
            action = action_function(flat_obs)

            for _ in range(consecutive_actions):
                env_action = action.reshape(env.action_space.shape)
                if self.normalize_actions:
                    if isinstance(env.action_space, gym.spaces.Box):
                        env_action = (env_action + 1) / 2 * (
                            env.action_space.high - env.action_space.low
                        ) + env.action_space.low
                    else:
                        raise NotImplementedError(
                            "Normaization not implemented for this Action Space"
                        )
                obs, reward, terminal, truncation, info = env.step(env_action)
                flat_obs = self._flatten_obs(obs)
                step_counter += 1
                env.render()
                episode.add_transition(
                    obs, flat_obs, action, reward, terminal, truncation, info
                )
                if terminal or truncation:
                    break

        return episode, step_counter

    @staticmethod
    def _flatten_obs(state: Dict[str, np.ndarray]) -> np.ndarray:
        obs_list = [obs.flatten() for obs in state.values()]
        obs_np = np.concatenate(obs_list)
        return obs_np

    def to(self, device: torch.device):
        self.device = device
        self.algo.to(device)

    def close(self):
        self.env_train.close()
        if id(self.env_train) != id(self.env_eval):
            self.env_eval.close()
        self.replay_buffer.close()

    def copy(self):
        ...
