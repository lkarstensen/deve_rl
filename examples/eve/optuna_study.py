import csv
import optuna
import os
from learner import sac_training
import torch.multiprocessing as mp
import argparse


def optuna_run(trial):
    cwd = os.getcwd()
    if not os.path.isdir(log_folder):
        os.mkdir(log_folder)
    lr = trial.suggest_loguniform("lr", 1e-6, 1e-2)
    gamma = trial.suggest_float("gamma", 0.98, 0.9999)
    n_layers = trial.suggest_int("n_layers", 1, 3)
    n_nodes = trial.suggest_int("n_nodes", 32, 256)
    hidden_layers = [n_nodes for _ in range(n_layers)]
    success, steps = sac_training(
        lr=lr,
        gamma=gamma,
        hidden_layers=hidden_layers,
        id=trial.number,
        log_folder=log_folder + "/" + name,
        n_worker=n_worker,
        n_trainer=n_trainer,
        env=env,
    )
    with open(log_folder + "/" + name + ".csv", "a+") as csvfile:
        writer = csv.writer(csvfile, delimiter=";")
        writer.writerow([trial.number, success, trial.params, steps])
    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Optuna Study.")
    parser.add_argument("name", type=str, help="an integer for the accumulator")
    parser.add_argument("logfolder", type=str, help="Folder to save logfiles")
    parser.add_argument("env", type=str, help="Environment to use")
    parser.add_argument("n_trials", type=int, help="number of study trials")
    parser.add_argument("n_worker", type=int, help="Amount of Exploration Workers")
    parser.add_argument("n_trainer", type=int, help="Amount of NN Training Agents")
    args = parser.parse_args()
    name = args.name
    n_trials = args.n_trials
    log_folder = args.logfolder
    n_worker = args.n_worker
    n_trainer = args.n_trainer
    env = args.env
    mp.set_start_method("spawn", force=True)
    study = optuna.create_study(
        study_name=name,
        direction="maximize",
        sampler=optuna.samplers.RandomSampler(),
    )
    study.optimize(optuna_run, n_trials=n_trials)
