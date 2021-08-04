import queue
from typing import List, Tuple

from .agent import Agent
from .singleagent import SingleAgent, dict_state_to_flat_np_state
from ..algo import Algo
from ..replaybuffer import ReplayBuffer, Episode
from ..environment import Environment, EnvFactory
from torch import multiprocessing as mp
from math import inf, ceil
import numpy as np
import torch


class SingleAgentProcess(mp.Process, SingleAgent):
    def __init__(
        self,
        algo: Algo,
        env: Environment,
        replay_buffer: ReplayBuffer,
        consecutive_action_steps: int = 1,
    ) -> None:
        mp.Process.__init__(
            self,
        )
        SingleAgent.__init__(self, algo, env, replay_buffer, consecutive_action_steps)

        self._shutdown_event = mp.Event()
        self._task_queue = mp.Queue()
        self._result_queue = mp.Queue()
        self._model_queue = mp.SimpleQueue()

        self.explore_step_counter_mp = mp.Value("i", 0)
        self.explore_episode_counter_mp = mp.Value("i", 0)
        self.update_step_counter_mp = mp.Value("i", 0)
        self.eval_step_counter_mp = mp.Value("i", 0)
        self.eval_episode_counter_mp = mp.Value("i", 0)

    def run(self):
        while not self._shutdown_event.is_set():
            try:
                task = self._task_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            task_name = task[0]
            if task_name == "heatup":
                result = self._heatup(task[1], task[2])
            elif task_name == "explore":
                result = self._explore(task[1], task[2])
                self.explore_episode_counter_mp.value = self.explore_episode_counter
                self.explore_step_counter_mp.value = self.explore_step_counter
            elif task_name == "evaluate":
                result = self._evaluate(task[1], task[2])
                self.eval_step_counter_mp.value = self.eval_step_counter
                self.eval_episode_counter_mp.value = self.eval_episode_counter
            elif task_name == "update":
                self._update(task[1], task[2])
                self.update_step_counter_mp.value = self.update_step_counter
                device = self.algo.model.device
                self.algo.to(torch.device("cpu"))
                self._model_queue.put(self.algo.model.all_state_dicts())
                self.algo.to(device)
                continue
            elif task_name == "set_all_state_dicts":
                device = self.algo.model.device
                self.algo.to(torch.device("cpu"))
                result = self.algo.model.load_all_state_dicts(task[1])
                self.algo.to(device)
                continue
            self._result_queue.put(result)

        while True:
            try:
                self._task_queue.get(block=False)
            except queue.Empty:
                break

        while True:
            try:
                self._result_queue.get(block=False)
            except queue.Empty:
                break

    def heatup(self, steps: int = None, episodes: int = None) -> Tuple[float, float]:
        self._task_queue.put(["heatup", steps, episodes])

    def explore(self, steps: int = None, episodes: int = None) -> Tuple[float, float]:
        self._task_queue.put(["explore", steps, episodes])

    def evaluate(self, steps: int = None, episodes: int = None) -> Tuple[float, float]:
        self._task_queue.put(["evaluate", steps, episodes])

    def update(self, steps, batch_size):
        self._task_queue.put(["update", steps, batch_size])

    def set_all_state_dicts(self, all_state_dicts):
        self._task_queue.put(["set_all_state_dicts", all_state_dicts])

    def get_result(self):
        return self._result_queue.get()

    def get_state_dict(self):
        return self._model_queue.get()

    def shutdown(self):
        self._shutdown_event.set()


class ParallelAgent(Agent):
    def __init__(
        self,
        n_agents: int,
        algo: Algo,
        env_factory: EnvFactory,
        replay_buffer: ReplayBuffer,
        consecutive_action_steps: int = 1,
        model_update_per_agent_tau=0.35,
    ) -> None:

        self.algo = algo
        self.dummy_algo = self.algo.copy()
        # self.dummy_algo.model.to(self.algo.device)
        self.env_factory = env_factory
        self.replay_buffer = replay_buffer
        self.consecutive_action_steps = consecutive_action_steps
        self.n_agents = n_agents
        self.model_update_per_agent_tau = model_update_per_agent_tau

        self.agents: List[SingleAgentProcess] = []

        for _ in range(n_agents):
            algo_copy = self.algo.copy()
            algo_copy.to(self.algo.device)
            self.agents.append(
                SingleAgentProcess(
                    algo_copy,
                    self.env_factory.create_env(),
                    replay_buffer.copy(),
                    consecutive_action_steps,
                )
            )

        self.algo.to(torch.device("cpu"))

        for agent in self.agents:
            agent.start()

    def _update(self, steps, batch_size):
        steps_per_agent = ceil(steps / self.n_agents)
        for agent in self.agents:
            agent.update(steps_per_agent, batch_size)
        for agent in self.agents:
            state_dict = agent.get_state_dict()
            self.dummy_algo.model.load_all_state_dicts(state_dict)
            all_parameters = self.dummy_algo.model.all_parameters()
            self.algo.model.soft_tau_update_all(all_parameters, self.model_update_per_agent_tau)

        all_state_dicts = self.algo.model.all_state_dicts()
        for agent in self.agents:
            agent.set_all_state_dicts(all_state_dicts)

    def _heatup(self, steps: int = None, episodes: int = None) -> Tuple[float, float]:
        steps_per_agent, episodes_per_agent = self._divide_steps_and_episodes(steps, episodes)
        for agent in self.agents:
            agent.heatup(steps_per_agent, episodes_per_agent)
        results = []
        for agent in self.agents:
            results.append(agent.get_result())

        results = np.array(results)
        results = np.mean(results, axis=0)
        return tuple(results)

    def _explore(self, steps: int = None, episodes: int = None) -> Tuple[float, float]:
        steps_per_agent, episodes_per_agent = self._divide_steps_and_episodes(steps, episodes)
        for agent in self.agents:
            agent.explore(steps_per_agent, episodes_per_agent)
        results = []
        for agent in self.agents:
            results.append(agent.get_result())

        results = np.array(results)
        results = np.mean(results, axis=0)
        return tuple(results)

    def _evaluate(self, steps: int = None, episodes: int = None) -> Tuple[float, float]:
        steps_per_agent, episodes_per_agent = self._divide_steps_and_episodes(steps, episodes)
        for agent in self.agents:
            agent.evaluate(steps_per_agent, episodes_per_agent)
        results = []
        for agent in self.agents:
            results.append(agent.get_result())

        results = np.array(results)
        results = np.mean(results, axis=0)
        return tuple(results)

    def close(self):
        for agent in self.agents:
            agent.shutdown()

    def _divide_steps_and_episodes(self, steps, episodes) -> Tuple[int, int]:
        if steps < inf:
            steps_per_agent = ceil(steps / self.n_agents)
        else:
            steps_per_agent = inf
        if episodes < inf:
            episodes_per_agent = ceil(episodes / self.n_agents)
        else:
            episodes_per_agent = inf
        return steps_per_agent, episodes_per_agent

    @property
    def explore_step_counter(self) -> int:
        result = 0
        for agent in self.agents:
            result += agent.explore_step_counter_mp.value
        return result

    @property
    def explore_episode_counter(self) -> int:
        result = 0
        for agent in self.agents:
            result += agent.explore_episode_counter_mp.value
        return result

    @property
    def eval_step_counter(self) -> int:
        result = 0
        for agent in self.agents:
            result += agent.eval_step_counter_mp.value
        return result

    @property
    def eval_episode_counter(self) -> int:
        result = 0
        for agent in self.agents:
            result += agent.eval_episode_counter_mp.value
        return result

    @property
    def update_step_counter(self) -> int:
        result = 0
        for agent in self.agents:
            result += agent.update_step_counter_mp.value
        return result
