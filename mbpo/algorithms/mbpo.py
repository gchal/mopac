## adapted from https://github.com/rail-berkeley/softlearning/blob/master/softlearning/algorithms/sac.py

import os
import math
import pickle
from collections import OrderedDict
from numbers import Number
from itertools import count
import gtimer as gt
import pdb
from queue import Queue

import numpy as np
import tensorflow as tf
from tensorflow.python.training import training_util

from softlearning.algorithms.rl_algorithm import RLAlgorithm
from softlearning.replay_pools.simple_replay_pool import SimpleReplayPool

from mbpo.models.constructor import construct_model, format_samples_for_training
from mbpo.models.fake_env import FakeEnv
from mbpo.utils.writer import Writer
from mbpo.utils.visualization import visualize_policy
from mbpo.utils.logging import Progress
import mbpo.utils.filesystem as filesystem


def td_target(reward, discount, next_value):
    return reward + discount * next_value


class MBPO(RLAlgorithm):
    """Model-Based Policy Optimization (MBPO)

    References
    ----------
        Michael Janner, Justin Fu, Marvin Zhang, Sergey Levine. 
        When to Trust Your Model: Model-Based Policy Optimization. 
        arXiv preprint arXiv:1906.08253. 2019.
    """

    def __init__(
            self,
            training_environment,
            evaluation_environment,
            policy,
            Qs,
            Vs,
            pool,
            static_fns,
            plotter=None,
            tf_summaries=False,

            lr=3e-4,
            reward_scale=1.0,
            target_entropy='auto',
            discount=0.99,
            tau=5e-3,
            target_update_interval=1,
            action_prior='uniform',
            reparameterize=False,
            store_extra_policy_info=False,

            mopac=False,
            valuefunc=False,
            deterministic_obs=False,
            deterministic_rewards=False,

            model_train_freq=250,
            num_networks=7,
            num_elites=5,
            model_retain_epochs=20,
            rollout_batch_size=100e3,
            real_ratio=0.1,
            ratio_schedule=[0,100,0.5,0.5],
            rollout_schedule=[20,100,1,1],
            hidden_dim=200,
            max_model_t=None,
            **kwargs,
    ):
        """
        Args:
            env (`SoftlearningEnv`): Environment used for training.
            policy: A policy function approximator.
            initial_exploration_policy: ('Policy'): A policy that we use
                for initial exploration which is not trained by the algorithm.
            Qs: Q-function approximators. The min of these
                approximators will be used. Usage of at least two Q-functions
                improves performance by reducing overestimation bias.
            pool (`PoolBase`): Replay pool to add gathered samples to.
            plotter (`QFPolicyPlotter`): Plotter instance to be used for
                visualizing Q-function during training.
            lr (`float`): Learning rate used for the function approximators.
            discount (`float`): Discount factor for Q-function updates.
            tau (`float`): Soft value function target update weight.
            target_update_interval ('int'): Frequency at which target network
                updates occur in iterations.
            reparameterize ('bool'): If True, we use a gradient estimator for
                the policy derived using the reparameterization trick. We use
                a likelihood ratio based estimator otherwise.
        """

        super(MBPO, self).__init__(**kwargs)

        obs_dim = np.prod(training_environment.observation_space.shape)
        act_dim = np.prod(training_environment.action_space.shape)
        self._model = construct_model(obs_dim=obs_dim, act_dim=act_dim, hidden_dim=hidden_dim, num_networks=num_networks, num_elites=num_elites)
        self._static_fns = static_fns
        self.fake_env = FakeEnv(self._model, self._static_fns)

        self._rollout_schedule = rollout_schedule
        self._ratio_schedule = ratio_schedule
        self._max_model_t = max_model_t

        # self._model_pool_size = model_pool_size
        # print('[ MBPO ] Model pool size: {:.2E}'.format(self._model_pool_size))
        # self._model_pool = SimpleReplayPool(pool._observation_space, pool._action_space, self._model_pool_size)

        self._mopac = mopac
        self._valuefunc = valuefunc

        self._model_retain_epochs = model_retain_epochs

        self._model_train_freq = model_train_freq
        self._rollout_batch_size = int(rollout_batch_size)
        self._deterministic_obs = deterministic_obs
        self._deterministic_rewards = deterministic_rewards
        #self._real_ratio = real_ratio

        self._log_dir = os.getcwd()
        self._writer = Writer(self._log_dir)

        self._training_environment = training_environment
        self._evaluation_environment = evaluation_environment
        self._policy = policy

        self._Qs = Qs
        self._Q_targets = tuple(tf.keras.models.clone_model(Q) for Q in Qs)

        self._Vs = Vs
        self._V_targets = tf.keras.models.clone_model(Vs)

        self._pool = pool
        self._plotter = plotter
        self._tf_summaries = tf_summaries

        self._policy_lr = lr
        self._Q_lr = lr
        self._V_lr = lr

        self._reward_scale = reward_scale
        self._target_entropy = (
            -np.prod(self._training_environment.action_space.shape)
            if target_entropy == 'auto'
            else target_entropy)
        print('[ MBPO ] Target entropy: {}'.format(self._target_entropy))

        self._discount = discount
        self._tau = tau
        self._target_update_interval = target_update_interval
        self._action_prior = action_prior

        self._reparameterize = reparameterize
        self._store_extra_policy_info = store_extra_policy_info

        observation_shape = self._training_environment.active_observation_shape
        action_shape = self._training_environment.action_space.shape

        assert len(observation_shape) == 1, observation_shape
        self._observation_shape = observation_shape
        assert len(action_shape) == 1, action_shape
        self._action_shape = action_shape

        self._build()

    def _build(self):
        self._training_ops = {}

        self._init_global_step()
        self._init_placeholders()
        self._init_actor_update()
        self._init_critic_update()
        self._init_value_update()
        self._init_mppi()

    def _train(self):
        
        """Return a generator that performs RL training.

        Args:
            env (`SoftlearningEnv`): Environment used for training.
            policy (`Policy`): Policy used for training
            initial_exploration_policy ('Policy'): Policy used for exploration
                If None, then all exploration is done using policy
            pool (`PoolBase`): Sample pool to add samples to
        """
        training_environment = self._training_environment
        evaluation_environment = self._evaluation_environment
        policy = self._policy
        pool = self._pool
        model_metrics = {}

        if not self._training_started:
            self._init_training()

            self._initial_exploration_hook(
                training_environment, self._initial_exploration_policy, pool)

        self.sampler.initialize(training_environment, policy, pool)

        gt.reset_root()
        gt.rename_root('RLAlgorithm')
        gt.set_def_unique(False)

        self._training_before_hook()

        for self._epoch in gt.timed_for(range(self._epoch, self._n_epochs)):

            self._epoch_before_hook()
            gt.stamp('epoch_before_hook')

            self._training_progress = Progress(self._epoch_length * self._n_train_repeat)
            start_samples = self.sampler._total_samples
            obs = None

            self._set_rollout_length()
            # reset U and noise
            self._reset_mppi()

            for i in count():
                samples_now = self.sampler._total_samples
                self._timestep = samples_now - start_samples

                if (samples_now >= start_samples + self._epoch_length
                    and self.ready_to_train):
                    break

                self._timestep_before_hook()
                gt.stamp('timestep_before_hook')

                self._set_real_ratio()
                if self._timestep % self._model_train_freq == 0 and self._real_ratio < 1.0:
                    self._training_progress.pause()
                    print('[ MBPO ] log_dir: {} | ratio: {}'.format(self._log_dir, self._real_ratio))
                    print('[ MBPO ] Training model at epoch {} | freq {} | timestep {} (total: {}) | epoch train steps: {} (total: {})'.format(
                        self._epoch, self._model_train_freq, self._timestep, self._total_timestep, self._train_steps_this_epoch, self._num_train_steps)
                    )

                    model_train_metrics = self._train_model(batch_size=256, max_epochs=None, holdout_ratio=0.2, max_t=self._max_model_t)
                    model_metrics.update(model_train_metrics)
                    gt.stamp('epoch_train_model')
                    
                    self._reallocate_model_pool()
                    model_rollout_metrics = self._rollout_model(gamma=self._discount, mopac=self._mopac, valuefunc=self._valuefunc,
                                                                deterministic_obs=self._deterministic_obs, deterministic_rewards=self._deterministic_rewards)
                    model_metrics.update(model_rollout_metrics)

                    gt.stamp('epoch_rollout_model')
                    # self._visualize_model(self._evaluation_environment, self._total_timestep)
                    self._training_progress.resume()

                self._do_sampling(timestep=self._total_timestep) # steps the env!!
                gt.stamp('sample')

                if self.ready_to_train:
                    self._do_training_repeats(timestep=self._total_timestep)
                gt.stamp('train')

                self._timestep_after_hook()
                gt.stamp('timestep_after_hook')

            training_paths = self.sampler.get_last_n_paths(
                math.ceil(self._epoch_length / self.sampler._max_path_length))
            gt.stamp('training_paths')
            evaluation_paths = self._evaluation_paths(
                policy, evaluation_environment)
            gt.stamp('evaluation_paths')

            training_metrics = self._evaluate_rollouts(
                training_paths, training_environment)
            gt.stamp('training_metrics')
            if evaluation_paths:
                evaluation_metrics = self._evaluate_rollouts(
                    evaluation_paths, evaluation_environment)
                gt.stamp('evaluation_metrics')
            else:
                evaluation_metrics = {}

            self._epoch_after_hook(training_paths)
            gt.stamp('epoch_after_hook')

            sampler_diagnostics = self.sampler.get_diagnostics()

            diagnostics = self.get_diagnostics(
                iteration=self._total_timestep,
                batch=self._evaluation_batch(),
                training_paths=training_paths,
                evaluation_paths=evaluation_paths)

            time_diagnostics = gt.get_times().stamps.itrs

            diagnostics.update(OrderedDict((
                *(
                    (f'evaluation/{key}', evaluation_metrics[key])
                    for key in sorted(evaluation_metrics.keys())
                ),
                *(
                    (f'training/{key}', training_metrics[key])
                    for key in sorted(training_metrics.keys())
                ),
                *(
                    (f'times/{key}', time_diagnostics[key][-1])
                    for key in sorted(time_diagnostics.keys())
                ),
                *(
                    (f'sampler/{key}', sampler_diagnostics[key])
                    for key in sorted(sampler_diagnostics.keys())
                ),
                *(
                    (f'model/{key}', model_metrics[key])
                    for key in sorted(model_metrics.keys())
                ),
                ('epoch', self._epoch),
                ('timestep', self._timestep),
                ('timesteps_total', self._total_timestep),
                ('train-steps', self._num_train_steps),
            )))

            if self._eval_render_mode is not None and hasattr(
                    evaluation_environment, 'render_rollouts'):
                training_environment.render_rollouts(evaluation_paths)

            yield diagnostics

        self.sampler.terminate()

        self._training_after_hook()

        self._training_progress.close()

        yield {'done': True, **diagnostics}

    def train(self, *args, **kwargs):
        return self._train(*args, **kwargs)

    def _log_policy(self):
        save_path = os.path.join(self._log_dir, 'models')
        filesystem.mkdir(save_path)
        weights = self._policy.get_weights()
        data = {'policy_weights': weights}
        full_path = os.path.join(save_path, 'policy_{}.pkl'.format(self._total_timestep))
        print('Saving policy to: {}'.format(full_path))
        pickle.dump(data, open(full_path, 'wb'))

    def _log_model(self):
        save_path = os.path.join(self._log_dir, 'models')
        filesystem.mkdir(save_path)
        print('Saving model to: {}'.format(save_path))
        self._model.save(save_path, self._total_timestep)

    def _set_rollout_length(self):
        min_epoch, max_epoch, min_length, max_length = self._rollout_schedule
        if self._epoch <= min_epoch:
            y = min_length
        else:
            dx = (self._epoch - min_epoch) / (max_epoch - min_epoch)
            dx = min(dx, 1)
            y = dx * (max_length - min_length) + min_length

        self._rollout_length = int(y)
        print('[ Model Length ] Epoch: {} (min: {}, max: {}) | Length: {} (min: {} , max: {})'.format(
            self._epoch, min_epoch, max_epoch, self._rollout_length, min_length, max_length
        ))

    def _set_real_ratio(self):
        min_epoch, max_epoch, min_length, max_length = self._ratio_schedule
        if self._epoch <= min_epoch:
            y = min_length
        else:
            dx = (self._epoch - min_epoch) / (max_epoch - min_epoch)
            dx = min(dx, 1)
            y = dx * (max_length - min_length) + min_length

        self._real_ratio = y
        print('[ Model Length ] Epoch: {} (min: {}, max: {}) | Ratio: {} (min: {} , max: {})'.format(
            self._epoch, min_epoch, max_epoch, self._real_ratio, min_length, max_length
        ))

    def _reallocate_model_pool(self):
        obs_space = self._pool._observation_space
        act_space = self._pool._action_space

        rollouts_per_epoch = self._rollout_batch_size * self._epoch_length / self._model_train_freq
        #rollouts_per_epoch = self._epoch_length / self._model_train_freq
        model_steps_per_epoch = int(self._rollout_length * rollouts_per_epoch)
        new_pool_size = self._model_retain_epochs * model_steps_per_epoch

        if not hasattr(self, '_model_pool'):
            print('[ MBPO ] Initializing new model pool with size {:.2e}'.format(
                new_pool_size
            ))
            self._model_pool = SimpleReplayPool(obs_space, act_space, new_pool_size)
        
        elif self._model_pool._max_size != new_pool_size:
            print('[ MBPO ] Updating model pool | {:.2e} --> {:.2e}'.format(
                self._model_pool._max_size, new_pool_size
            ))
            samples = self._model_pool.return_all_samples()
            new_pool = SimpleReplayPool(obs_space, act_space, new_pool_size)
            new_pool.add_samples(samples)
            assert self._model_pool.size == new_pool.size
            self._model_pool = new_pool

    def _train_model(self, **kwargs):
        env_samples = self._pool.return_all_samples()
        train_inputs, train_outputs = format_samples_for_training(env_samples)
        model_metrics = self._model.train(train_inputs, train_outputs, **kwargs)
        return model_metrics

    # TODO: refactor, extract functions
    def _rollout_model(self, gamma=0.9, lambda_=1.0, mopac=False, valuefunc=False, deterministic_obs=False, deterministic_rewards=False):
        print('[ Model Rollout ] Starting | Epoch: {} | Rollout length: {} | Batch size: {}'.format(
            self._epoch, self._rollout_length, self._rollout_batch_size
        ))
        batch = self.sampler.random_batch(self._rollout_batch_size)
        obs = batch['observations']
        steps_added = []

        if mopac:
            # repeat initial states for mppi
            obs = np.repeat(obs, self.repeats, axis=0)

            x_acts = np.zeros((self._rollout_batch_size*self.repeats, self._rollout_length, *self._action_shape))
            x_obs = np.zeros((self._rollout_batch_size*self.repeats, self._rollout_length, *self._observation_shape))
            x_total_reward = np.zeros((self._rollout_batch_size*self.repeats, self._rollout_length, 1))

            # fix model inds across rollouts
            # TODO: and initial states?
            #model_inds = self._model.random_inds(self._rollout_batch_size).repeat(self.repeats)
            model_inds = self._model.random_inds(self._rollout_batch_size*self.repeats)

        # rollouts
        # in mopac last step is replaced by value func
        horiz = self._rollout_length-1 if valuefunc and mopac else self._rollout_length
        for t in range(horiz):
            if mopac:
                # first action from control sequence
                #act = self.U[:,t]
                act = self._policy.actions_np(obs)

                # add noise and clip
                act += self.noise[:,t]
                act = np.clip(act, -self.uclip, self.uclip)
            else:
                act = self._policy.actions_np(obs)

                # new random model inds on each step
                model_inds = self._model.random_inds(self._rollout_batch_size)
            
            next_obs, rew, term, info = self.fake_env.step(obs, act, model_inds,
                                                            deterministic_obs=deterministic_obs,
                                                            deterministic_rewards=deterministic_rewards)
            steps_added.append(len(obs))

            if mopac:
                # store reward (incl gamma decay) and observation
                x_total_reward[:,t] = (gamma**t) * rew

                x_obs[:,t] = obs
                x_acts[:,t] = act
            else:
                samples = {'observations': obs, 'actions': act, 'next_observations': next_obs, 'rewards': rew, 'terminals': term, 'cumrewards': rew}
                self._model_pool.add_samples(samples)

            nonterm_mask = ~term.squeeze(-1)
            if nonterm_mask.sum() == 0:
                print('[ Model Rollout ] Breaking early: {} | {} / {}'.format(t, nonterm_mask.sum(), nonterm_mask.shape))
                break

            obs = next_obs if mopac else next_obs[nonterm_mask]  # making changes the shape of the array!

        if mopac:
            # VF on final state, replaces terminal reward
            if valuefunc:
                # previous next obs becomes last obs for storage
                x_obs[:,-1] = next_obs
                # predict terminal reward (normalized dsr)
                x_total_reward[:,-1] = self._V_targets.predict([x_obs[:,-1]])

                #x_total_reward[:,-1] = self._Vs.predict([x_obs[:,-1]])
                #next_Qs_values = tuple(Q.predict([obs, next_actions])
                #    for Q in self._Q_targets)
                #min_next_Q = tf.reduce_min(next_Qs_values, axis=0)

            x_opt_acts = np.zeros((self._rollout_batch_size, self._rollout_length, self._action_shape[0]))
            x_opt_obs = np.zeros((self._rollout_batch_size, *self._observation_shape))

            # mppi optimization
            for l in range(0, self._rollout_batch_size*self.repeats, self.repeats):
                # selectors
                i = int(l/self.repeats)
                r = range(l, l+self.repeats)

                # cum reward of rollout
                s = np.sum(x_total_reward[r], axis=1)

                # normalize cum reward
                alpha = np.exp(1/lambda_ * (s - np.max(s)))
                omega = alpha / (np.sum(alpha) + 1e-6)

                # compute control offset (most important part in mppi)
                u_delta = np.sum((omega.squeeze() * self.noise[r].T).T, axis=0)

                # tweak control (duplicated across range)
                #self.U[r] += 1 * u_delta
                #self.U[r] = np.clip(self.U[r], -self.uclip, self.uclip)
                x_acts[r] += 1 * u_delta

                # store first initial observation (and action sequence) belonging to action sequence
                x_opt_obs[i] = x_obs[l][0]  # initial observation
                #x_opt_acts[i] = self.U[l][:self._rollout_length]  # truncate
                x_opt_acts[i] = x_acts[l][:self._rollout_length]  # truncate

                # shift all elements to the left along horizon (for next env step)
                #self.U[r] = np.roll(self.U[r], -1, axis=1)

            # rollout trajectories using mppi control action sequences to generate samples
            # fix model inds
            model_inds = self._model.random_inds(self._rollout_batch_size)

            samples = []
            obs = x_opt_obs  # inital obs from first rollout
            for t in range(self._rollout_length):
                act = x_opt_acts[:,t]
                next_obs, rew, term, info = self.fake_env.step(obs, act, model_inds,
                                                                deterministic_obs=deterministic_obs,
                                                                deterministic_rewards=deterministic_rewards)

                # gamma decay on reward, update cum reward
                rew *= (gamma**t)

                # store sample
                samples += [{'observations': obs, 'actions': act, 'next_observations': next_obs, 'rewards': rew, 'terminals': term}]

                obs = next_obs

            # cum rewards: potential reward, decreases with every step in trajectory
            # for last step the cum reward becomes the reward of just that step
            # ex.: rew=(1,2,3,4,5) -> cumrewards=(15,14,12,9,5)
            rews = np.array([s['rewards'] for s in samples])
            cumrewards =  np.flip(np.cumsum(np.flip(rews), axis=0))
            # normalize
            cumrewards /= self._rollout_length

            # add samples to pool, together with cum reward
            for s, cw in zip(samples, cumrewards):
                s.update({'cumrewards': cw})

                # add to pool
                self._model_pool.add_samples(s)

        mean_rollout_length = sum(steps_added) / self._rollout_batch_size
        rollout_stats = {'mean_rollout_length': mean_rollout_length}
        print('[ Model Rollout ] Added: {:.1e} | Model pool: {:.1e} (max {:.1e}) | Length: {} | Train rep: {}'.format(
            sum(steps_added), self._model_pool.size, self._model_pool._max_size, mean_rollout_length, self._n_train_repeat
        ))

        return rollout_stats

    def _visualize_model(self, env, timestep):
        ## save env state
        state = env.unwrapped.state_vector()
        qpos_dim = len(env.unwrapped.sim.data.qpos)
        qpos = state[:qpos_dim]
        qvel = state[qpos_dim:]

        print('[ Visualization ] Starting | Epoch {} | Log dir: {}\n'.format(self._epoch, self._log_dir))
        visualize_policy(env, self.fake_env, self._policy, self._writer, timestep)
        print('[ Visualization ] Done')
        ## set env state
        env.unwrapped.set_state(qpos, qvel)

    def _training_batch(self, batch_size=None):
        batch_size = batch_size or self.sampler._batch_size
        env_batch_size = int(batch_size*self._real_ratio)
        model_batch_size = batch_size - env_batch_size

        ## can sample from the env pool even if env_batch_size == 0
        env_batch = self._pool.random_batch(env_batch_size)

        if model_batch_size > 0:
            model_batch = self._model_pool.random_batch(model_batch_size)

            keys = env_batch.keys()
            batch = {k: np.concatenate((env_batch[k], model_batch[k]), axis=0) for k in keys}
        else:
            ## if real_ratio == 1.0, no model pool was ever allocated,
            ## so skip the model pool sampling
            batch = env_batch
        return batch

    def _init_global_step(self):
        self.global_step = training_util.get_or_create_global_step()
        self._training_ops.update({
            'increment_global_step': training_util._increment_global_step(1)
        })

    def _init_placeholders(self):
        """Create input placeholders for the SAC algorithm.

        Creates `tf.placeholder`s for:
            - observation
            - next observation
            - action
            - reward
            - terminals
        """
        self._iteration_ph = tf.placeholder(
            tf.int64, shape=None, name='iteration')

        self._observations_ph = tf.placeholder(
            tf.float32,
            shape=(None, *self._observation_shape),
            name='observation',
        )

        self._next_observations_ph = tf.placeholder(
            tf.float32,
            shape=(None, *self._observation_shape),
            name='next_observation',
        )

        self._actions_ph = tf.placeholder(
            tf.float32,
            shape=(None, *self._action_shape),
            name='actions',
        )

        self._rewards_ph = tf.placeholder(
            tf.float32,
            shape=(None, 1),
            name='rewards',
        )

        self._terminals_ph = tf.placeholder(
            tf.float32,
            shape=(None, 1),
            name='terminals',
        )

        self._cumrewards_ph = tf.placeholder(
            tf.float32,
            shape=(None, 1),
            name='cumrewards',
        )

        if self._store_extra_policy_info:
            self._log_pis_ph = tf.placeholder(
                tf.float32,
                shape=(None, 1),
                name='log_pis',
            )
            self._raw_actions_ph = tf.placeholder(
                tf.float32,
                shape=(None, *self._action_shape),
                name='raw_actions',
            )

    def _get_Q_target(self):
        next_actions = self._policy.actions([self._next_observations_ph])
        next_log_pis = self._policy.log_pis(
            [self._next_observations_ph], next_actions)

        next_Qs_values = tuple(
            Q([self._next_observations_ph, next_actions])
            for Q in self._Q_targets)

        min_next_Q = tf.reduce_min(next_Qs_values, axis=0)
        next_value = min_next_Q - self._alpha * next_log_pis

        Q_target = td_target(
            reward=self._reward_scale * self._rewards_ph,
            discount=self._discount,
            next_value=(1 - self._terminals_ph) * next_value)

        return Q_target

    def _get_V_target(self):
        V_target = self._reward_scale * self._cumrewards_ph
        return V_target

    def _init_critic_update(self):
        """Create minimization operation for critic Q-function.

        Creates a `tf.optimizer.minimize` operation for updating
        critic Q-function with gradient descent, and appends it to
        `self._training_ops` attribute.
        """
        Q_target = tf.stop_gradient(self._get_Q_target())

        assert Q_target.shape.as_list() == [None, 1]

        Q_values = self._Q_values = tuple(
            Q([self._observations_ph, self._actions_ph])
            for Q in self._Qs)

        Q_losses = self._Q_losses = tuple(
            tf.losses.mean_squared_error(
                labels=Q_target, predictions=Q_value, weights=0.5)
            for Q_value in Q_values)

        self._Q_optimizers = tuple(
            tf.train.AdamOptimizer(
                learning_rate=self._Q_lr,
                name='{}_{}_optimizer'.format(Q._name, i)
            ) for i, Q in enumerate(self._Qs))
        Q_training_ops = tuple(
            tf.contrib.layers.optimize_loss(
                Q_loss,
                self.global_step,
                learning_rate=self._Q_lr,
                optimizer=Q_optimizer,
                variables=Q.trainable_variables,
                increment_global_step=False,
                summaries=((
                    "loss", "gradients", "gradient_norm", "global_gradient_norm"
                ) if self._tf_summaries else ()))
            for i, (Q, Q_loss, Q_optimizer)
            in enumerate(zip(self._Qs, Q_losses, self._Q_optimizers)))

        self._training_ops.update({'Q': tf.group(Q_training_ops)})

    def _init_value_update(self):
        """Create minimization operation for critic V-function.

        Creates a `tf.optimizer.minimize` operation for updating
        critic V-function with gradient descent, and appends it to
        `self._training_ops` attribute.
        """
        V_target = tf.stop_gradient(self._get_V_target())

        assert V_target.shape.as_list() == [None, 1]

        V_values = self._V_values = self._Vs([self._observations_ph])

        V_losses = self._V_losses = tf.losses.mean_squared_error(
                labels=V_target, predictions=V_values, weights=0.5)

        self._V_optimizers = tf.train.AdamOptimizer(
                learning_rate=self._V_lr,
                name='{}_optimizer'.format(self._Vs._name)
            )
        V_training_ops = tf.contrib.layers.optimize_loss(
                V_losses,
                self.global_step,
                learning_rate=self._V_lr,
                optimizer=self._V_optimizers,
                variables=self._Vs.trainable_variables,
                increment_global_step=False,
                summaries=((
                    "loss", "gradients", "gradient_norm", "global_gradient_norm"
                ) if self._tf_summaries else ()))

        self._training_ops.update({'V': tf.group(V_training_ops)})

    def _init_actor_update(self):
        """Create minimization operations for policy and entropy.

        Creates a `tf.optimizer.minimize` operations for updating
        policy and entropy with gradient descent, and adds them to
        `self._training_ops` attribute.
        """

        actions = self._policy.actions([self._observations_ph])
        log_pis = self._policy.log_pis([self._observations_ph], actions)

        assert log_pis.shape.as_list() == [None, 1]

        log_alpha = self._log_alpha = tf.get_variable(
            'log_alpha',
            dtype=tf.float32,
            initializer=0.0)
        alpha = tf.exp(log_alpha)

        if isinstance(self._target_entropy, Number):
            alpha_loss = -tf.reduce_mean(
                log_alpha * tf.stop_gradient(log_pis + self._target_entropy))

            self._alpha_optimizer = tf.train.AdamOptimizer(
                self._policy_lr, name='alpha_optimizer')
            self._alpha_train_op = self._alpha_optimizer.minimize(
                loss=alpha_loss, var_list=[log_alpha])

            self._training_ops.update({
                'temperature_alpha': self._alpha_train_op
            })

        self._alpha = alpha

        if self._action_prior == 'normal':
            policy_prior = tf.contrib.distributions.MultivariateNormalDiag(
                loc=tf.zeros(self._action_shape),
                scale_diag=tf.ones(self._action_shape))
            policy_prior_log_probs = policy_prior.log_prob(actions)
        elif self._action_prior == 'uniform':
            policy_prior_log_probs = 0.0

        Q_log_targets = tuple(
            Q([self._observations_ph, actions])
            for Q in self._Qs)
        min_Q_log_target = tf.reduce_min(Q_log_targets, axis=0)

        if self._reparameterize:
            policy_kl_losses = (
                alpha * log_pis
                - min_Q_log_target
                - policy_prior_log_probs)
        else:
            raise NotImplementedError

        assert policy_kl_losses.shape.as_list() == [None, 1]

        policy_loss = tf.reduce_mean(policy_kl_losses)

        self._policy_optimizer = tf.train.AdamOptimizer(
            learning_rate=self._policy_lr,
            name="policy_optimizer")
        policy_train_op = tf.contrib.layers.optimize_loss(
            policy_loss,
            self.global_step,
            learning_rate=self._policy_lr,
            optimizer=self._policy_optimizer,
            variables=self._policy.trainable_variables,
            increment_global_step=False,
            summaries=(
                "loss", "gradients", "gradient_norm", "global_gradient_norm"
            ) if self._tf_summaries else ())

        self._training_ops.update({'policy_train_op': policy_train_op})

    def _init_training(self):
        self._update_target(tau=1.0)

    def _init_mppi(self, hl=0.4, horiz=15, noise_mu=0., noise_sigma=0.5, uclip=1.4, lambda_=1.0, repeats=50):
        action_len = self._action_shape[0]
        obs_len = self._observation_shape[0]
        self.repeats = repeats
        self.U = np.random.uniform(low=-hl, high=hl, size=(self._rollout_batch_size*repeats, horiz, action_len))
        self.noise = np.random.normal(loc=noise_mu, scale=noise_sigma, size=(self._rollout_batch_size*repeats, horiz, action_len))
        self.uclip = uclip
        #self.action_q = Queue()

    def _reset_mppi(self):
        self._init_mppi(horiz=self._rollout_length)
        
        # sample batch for init U with policy
        #batch = self.sampler.random_batch(self._rollout_batch_size)
        #obs = batch['observations'].repeat(self.repeats, axis=0)

        ## init every timestep with same action
        #for t in range(self._rollout_length):
        #    self.U[:,t] = self._policy.actions_np(obs)

    def _update_target(self, tau=None):
        tau = tau or self._tau

        for Q, Q_target in zip(self._Qs, self._Q_targets):
            source_params = Q.get_weights()
            target_params = Q_target.get_weights()
            Q_target.set_weights([
                tau * source + (1.0 - tau) * target
                for source, target in zip(source_params, target_params)
            ])

        source_params = self._Vs.get_weights()
        target_params = self._V_targets.get_weights()
        self._V_targets.set_weights([
            tau * source + (1.0 - tau) * target
            for source, target in zip(source_params, target_params)
        ])

    def _do_training(self, iteration, batch):
        """Runs the operations for updating training and target ops."""

        self._training_progress.update()
        self._training_progress.set_description()

        feed_dict = self._get_feed_dict(iteration, batch)

        self._session.run(self._training_ops, feed_dict)

        if iteration % self._target_update_interval == 0:
            # Run target ops here.
            self._update_target()

    def _get_feed_dict(self, iteration, batch):
        """Construct TensorFlow feed_dict from sample batch."""

        feed_dict = {
            self._observations_ph: batch['observations'],
            self._actions_ph: batch['actions'],
            self._next_observations_ph: batch['next_observations'],
            self._rewards_ph: batch['rewards'],
            self._terminals_ph: batch['terminals']
        }

        feed_dict[self._cumrewards_ph] = batch['cumrewards']

        if self._store_extra_policy_info:
            feed_dict[self._log_pis_ph] = batch['log_pis']
            feed_dict[self._raw_actions_ph] = batch['raw_actions']

        if iteration is not None:
            feed_dict[self._iteration_ph] = iteration

        return feed_dict

    def get_diagnostics(self,
                        iteration,
                        batch,
                        training_paths,
                        evaluation_paths):
        """Return diagnostic information as ordered dictionary.

        Records mean and standard deviation of Q-function and state
        value function, and TD-loss (mean squared Bellman error)
        for the sample batch.

        Also calls the `draw` method of the plotter, if plotter defined.
        """

        feed_dict = self._get_feed_dict(iteration, batch)

        (Q_values, Q_losses, alpha, global_step) = self._session.run(
            (self._Q_values,
             self._Q_losses,
             self._alpha,
             self.global_step),
            feed_dict)

        (V_values, V_losses, alpha, global_step) = self._session.run(
            (self._V_values,
             self._V_losses,
             self._alpha,
             self.global_step),
            feed_dict)

        diagnostics = OrderedDict({
            'Q-avg': np.mean(Q_values),
            'Q-std': np.std(Q_values),
            'Q_loss': np.mean(Q_losses),
            'V-avg': np.mean(V_values),
            'V-std': np.std(V_values),
            'V_loss': np.mean(V_losses),
            'alpha': alpha,
        })

        policy_diagnostics = self._policy.get_diagnostics(
            batch['observations'])
        diagnostics.update({
            f'policy/{key}': value
            for key, value in policy_diagnostics.items()
        })

        if self._plotter:
            self._plotter.draw()

        return diagnostics

    @property
    def tf_saveables(self):
        saveables = {
            '_policy_optimizer': self._policy_optimizer,
            **{
                f'Q_optimizer_{i}': optimizer
                for i, optimizer in enumerate(self._Q_optimizers)
            },
            '_log_alpha': self._log_alpha,
        }

        if hasattr(self, '_alpha_optimizer'):
            saveables['_alpha_optimizer'] = self._alpha_optimizer

        return saveables
