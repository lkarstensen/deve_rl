import platform
from typing import Any, Dict, List, Optional, Tuple
from random import randint
import logging
import logging.config
import os
import traceback
import queue

from torch import multiprocessing as mp
import torch

from .agent import (
    Agent,
    EpisodeCounterShared,
    StepCounterShared,
    StepCounter,
    EpisodeCounter,
)
from .single import Single, Algo, ReplayBuffer, gym
from ..replaybuffer import Episode


def file_handler_callback(handler: logging.FileHandler):
    handler_dict = {
        handler.name: {
            "level": handler.level,
            "class": "logging.FileHandler",
            "filename": handler.baseFilename,
            "mode": handler.mode,
        }
    }
    if handler.formatter is not None:
        formatter_name = handler.name or randint(1, 99999)
        handler_dict[handler.name]["formatter"] = str(formatter_name)
        # pylint: disable=protected-access
        formatter_dict = {str(formatter_name): {"format": handler.formatter._fmt}}
    else:
        formatter_dict = None
    return handler_dict, formatter_dict, handler.name


handler_callback = {logging.FileHandler: file_handler_callback}


def get_logging_config_dict():
    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {},
        "handlers": {},
        "loggers": {
            "": {
                "handlers": [],
                "level": logging.WARNING,
                "propagate": False,
            },  # root logger
        },
    }

    config["loggers"][""]["level"] = logging.root.level
    config["loggers"][""]["propagate"] = logging.root.propagate
    for handler in logging.root.handlers:
        handler_dict, formatter_dict, name = handler_callback[type(handler)](handler)
        if formatter_dict is not None:
            config["formatters"].update(formatter_dict)
        config["handlers"].update(handler_dict)
        config["loggers"][""]["handlers"].append(name)
    return config


def run(
    algo: Algo,
    env_train: gym.Env,
    env_eval: gym.Env,
    replay_buffer: ReplayBuffer,
    device: torch.device,
    consecutive_action_steps: int,
    normalize_actions,
    log_config_dict: Dict,
    task_queue,
    result_queue,
    model_queue,
    step_counter,
    episode_counter,
    shutdown,
    is_shutdown,
    name,
    nice_level: int,
):
    if platform.system() != "Windows":
        os.nice(nice_level)

    try:
        torch.set_num_threads(4)
        for handler_name, handler_config in log_config_dict["handlers"].items():
            if "filename" in handler_config.keys():
                filename = handler_config["filename"]
                path, _ = os.path.split(filename)
                path = os.path.join(path, "logs_subprocesses")
                if not os.path.isdir(path):
                    os.mkdir(path)
                filename = os.path.join(path, f"{name}.log")
                log_config_dict["handlers"][handler_name]["filename"] = filename
        logging.config.dictConfig(log_config_dict)
        logger = logging.getLogger(__name__)
        logger.info("logger initialized")
        agent = Single(
            algo,
            env_train,
            env_eval,
            replay_buffer,
            device,
            consecutive_action_steps,
            normalize_actions,
        )
        agent.step_counter = step_counter
        agent.episode_counter = episode_counter
        while not shutdown.is_set():
            try:
                task = task_queue.get(timeout=1)
            except queue.Empty:
                continue
            task_name = task[0]

            if task_name in ["load_state_dicts_network", "state_dicts_network"]:
                log_debug = f"Received {task[0]=} with {len(task)=}"
            else:
                log_debug = f"Received {task=}"
            logger.debug(log_debug)
            if task_name == "heatup":
                result = agent.heatup(
                    steps=task[1],
                    episodes=task[2],
                    step_limit=task[3],
                    episode_limit=task[4],
                    custom_action_low=task[5],
                    custom_action_high=task[6],
                )
            elif task_name == "explore":
                result = agent.explore(
                    steps=task[1],
                    episodes=task[2],
                    step_limit=task[3],
                    episode_limit=task[4],
                )
            elif task_name == "evaluate":
                result = agent.evaluate(
                    steps=task[1],
                    episodes=task[2],
                    step_limit=task[3],
                    episode_limit=task[4],
                    seeds=task[5],
                    options=task[6],
                )
            elif task_name == "update":
                try:
                    result = agent.update(steps=task[1], step_limit=task[2])
                except ValueError as error:
                    log_warning = f"Update Error: {error}"
                    logger.warning(log_warning)
                    shutdown.set()
                    result = error
            elif task_name == "explore_and_update":
                result = agent.explore_and_update(
                    explore_steps=task[1],
                    explore_episodes=task[2],
                    explore_step_limit=task[3],
                    explore_episode_limit=task[4],
                    update_steps=task[5],
                    update_step_limit=task[6],
                )
            elif task_name == "state_dicts_network":
                destination = task[1]
                state_dicts = agent.algo.state_dicts_network(destination)
                model_queue.put(state_dicts)
                del state_dicts
                continue
            elif task_name == "load_state_dicts_network":
                state_dicts = task[1]
                agent.algo.load_state_dicts_network(state_dicts)
                del state_dicts
                continue
            elif task_name == "state_dicts_optimizer":
                state_dicts = agent.algo.state_dicts_optimizer()
                model_queue.put(state_dicts)
                del state_dicts
                continue
            elif task_name == "load_state_dicts_optimizer":
                state_dicts = task[1]
                agent.algo.load_state_dicts_optimizer(state_dicts)
                del state_dicts
                continue
            elif task_name == "state_dicts_scheduler":
                state_dicts = agent.algo.state_dicts_scheduler()
                model_queue.put(state_dicts)
                del state_dicts
                continue
            elif task_name == "load_state_dicts_scheduler":
                state_dicts = task[1]
                agent.algo.load_state_dicts_scheduler(state_dicts)
                del state_dicts
                continue
            elif task_name == "shutdown":
                break
            else:
                continue
            result_queue.put(result)
    except Exception as exception:  # pylint: disable=broad-exception-caught
        exception_traceback = "".join(traceback.format_tb(exception.__traceback__))
        logger.warning("Traceback:\n" + exception_traceback)
        logger.warning(exception)
        result_queue.put(exception)
    agent.close()

    for queue_ in [result_queue, model_queue, task_queue]:
        while True:
            try:
                queue_.get_nowait()
            except queue.Empty:
                queue_.close()
                break
    is_shutdown.set()


class SingleAgentProcess(Agent):
    def __init__(
        self,
        agent_id: int,
        algo: Algo,
        env_train: gym.Env,
        env_eval: gym.Env,
        replay_buffer: ReplayBuffer,
        device: torch.device,
        consecutive_action_steps: int,
        normalize_actions: bool,
        name: str,
        parent_agent: Agent,
        step_counter: StepCounterShared = None,
        episode_counter: EpisodeCounterShared = None,
        nice_level: int = 0,
    ) -> None:
        self.logger = logging.getLogger(self.__module__)
        self.agent_id = agent_id
        self.name = name
        self._shutdown = mp.Event()
        self._is_shutdown = mp.Event()
        self._task_queue = mp.Queue()
        self._result_queue = mp.Queue()
        self._model_queue = mp.Queue()

        self.device = device
        self.parent_agent = parent_agent

        self._step_counter = step_counter or StepCounterShared()
        self._episode_counter = episode_counter or EpisodeCounterShared()
        logging_config = get_logging_config_dict()

        for handler_config in logging_config["handlers"].values():
            if "filename" in handler_config.keys():
                filename = handler_config["filename"]
                path, _ = os.path.split(filename)
                path = os.path.join(path, "logs_subprocesses")
                if not os.path.isdir(path):
                    os.mkdir(path)

        self._process = mp.Process(
            target=run,
            args=[
                algo,
                env_train,
                env_eval,
                replay_buffer,
                device,
                consecutive_action_steps,
                normalize_actions,
                logging_config,
                self._task_queue,
                self._result_queue,
                self._model_queue,
                self.step_counter,
                self.episode_counter,
                self._shutdown,
                self._is_shutdown,
                name,
                nice_level,
            ],
            name=name,
        )
        self._process.start()

    def heatup(
        self,
        *,
        steps: Optional[int] = None,
        episodes: Optional[int] = None,
        step_limit: Optional[int] = None,
        episode_limit: Optional[int] = None,
        custom_action_low: Optional[List[float]] = None,
        custom_action_high: Optional[List[float]] = None,
    ) -> None:
        self._task_queue.put(
            [
                "heatup",
                steps,
                episodes,
                step_limit,
                episode_limit,
                custom_action_low,
                custom_action_high,
            ]
        )

    def explore(
        self,
        *,
        steps: Optional[int] = None,
        episodes: Optional[int] = None,
        step_limit: Optional[int] = None,
        episode_limit: Optional[int] = None,
    ) -> None:
        try:
            self._task_queue.put(
                ["explore", steps, episodes, step_limit, episode_limit]
            )
        except ValueError:
            self.close()

    def evaluate(
        self,
        *,
        steps: Optional[int] = None,
        episodes: Optional[int] = None,
        step_limit: Optional[int] = None,
        episode_limit: Optional[int] = None,
        seeds: Optional[List[int]] = None,
        options: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        try:
            self._task_queue.put(
                ["evaluate", steps, episodes, step_limit, episode_limit, seeds, options]
            )
        except ValueError:
            self.close()

    def update(
        self, *, steps: Optional[int] = None, step_limit: Optional[int] = None
    ) -> None:
        try:
            self._task_queue.put(["update", steps, step_limit])
        except ValueError:
            self.close()

    def explore_and_update(
        self,
        *,
        explore_steps: Optional[int] = None,
        explore_episodes: Optional[int] = None,
        explore_step_limit: Optional[int] = None,
        explore_episode_limit: Optional[int] = None,
        update_steps: Optional[int] = None,
        update_step_limit: Optional[int] = None,
    ) -> Tuple[List[Episode], List[float]]:
        try:
            self._task_queue.put(
                [
                    "explore_and_update",
                    explore_steps,
                    explore_episodes,
                    explore_step_limit,
                    explore_episode_limit,
                    update_steps,
                    update_step_limit,
                ]
            )
        except ValueError:
            self.close()

    def get_result(self, timeout: float) -> List[Any]:
        try:
            result = self._result_queue.get(timeout=timeout)
        except queue.Empty as error:
            result = error
        except ValueError:
            self.close()
            result = []
        return result

    def state_dicts_network(self, destination: Dict[str, Any] = None) -> Dict[str, Any]:
        try:
            self._task_queue.put(["state_dicts_network", destination])
            return self._model_queue.get()
        except ValueError:
            self.close()
            return None

    def load_state_dicts_network(self, states_dict: Dict[str, Any]):
        try:
            self._task_queue.put(["load_state_dicts_network", states_dict])
        except ValueError:
            self.close()

    def state_dicts_optimizer(self) -> Dict[str, Any]:
        try:
            self._task_queue.put(["state_dicts_optimizer"])
            return self._model_queue.get()
        except ValueError:
            self.close()
            return None

    def load_state_dicts_optimizer(self, states_dict: Dict[str, Any]):
        try:
            self._task_queue.put(["load_state_dicts_optimizer", states_dict])
        except ValueError:
            self.close()

    def state_dicts_scheduler(self) -> Dict[str, Any]:
        try:
            self._task_queue.put(["state_dicts_scheduler"])
            return self._model_queue.get()
        except ValueError:
            self.close()
            return None

    def load_state_dicts_scheduler(self, states_dict: Dict[str, Any]):
        try:
            self._task_queue.put(["load_state_dicts_scheduler", states_dict])
        except ValueError:
            self.close()

    def close(self) -> None:
        if self._process is not None and self._process.is_alive():
            self._shutdown.set()
            self._task_queue.put(["shutdown"])
            self._process.join(5)
            exitcode = self._process.exitcode
            if exitcode is None:
                if not self._is_shutdown.is_set():
                    self._clear_queues()
                    self._close_queues()
                self._process.kill()
                self._process.join()
            self._process.close()
            self._process = None

    def is_alive(self) -> bool:
        if self._process is None:
            return False
        return self._process.is_alive()

    def _clear_queues(self):
        for queue_ in [self._result_queue, self._model_queue, self._task_queue]:
            while True:
                try:
                    queue_.get_nowait()
                except (queue.Empty, ValueError):
                    break

    def _close_queues(self):
        for queue_ in [self._result_queue, self._model_queue, self._task_queue]:
            queue_.close()

    @property
    def step_counter(self) -> StepCounterShared:
        return self._step_counter

    @step_counter.setter
    def step_counter(self, new_counter: StepCounter) -> None:
        self._step_counter.heatup = new_counter.heatup
        self._step_counter.exploration = new_counter.exploration
        self._step_counter.evaluation = new_counter.evaluation
        self._step_counter.update = new_counter.update

    @property
    def episode_counter(self) -> EpisodeCounterShared:
        return self._episode_counter

    @episode_counter.setter
    def episode_counter(self, new_counter: EpisodeCounter) -> None:
        self._episode_counter.heatup = new_counter.heatup
        self._episode_counter.exploration = new_counter.exploration
        self._episode_counter.evaluation = new_counter.evaluation
