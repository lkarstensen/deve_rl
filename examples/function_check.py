import csv
import os
from torch import optim
import torch
import numpy as np

import eve
from eve.visualisation import SofaPygame
import eve_rl
import torch.backends
import torch.backends.mps
import torch.mps


def sac_training(
    device=torch.device("cuda" if torch.cuda.is_available() else "cpu"),
    lr=0.001991743536437494,
    hidden_layers=(255, 255, 255),
    gamma=0.99,
    replay_buffer=1e6,
    training_steps=1e3,
    consecutive_explore_steps=1,
    steps_between_eval=5e2,
    eval_episodes=1,
    batch_size=2,
    heatup=1e2,
    log_folder: str = os.getcwd() + "/fast_learner_example_results/",
    id_training=0,
    name="fast_learner",
):
    if not os.path.isdir(log_folder):
        os.mkdir(log_folder)
    success = 0.0

    env = make_env()

    obs_dict = env.observation_space.sample()
    obs_list = [obs.flatten() for obs in obs_dict.values()]
    obs_np = np.concatenate(obs_list)

    n_observations = obs_np.shape[0]
    n_actions = env.action_space.sample().flatten().shape[0]
    q1_mlp = eve_rl.network.component.MLP(hidden_layers)
    q_net_1 = eve_rl.network.QNetwork(q1_mlp, n_observations, n_actions)
    q1_optimizer = eve_rl.optim.Adam(q_net_1, lr)
    q1_scheduler = optim.lr_scheduler.LinearLR(
        q1_optimizer, end_factor=5e-5, total_iters=1e5
    )

    q2_mlp = eve_rl.network.component.MLP(hidden_layers)
    q_net_2 = eve_rl.network.QNetwork(q2_mlp, n_observations, n_actions)
    q2_optimizer = eve_rl.optim.Adam(q_net_2, lr)
    q2_scheduler = optim.lr_scheduler.LinearLR(
        q2_optimizer, end_factor=5e-5, total_iters=1e5
    )

    policy_mlp = eve_rl.network.component.MLP(hidden_layers)
    policy_net = eve_rl.network.GaussianPolicy(policy_mlp, n_observations, n_actions)
    policy_optimizer = eve_rl.optim.Adam(policy_net, lr)
    policy_scheduler = optim.lr_scheduler.LinearLR(
        policy_optimizer, end_factor=5e-5, total_iters=1e5
    )

    sac_model = eve_rl.model.SACModel(
        q1=q_net_1,
        q2=q_net_2,
        policy=policy_net,
        q1_optimizer=q1_optimizer,
        q2_optimizer=q2_optimizer,
        policy_optimizer=policy_optimizer,
        q1_scheduler=q1_scheduler,
        q2_scheduler=q2_scheduler,
        policy_scheduler=policy_scheduler,
        lr_alpha=lr,
    )
    algo = eve_rl.algo.SAC(sac_model, n_actions=n_actions, gamma=gamma)
    replay_buffer = eve_rl.replaybuffer.VanillaStep(replay_buffer, batch_size)
    agent = eve_rl.agent.Single(
        algo,
        env,
        env,
        replay_buffer,
        consecutive_action_steps=1,
        device=device,
        normalize_actions=True,
    )
    while True:
        logfile = log_folder + f"/{name}_{id_training}.csv"
        if os.path.isfile(logfile):
            id_training += 1
        else:
            break
    with open(logfile, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile, delimiter=";")
        writer.writerow(["lr", "gamma", "hidden_layers"])
        writer.writerow([lr, gamma, hidden_layers])
        writer.writerow(["Episodes", "Steps", "Reward", "Success"])
    next_eval_step_limt = steps_between_eval + agent.step_counter.exploration
    training_steps += agent.step_counter.exploration
    print("starting heatup")
    agent.heatup(steps=heatup, custom_action_low=[-10.0, -1.5])
    step_counter = agent.step_counter
    print("starting training loop")
    while step_counter.exploration < training_steps:
        agent.explore(steps=consecutive_explore_steps)
        step_counter = agent.step_counter
        update_steps = step_counter.exploration - step_counter.update
        agent.update(steps=update_steps)

        if step_counter.exploration >= next_eval_step_limt:
            agent.save_checkpoint(
                os.path.join(log_folder, f"checkpoint_{step_counter.exploration}")
            )
            episodes = agent.evaluate(episodes=eval_episodes)
            rewards = [episode.episode_reward for episode in episodes]
            successes = [episode.terminals[-1] for episode in episodes]
            reward = sum(rewards) / len(rewards)
            success = sum(successes) / len(successes)
            next_eval_step_limt += steps_between_eval

            print(
                f"Steps: {step_counter.exploration}, Reward: {reward}, Success: {success}"
            )
            with open(logfile, "a+", newline="", encoding="utf-8") as csvfile:
                writer = csv.writer(csvfile, delimiter=";")
                writer.writerow(
                    [
                        agent.episode_counter.exploration,
                        step_counter.exploration,
                        reward,
                        success,
                    ]
                )

    return success, agent.step_counter.exploration


def make_env() -> eve.Env:
    vessel_tree = eve.intervention.vesseltree.AorticArch(
        seed=30,
        scaling_xyzd=[1.0, 1.0, 1.0, 0.75],
        # rotation_yzx_deg=[0, -20, -5],
    )

    device = eve.intervention.device.JShaped()

    simulation = eve.intervention.simulation.SofaBeamAdapter(friction=0.001)

    fluoroscopy = eve.intervention.fluoroscopy.TrackingOnly(
        simulation=simulation,
        vessel_tree=vessel_tree,
        image_frequency=7.5,
        image_rot_zx=[20, 5],
    )

    target = eve.intervention.target.CenterlineRandom(
        vessel_tree=vessel_tree,
        fluoroscopy=fluoroscopy,
        threshold=5,
        branches=["lcca", "rcca", "lsa", "rsa", "bct", "co"],
    )

    intervention = eve.intervention.MonoPlaneStatic(
        vessel_tree=vessel_tree,
        devices=[device],
        simulation=simulation,
        fluoroscopy=fluoroscopy,
        target=target,
    )

    start = eve.start.InsertionPoint(intervention=intervention)
    pathfinder = eve.pathfinder.BruteForceBFS(intervention=intervention)

    position = eve.observation.Tracking2D(intervention=intervention, n_points=5)
    position = eve.observation.wrapper.NormalizeTracking2DEpisode(
        position, intervention
    )
    target_state = eve.observation.Target2D(intervention=intervention)
    target_state = eve.observation.wrapper.NormalizeTracking2DEpisode(
        target_state, intervention
    )
    rotation = eve.observation.Rotations(intervention=intervention)

    state = eve.observation.ObsDict(
        {"position": position, "target": target_state, "rotation": rotation}
    )

    target_reward = eve.reward.TargetReached(
        intervention=intervention,
        factor=1.0,
    )
    path_delta = eve.reward.PathLengthDelta(
        pathfinder=pathfinder,
        factor=0.01,
    )
    reward = eve.reward.Combination([target_reward, path_delta])

    target_reached = eve.terminal.TargetReached(intervention=intervention)
    max_steps = eve.truncation.MaxSteps(200)

    visualisation = SofaPygame(intervention=intervention)

    env = eve.Env(
        intervention=intervention,
        observation=state,
        reward=reward,
        terminal=target_reached,
        truncation=max_steps,
        visualisation=visualisation,
        start=start,
        pathfinder=pathfinder,
    )
    return env


if __name__ == "__main__":
    if torch.cuda.is_available():
        trainer_device = torch.device("cuda")
    elif torch.backends.mps.is_available():
        trainer_device = torch.device("mps")
    else:
        trainer_device = torch.device("cpu")

    result = sac_training(device=trainer_device)
