params = {
    'type': 'MBPO',
    'universe': 'gym',
    'domain': 'HalfCheetah',
    'task': 'v2',

    'log_dir': '/work/scratch/dn38jyty/ray_mbpo/',
    'exp_name': 'mopacfixedv2',

    'kwargs': {
        'epoch_length': 1000,
        'train_every_n_steps': 1,
        'n_train_repeat': 40,
        'eval_render_mode': None,
        'eval_n_episodes': 1,
        'eval_deterministic': True,

        'discount': 0.99,
        'tau': 5e-3,
        'reward_scale': 1.00,

        'mopac': True,
        'valuefunc': True,
        'deterministic_obs': False,
        'deterministic_rewards': True,

        'model_train_freq': 250,
        'model_retain_epochs': 1,
        'rollout_batch_size': 9999,
        'num_networks': 7,
        'num_elites': 5,
        'target_entropy': -3,
        'max_model_t': None,
        'rollout_schedule': [0, 80, 5, 15],
        'ratio_schedule': [0, 80, 0.95, 0.05],
    }
}
