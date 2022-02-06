import stacierl
import tiltmaze
import eve
import torch
from datetime import datetime
import csv
import os
import math


def sac_training(
    device=torch.device("cuda" if torch.cuda.is_available() else "cpu"),
    lr=0.005857455980764544,
    hidden_layers=[128, 128],
    gamma=0.99,
    replay_buffer=1e6,
    training_steps=1.5e5,
    consecutive_explore_steps=1,
    steps_between_eval=1e4,
    eval_episodes=100,
    batch_size=64,
    heatup=1e4,
    log_folder: str = "",
    id=0,
    name="",
    *args,
    **kwargs,
):

    if not os.path.isdir(log_folder):
        os.mkdir(log_folder)
    success = 0.0
    eve_tree = eve.vesseltree.AorticArch(seed=1234)
    vessel_tree = tiltmaze.vesseltree.FromEve3d(
        eve3d_vessel_tree=eve_tree,
        lao_rao=20,
        cra_cau=-5,
        contour_approx_margin=2.0,
        scaling_artery_diameter=0.5,
    )
    simu = tiltmaze.simulation.Guidewire(
        tip_length=25,
        tip_angle=math.pi / 2,
        flex_length=30,
        flex_element_length=1,
        flex_rotary_spring_stiffness=7e5,
        flex_rotary_spring_damping=3e2,
        stiff_element_length=2,
        stiff_rotary_spring_stiffness=1e6,
        stiff_rotary_spring_damping=3e2,
        wire_diameter=2,
        friction=1.0,
        damping=0.000001,
        velocity_limits=(50, 1.5),
        normalize_action=True,
        dt_step=1 / 7.5,
        dt_simulation=0.0002,
        body_mass=0.01,
        body_moment=0.1,
        spring_stiffness=2.5e6,
        spring_damping=100,
        last_segment_kp_angle=3,
        last_segment_kp_translation=5,
    )
    target = tiltmaze.target.CenterlineRandom(
        5,
        branches=[
            "right subclavian artery",
            "right common carotid artery",
            "left common carotid artery",
            "left subclavian artery",
            "brachiocephalic trunk",
        ],
    )
    pathfinder = tiltmaze.pathfinder.BruteForceBFS()
    start = tiltmaze.start.InsertionPoint()
    imaging = tiltmaze.imaging.ImagingDummy((500, 500))

    pos = tiltmaze.state.Position(n_points=6, resolution=1)
    pos = tiltmaze.state.wrapper.Normalize(pos)
    target_state = tiltmaze.state.Target()
    target_state = tiltmaze.state.wrapper.Normalize(target_state)
    rot = tiltmaze.state.Rotation()
    state = tiltmaze.state.Combination([pos, target_state, rot])

    target_reward = tiltmaze.reward.TargetReached(1.0)
    step_reward = tiltmaze.reward.Step(-0.005)
    path_length_reward = tiltmaze.reward.PathLengthDelta(0.001)
    reward = tiltmaze.reward.Combination([target_reward, step_reward, path_length_reward])

    done_target = tiltmaze.done.TargetReached()
    done_steps = tiltmaze.done.MaxSteps(100)
    done = tiltmaze.done.Combination([done_target, done_steps])

    success = tiltmaze.success.TargetReached()
    visu = tiltmaze.visualisation.VisualisationDummy()

    randomizer = tiltmaze.randomizer.RandomizerDummy()
    env = tiltmaze.Env(
        vessel_tree=vessel_tree,
        state=state,
        reward=reward,
        done=done,
        simulation=simu,
        start=start,
        target=target,
        imaging=imaging,
        pathfinder=pathfinder,
        visualisation=visu,
        success=success,
        randomizer=randomizer,
    )

    q_net_1 = stacierl.network.QNetwork(hidden_layers)
    q_net_2 = stacierl.network.QNetwork(hidden_layers)
    policy_net = stacierl.network.GaussianPolicy(hidden_layers, env.action_space)
    sac_model = stacierl.algo.sacmodel.Vanilla(
        q1=q_net_1,
        q2=q_net_2,
        policy=policy_net,
        learning_rate=lr,
        obs_space=env.observation_space,
        action_space=env.action_space,
    )
    algo = stacierl.algo.SAC(sac_model, action_space=env.action_space, gamma=gamma)
    replay_buffer = stacierl.replaybuffer.VanillaStep(replay_buffer, batch_size)
    agent = stacierl.agent.Single(
        algo, env, env, replay_buffer, consecutive_action_steps=1, device=device
    )

    while True:
        logfile = log_folder + f"/{name}_{id}.csv"
        if os.path.isfile(logfile):
            id += 1
        else:
            break
    with open(logfile, "w", newline="") as csvfile:
        writer = csv.writer(csvfile, delimiter=";")
        writer.writerow(["lr", "gamma", "hidden_layers"])
        writer.writerow([lr, gamma, hidden_layers])
        writer.writerow(["Episodes", "Steps", "Reward", "Success"])

    next_eval_step_limt = steps_between_eval
    agent.heatup(steps=heatup, custom_action_low=[0.0, -1.0])
    step_counter = agent.step_counter
    while step_counter.exploration < training_steps:
        agent.explore(steps=consecutive_explore_steps)
        step_counter = agent.step_counter
        update_steps = step_counter.exploration - step_counter.update
        agent.update(update_steps)

        if step_counter.exploration >= next_eval_step_limt:
            reward, success = agent.evaluate(episodes=eval_episodes)
            reward = sum(reward) / len(reward)
            success = sum(success) / len(success)
            next_eval_step_limt += steps_between_eval

            print(f"Steps: {step_counter.exploration}, Reward: {reward}, Success: {success}")
            with open(logfile, "a+", newline="") as csvfile:
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


if __name__ == "__main__":
    cwd = os.getcwd()
    log_folder = cwd + "/fast_learner_example_results/"
    result = sac_training(
        lr=0.001991743536437494,
        gamma=0.9800243887646142,
        hidden_layers=[255, 255, 255],
        log_folder=log_folder,
        batch_size=164,
    )
