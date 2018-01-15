import random

import gym
import numpy as np
import tensorflow as tf

from rltf.agents.agent  import OffPolicyAgent
from rltf.memory        import ReplayBuffer


class AgentDQN(OffPolicyAgent):

  def __init__(self,
               model_type,
               model_kwargs,
               opt_conf,
               exploration,
               update_target_freq=10000,
               memory_size=int(1e6),
               obs_hist_len=4,
               **agent_kwargs
              ):
    """
    Args:
      model_type: rltf.models.Model. TF implementation of a model network
      model_kwargs: dict. Model-specific keyword arguments to pass to the model
      opt_conf: rltf.optimizers.OptimizerConf. Config for the network optimizer
      exploration: rltf.schedules.Schedule. Epsilon value for e-greedy exploration
      update_target_freq: Period in number of steps at which to update the target net
      memory_size: int. Size of the replay buffer
      obs_hist_len: int. How many environment observations comprise a single state.
      agent_kwargs: Keyword arguments that will be passed to the Agent base class
    """

    super().__init__(**agent_kwargs)

    assert isinstance(self.env.observation_space, gym.spaces.Box)
    assert isinstance(self.env.action_space,      gym.spaces.Discrete)
    assert update_target_freq % self.train_freq == 0

    self.opt_conf = opt_conf
    self.exploration = exploration
    self.update_target_freq = update_target_freq

    # Get environment specs
    obs_shape = self.env.observation_space.shape
    if isinstance(obs_shape, int):
        obs_shape = [obs_shape]
    obs_shape = list(obs_shape)
    buf_obs_shape = obs_shape
    n_actions = self.env.action_space.n

    model_kwargs["obs_shape"] = obs_shape
    model_kwargs["n_actions"] = n_actions
    model_kwargs["opt_conf"]  = opt_conf

    self.model      = model_type(**model_kwargs)
    self.replay_buf = ReplayBuffer(memory_size, buf_obs_shape,
            np.float32, [], np.uint8, obs_hist_len)

    # Configure what information to log
    super()._build_log_info()

    # Custom TF Tensors and Ops
    self.learn_rate_ph  = None
    self.epsilon_ph     = None


  def _build(self):
    # Create Learning rate placeholders
    self.learn_rate_ph  = tf.placeholder(tf.float32, shape=(), name="learn_rate_ph")
    self.epsilon_ph     = tf.placeholder(tf.float32, shape=(), name="epsilon_ph")

    # Set the learn rate placeholders for the model
    self.opt_conf.lr_ph = self.learn_rate_ph

    # Add summaries
    tf.summary.scalar("learn_rate", self.learn_rate_ph)
    tf.summary.scalar("epsilon",    self.epsilon_ph)


  def _restore(self, graph):
    self.learn_rate_ph  = graph.get_tensor_by_name("learn_rate_ph:0")
    self.epsilon_ph     = graph.get_tensor_by_name("epsilon_ph:0")


  def _custom_log_info(self):
    log_info = [
      ( "train/learn_rate",  "f", self.opt_conf.lr_value ),
      ( "train/exploration", "f", self.exploration.value ),
    ]
    return log_info


  def _run_env(self):

    last_obs  = self.env.reset()

    for t in range (self.start_step, self.max_steps+1):
      # sess.run(t_inc_op)

      # Wait until net_thread is done
      self._wait_train_done()

      # Store the latest obesrvation in the buffer
      idx = self.replay_buf.store_frame(last_obs)

      # Get an action to run
      if self.learn_started:
        # Run epsilon greedy policy
        epsilon = self.exploration.value(t)

        if np.random.uniform(0,1) < epsilon:
          action = self.env.action_space.sample()
        else:
          # Run the network to select an action
          state   = self.replay_buf.encode_recent_obs()
          action  = self.model.control_action(self.sess, state)

      else:
        # Choose random action when model not initialized
        action = self.env.action_space.sample()

      # Signal to net_thread that action is chosen
      self._signal_act_chosen()

      # Run action
      # next_obs, reward, done, info = self.env.step(action)
      last_obs, reward, done, _ = self.env.step(action)

      # Store the effect of the action taken upon last_obs
      # self.replay_buf.store(obs, action, reward, done)
      self.replay_buf.store_effect(idx, action, reward, done)

      # Reset the environment if end of episode
      # if done: next_obs = self.env.reset()
      # obs = next_obs

      if done:
        last_obs = self.env.reset()

      self._log_progress(t)


  def _train_model(self):

    for t in range (self.start_step, self.max_steps+1):

      if (t >= self.start_train and t % self.train_freq == 0):

        self.learn_started = True

        # Sample the Replay Buffer
        batch = self.replay_buf.sample(self.batch_size)

        # Compose feed_dict
        feed_dict = {
          self.model.obs_t_ph:       batch["obs"],
          self.model.act_t_ph:       batch["act"],
          self.model.rew_t_ph:       batch["rew"],
          self.model.obs_tp1_ph:     batch["obs_tp1"],
          self.model.done_ph:        batch["done"],
          self.learn_rate_ph:        self.opt_conf.lr_value(t),
          self.epsilon_ph:           self.exploration.value(t),
          self.mean_ep_rew_ph:       self.mean_ep_rew,
          self.best_mean_ep_rew_ph:  self.best_mean_ep_rew,
        }

        self._wait_act_chosen()

        # Run a training step
        self.summary, _ = self.sess.run([self.summary_op, self.model.train_op], feed_dict=feed_dict)

        # Update target network
        if t % self.update_target_freq == 0:
          self.sess.run(self.model.update_target)

      else:
        self._wait_act_chosen()

      if t % self.save_freq == 0:
        self._save()

      self._signal_train_done()

  def reset(self):
    pass
