# Model Predictive Actor-Critic Reinforcement Learning


## Installation
1. Install MuJoCo (https://www.roboti.us/index.html) at `~/.mujoco/mjpro150` and copy your license key to `~/.mujoco/mjkey.txt`
2. Clone `mopac`
```
git clone --recursive https://github.com/dnandha/mopac.git
```
3. Create a conda environment and install mopac
```
cd mopac
conda env create -f environment/gpu-env.yml
conda activate mopac
pip install -e viskit
pip install -e .
```

## Usage
Configuration files can be found in [`mopac/examples/config/`](mopac/examples/config).

```
mopac run_local mopac.examples.development --config=mopac.examples.config.halfcheetah.0 --gpus=1 --trial-gpus=1
```

Currently only running locally is supported.

#### New environments
To run on a different environment, you can modify the provided [template](examples/config/custom/0.py). You will also need to provide the termination function for the environment in [`mopac/static`](mopac/static). If you name the file the lowercase version of the environment name, it will be found automatically. See [`hopper.py`](mopac/static/hopper.py) for an example.

#### Logging

This codebase contains [viskit](https://github.com/vitchyr/viskit) as a submodule. You can view saved runs with:
```
viskit ~/ray_mopac --port 6008
```
assuming you used the default [`log_dir`](mopac/examples/config/halfcheetah/0.py#L7).

#### Hyperparameters

The rollout length schedule is defined by a length-4 list in a [config file](mopac/examples/config/halfcheetah/0.py#L31). The format is `[start_epoch, end_epoch, start_length, end_length]`, so the following:
```
'rollout_schedule': [20, 100, 1, 5] 
```
corresponds to a model rollout length linearly increasing from 1 to 5 over epochs 20 to 100. 

If you want to speed up training in terms of wall clock time (but possibly make the runs less sample-efficient), you can set a timeout for model training ([`max_model_t`](mopac/examples/config/halfcheetah/0.py#L30), in seconds) or train the model less frequently (every [`model_train_freq`](mopac/examples/config/halfcheetah/0.py#L22) steps).

## Comparing to MOPAC
If you would like to compare to MOPAC but do not have the resources to re-run all experiments, the learning curves found in Figure 2 of the paper (plus on the Humanoid environment) are available in this [shared folder](https://drive.google.com/drive/folders/1matvC7hPi5al9-5S2uL4GuXfT5rzO9qU?usp=sharing). See `plot.py` for an example of how to read the pickle files with the results.

## Reference
If you find this code useful in an academic setting, please cite:

```
@article{janner2019mopac,
  author = {Michael Janner and Justin Fu and Marvin Zhang and Sergey Levine},
  title = {When to Trust Your Model: Model-Based Policy Optimization},
  journal = {arXiv preprint arXiv:1906.08253},
  year = {2019}
}
```

## Acknowledgments
The underlying soft actor-critic implementation in MOPAC comes from [Tuomas Haarnoja](https://scholar.google.com/citations?user=VT7peyEAAAAJ&hl=en) and [Kristian Hartikainen's](https://hartikainen.github.io/) [softlearning](https://github.com/rail-berkeley/softlearning) codebase. The modeling code is a slightly modified version of [Kurtland Chua's](https://kchua.github.io/) [PETS](https://github.com/kchua/handful-of-trials) implementation.


