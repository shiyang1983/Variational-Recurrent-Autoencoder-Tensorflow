# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Sequence-to-sequence model with an attention mechanism."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import random

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf

import data_utils
import seq2seq
import pdb
from bnlstm import BNLSTMCell
from tf_beam_decoder import BeamDecoder
from tensorflow.python.ops import variable_scope

class Seq2SeqModel(object):
  """Sequence-to-sequence model with attention and for multiple buckets.

  This class implements a multi-layer recurrent neural network as encoder,
  and an attention-based decoder. This is the same as the model described in
  this paper: http://arxiv.org/abs/1412.7449 - please look there for details,
  or into the seq2seq library for complete model implementation.
  This class also allows to use GRU cells in addition to LSTM cells, and
  sampled softmax to handle large output vocabulary size. A single-layer
  version of this model, but with bi-directional encoder, was presented in
    http://arxiv.org/abs/1409.0473
  and sampled softmax is described in Section 3 of the following paper.
    http://arxiv.org/abs/1412.2007
  """

  def __init__(self,
               source_vocab_size,
               target_vocab_size,
               buckets,
               size,
               num_layers,
               latent_dim,
               max_gradient_norm,
               batch_size,
               learning_rate,
               latent_splits=8,
               Lambda=2,
               word_dropout_keep_prob=1.0,
               beam_size=2,
               annealing=False,
               lower_bound_KL=True,
               kl_rate_rise_time=None,
               kl_rate_rise_factor=None,
               use_lstm=False,
               mean_logvar_split=False,
               load_embeddings=False,
               Lambda_annealing=False,
               num_samples=512,
               optimizer=None,
               activation=tf.nn.relu,
               dnn_in_between=False,
               probabilistic=False,
               batch_norm=False,
               forward_only=False,
               feed_previous=True,
               bidirectional=False,
               weight_initializer=None,
               bias_initializer=None,
               iaf=False,
               adamax=False,
               dtype=tf.float32):
    """Create the model.

    Args:
      source_vocab_size: size of the source vocabulary.
      target_vocab_size: size of the target vocabulary.
      buckets: a list of pairs (I, O), where I specifies maximum input length
        that will be processed in that bucket, and O specifies maximum output
        length. Training instances that have inputs longer than I or outputs
        longer than O will be pushed to the next bucket and padded accordingly.
        We assume that the list is sorted, e.g., [(2, 4), (8, 16)].
      size: number of units in each layer of the model.
      num_layers: number of layers in the model.
      max_gradient_norm: gradients will be clipped to maximally this norm.
      batch_size: the size of the batches used during training;
        the model construction is independent of batch_size, so it can be
        changed after initialization if this is convenient, e.g., for decoding.
      learning_rate: learning rate to start with.
      use_lstm: if true, we use LSTM cells instead of GRU cells.
      num_samples: number of samples for sampled softmax.
      forward_only: if set, we do not construct the backward pass in the model.
      dtype: the data type to use to store internal variables.
    """
    self.source_vocab_size = source_vocab_size
    self.target_vocab_size = target_vocab_size
    self.probabilistic = probabilistic
    self.latent_dim = latent_dim
    self.buckets = buckets
    self.batch_size = batch_size
    self.word_dropout_keep_prob = word_dropout_keep_prob
    self.Lambda = Lambda
    feed_previous = feed_previous or forward_only
    if Lambda_annealing:
      self.Lambda = tf.Variable(
          Lambda, trainable=False, dtype=dtype)
      self.Lambda_divide_by_two_op = self.Lambda.assign(
          self.Lambda / 2)
    self.learning_rate = tf.Variable(
        float(learning_rate), trainable=False, dtype=dtype)

    self.enc_embedding = tf.get_variable("enc_embedding", [source_vocab_size, size], dtype=dtype, initializer=weight_initializer())
    self.enc_embedding_placeholder = tf.placeholder(tf.float32, [source_vocab_size, size])
    self.enc_embedding_init_op = self.enc_embedding.assign(self.enc_embedding_placeholder)

    self.dec_embedding = tf.get_variable("dec_embedding", [target_vocab_size, size], dtype=dtype, initializer=weight_initializer())
    self.dec_embedding_placeholder = tf.placeholder(tf.float32, [target_vocab_size, size])
    self.dec_embedding_init_op = self.dec_embedding.assign(self.dec_embedding_placeholder)

    self.replace_input = None
    replace_input = None
    if word_dropout_keep_prob < 1:
      self.replace_input = tf.placeholder(tf.int32, shape=[None], name="replace_input")
      replace_input = tf.nn.embedding_lookup(self.dec_embedding, self.replace_input)

    self.kl_rate = tf.Variable(
        0, trainable=False, dtype=dtype)
    self.kl_rate_rise_op = self.kl_rate.assign(
        self.kl_rate + kl_rate_rise_factor)


    self.global_step = tf.Variable(0, trainable=False)

    # If we use sampled softmax, we need an output projection.
    output_projection = None
    softmax_loss_function = None
    # Sampled softmax only makes sense if we sample less than vocabulary size.
    if num_samples > 0 and num_samples < self.target_vocab_size:
      w_t = tf.get_variable("proj_w", [self.target_vocab_size, size], dtype=dtype, initializer=weight_initializer())
      w = tf.transpose(w_t)
      b = tf.get_variable("proj_b", [self.target_vocab_size], dtype=dtype, initializer=bias_initializer)
      output_projection = (w, b)

      def sampled_loss(inputs, labels):
        labels = tf.reshape(labels, [-1, 1])
        # We need to compute the sampled_softmax_loss using 32bit floats to
        # avoid numerical instabilities.
        local_w_t = tf.cast(w_t, tf.float32)
        local_b = tf.cast(b, tf.float32)
        local_inputs = tf.cast(inputs, tf.float32)
        return tf.cast(
            tf.nn.sampled_softmax_loss(local_w_t, local_b, local_inputs, labels,
                                       num_samples, self.target_vocab_size),
            dtype)
      softmax_loss_function = sampled_loss
    # Create the internal multi-layer cell for our RNN.
    single_cell = tf.nn.rnn_cell.GRUCell(size)
    if use_lstm:
      if batch_norm:
        tf_forward_only = tf.Variable(forward_only)
        single_cell = BNLSTMCell(size, tf_forward_only)
      else:
        single_cell = tf.nn.rnn_cell.BasicLSTMCell(size)
    cell = single_cell
    if num_layers > 1:
      cell = tf.nn.rnn_cell.MultiRNNCell([single_cell] * num_layers)

    def encoder_f(encoder_inputs):
      return seq2seq.embedding_encoder(
          encoder_inputs,
          cell,
          embedding=self.enc_embedding,
          num_symbols=source_vocab_size,
          embedding_size=size,
          bidirectional=bidirectional,
          weight_initializer=weight_initializer,
          dtype=dtype)

    def decoder_f(encoder_state, decoder_inputs):
      return seq2seq.embedding_rnn_decoder(
          decoder_inputs,
          encoder_state,
          cell,
          embedding=self.dec_embedding,
          word_dropout_keep_prob=word_dropout_keep_prob,
          replace_input=replace_input,
          num_symbols=target_vocab_size,
          embedding_size=size,
          output_projection=output_projection,
          feed_previous=feed_previous,
          weight_initializer=weight_initializer)

    def beam_decoder_f(encoder_state, decoder_inputs):
      beam_decoder = BeamDecoder(target_vocab_size, beam_size=beam_size, max_len=len(decoder_inputs))
      with variable_scope.variable_scope("beam_decoder_f") as scope:
        decoder_inputs = [tf.nn.embedding_lookup(self.dec_embedding, i) for i in decoder_inputs]
        _, final_state = seq2seq.rnn_decoder(
            [beam_decoder.wrap_input(decoder_input) for decoder_input in decoder_inputs],
            beam_decoder.wrap_state(encoder_state), beam_decoder.wrap_cell(cell),
            loop_function = lambda prev_symbol, i: tf.nn.embedding_lookup(self.dec_embedding, prev_symbol))
        best_dense = beam_decoder.unwrap_output_dense(final_state) # Dense tensor output, right-aligned
        best_sparse = beam_decoder.unwrap_output_sparse(final_state) # Output, this time as a sparse tensor
      return best_dense, final_state

    def latent_dec_f(latent_vector):
      return seq2seq.latent_to_decoder(latent_vector,
           embedding_size=size,
           latent_dim=latent_dim,
           num_layers=num_layers,
           activation=activation,
           use_lstm=use_lstm,
           dtype=dtype)

    def lower_bounded_kl_f(mean, logvar):
      return seq2seq.lower_bounded_KL_divergence(
        mean, logvar, latent_splits, self.Lambda)

    def iaf_sample_f(means, logvars):
      return seq2seq.iaf_sample(
        means, logvars, latent_dim, self.Lambda, dtype=dtype)

    def enc_latent_f(encoder_state):
      return seq2seq.encoder_to_latent(encoder_state,
                     embedding_size=size,
                     latent_dim=latent_dim,
                     num_layers=num_layers,
                     activation=activation,
                     use_lstm=use_lstm,
                     mean_logvar_split=mean_logvar_split,
                     enc_state_bidirectional=bidirectional,
                     dtype=dtype)

    def sample_f(mean, logvar):
      return seq2seq.sample(
        mean, logvar,
        batch_size=batch_size,
        latent_dim=latent_dim,
        dtype=dtype)

    # The seq2seq function: we use embedding for the input and attention.
    def seq2seq_f(encoder_inputs, decoder_inputs, do_decode):
      return tf.nn.seq2seq.embedding_attention_seq2seq(
          encoder_inputs,
          decoder_inputs,
          cell,
          num_encoder_symbols=source_vocab_size,
          num_decoder_symbols=target_vocab_size,
          embedding_size=size,
          output_projection=output_projection,
          feed_previous=do_decode,
          dtype=dtype)


    # Feeds for inputs.
    self.encoder_inputs = []
    self.decoder_inputs = []
    self.target_weights = []
    for i in xrange(buckets[-1][0]):  # Last bucket is the biggest one.
      self.encoder_inputs.append(tf.placeholder(tf.int32, shape=[None],
                                                name="encoder{0}".format(i)))
    for i in xrange(buckets[-1][1] + 1):
      self.decoder_inputs.append(tf.placeholder(tf.int32, shape=[None],
                                                name="decoder{0}".format(i)))
      self.target_weights.append(tf.placeholder(dtype, shape=[None],
                                                name="weight{0}".format(i)))

    # Our targets are decoder inputs shifted by one.
    targets = [self.decoder_inputs[i + 1]
               for i in xrange(len(self.decoder_inputs) - 1)]

    if iaf:
      sample_f = iaf_sample_f

    if annealing and not lower_bound_KL:
      kl_f = seq2seq.KL_divergence
    else:
      kl_f = lower_bounded_kl_f
    if beam_size > 1:
      decoder = beam_decoder_f
    else:
      decoder = decoder_f
    # Training outputs and losses.
    if dnn_in_between:
      self.means, self.logvars = seq2seq.variational_encoder_with_buckets(
          self.encoder_inputs, buckets, encoder_f, enc_latent_f,
          softmax_loss_function=softmax_loss_function)
      self.outputs, self.losses, self.KL_divergences = seq2seq.variational_decoder_with_buckets(
          self.means, self.logvars, self.decoder_inputs, targets,
          self.target_weights, buckets, decoder,
          latent_dec_f, kl_f, sample_f, iaf,
          softmax_loss_function=softmax_loss_function)
    else:
      self.outputs, self.losses = seq2seq.autoencoder_with_buckets(
          self.encoder_inputs, self.decoder_inputs, targets,
          self.target_weights, buckets, encoder_f, decoder,
          softmax_loss_function=softmax_loss_function)
    # If we use output projection, we need to project outputs for decoding.
    if output_projection is not None:
      for b in xrange(len(buckets)):
        self.outputs[b] = [
            tf.matmul(output, output_projection[0]) + output_projection[1]
            for output in self.outputs[b]
          ]
    # Gradients and SGD update operation for training the model.
    params = tf.trainable_variables()
    if not forward_only:
      ema = tf.train.ExponentialMovingAverage(decay=0.999)
      self.gradient_norms = []
      self.updates = []
      for b in xrange(len(buckets)):
        if probabilistic:
          if annealing:
            annealed_KL_divergence = self.kl_rate * self.KL_divergences[b]
            total_loss = self.losses[b] + annealed_KL_divergence
          else:
            print("kl_divergence taken into account")
            total_loss = self.losses[b] + self.KL_divergences[b]
        else:
            total_loss = self.losses[b]
        gradients = tf.gradients(total_loss, params)
        clipped_gradients, norm = tf.clip_by_global_norm(gradients,
                                                         max_gradient_norm)
        self.gradient_norms.append(norm)
        if adamax:
          with tf.name_scope(None):  # This is needed due to EMA implementation silliness.
            # keep track of moving average
            train_op = optimizer.apply_gradients(
                    zip(clipped_gradients, params), global_step=self.global_step)
            train_op = tf.group(*[train_op, ema.apply(params)])
            self.updates.append(train_op)
        else:
          self.updates.append(optimizer.apply_gradients(
              zip(clipped_gradients, params), global_step=self.global_step))

    if adamax:
      self.avg_dict = ema.variables_to_restore()
      self.saver = tf.train.Saver(self.avg_dict)
    else:
      self.saver = tf.train.Saver(tf.global_variables())



  
  def step(self, session, encoder_inputs, decoder_inputs, target_weights,
           bucket_id, forward_only):
    """Run a step of the model feeding the given inputs.

    Args:
      session: tensorflow session to use.
      encoder_inputs: list of numpy int vectors to feed as encoder inputs.
      decoder_inputs: list of numpy int vectors to feed as decoder inputs.
      target_weights: list of numpy float vectors to feed as target weights.
      bucket_id: which bucket of the model to use.
      forward_only: whether to do the backward step or only forward.

    Returns:
      A triple consisting of gradient norm (or None if we did not do backward),
      average perplexity, and the outputs.

    Raises:
      ValueError: if length of encoder_inputs, decoder_inputs, or
        target_weights disagrees with bucket size for the specified bucket_id.
    """
    # Check if the sizes match.
    encoder_size, decoder_size = self.buckets[bucket_id]
    if len(encoder_inputs) != encoder_size:
      raise ValueError("Encoder length must be equal to the one in bucket,"
                       " %d != %d." % (len(encoder_inputs), encoder_size))
    if len(decoder_inputs) != decoder_size:
      raise ValueError("Decoder length must be equal to the one in bucket,"
                       " %d != %d." % (len(decoder_inputs), decoder_size))
    if len(target_weights) != decoder_size:
      raise ValueError("Weights length must be equal to the one in bucket,"
                       " %d != %d." % (len(target_weights), decoder_size))

    # Input feed: encoder inputs, decoder inputs, target_weights, as provided.
    input_feed = {}
    for l in xrange(encoder_size):
      input_feed[self.encoder_inputs[l].name] = encoder_inputs[l]
    for l in xrange(decoder_size):
      input_feed[self.decoder_inputs[l].name] = decoder_inputs[l]
      input_feed[self.target_weights[l].name] = target_weights[l]
    if self.word_dropout_keep_prob < 1:
      input_feed[self.replace_input.name] = np.full((self.batch_size), data_utils.UNK_ID, dtype=np.int32)

    # Since our targets are decoder inputs shifted by one, we need one more.
    last_target = self.decoder_inputs[decoder_size].name
    input_feed[last_target] = np.zeros([self.batch_size], dtype=np.int32)
    if not self.probabilistic:
      input_feed[self.logvars[bucket_id]] = np.zeros([self.batch_size, self.latent_dim], dtype=np.int32)

    # Output feed: depends on whether we do a backward step or not.
    if not forward_only:
      output_feed = [self.updates[bucket_id],  # Update Op that does SGD.
                     self.gradient_norms[bucket_id],  # Gradient norm.
                     self.losses[bucket_id],
                     self.KL_divergences[bucket_id]]  # Loss for this batch.
    else:
      output_feed = [self.losses[bucket_id], self.KL_divergences[bucket_id]]  # Loss for this batch.
      for l in xrange(decoder_size):  # Output logits.
        output_feed.append(self.outputs[bucket_id][l])

    outputs = session.run(output_feed, input_feed)
    if not forward_only:
      return outputs[1], outputs[2], outputs[3], None  # Gradient norm, loss, KL divergence, no outputs.
    else:
        return None, outputs[0], outputs[1], outputs[2:]  # no gradient norm, loss, KL divergence, outputs.


  def encode_to_latent(self, session, encoder_inputs, bucket_id):

    # Check if the sizes match.
    encoder_size, _ = self.buckets[bucket_id]
    if len(encoder_inputs) != encoder_size:
      raise ValueError("Encoder length must be equal to the one in bucket,"
                       " %d != %d." % (len(encoder_inputs), encoder_size))

    input_feed = {}
    for l in xrange(encoder_size):
      input_feed[self.encoder_inputs[l].name] = encoder_inputs[l]



    output_feed = [self.means, self.logvars]
    means, logvars = session.run(output_feed, input_feed)

    return means, logvars


  def decode_from_latent(self, session, means, logvars, bucket_id, decoder_inputs, target_weights):

    _, decoder_size = self.buckets[bucket_id]
    # Input feed: means.
    input_feed = {self.means[bucket_id]: means}
    if not self.probabilistic:
      input_feed[self.logvars[bucket_id]] = np.zeros([self.batch_size, self.latent_dim], dtype=np.int32)
    else:
      input_feed[self.logvars[bucket_id]] = logvars

    for l in xrange(decoder_size):
      input_feed[self.decoder_inputs[l].name] = decoder_inputs[l]
      input_feed[self.target_weights[l].name] = target_weights[l]
    if self.word_dropout_keep_prob < 1:
      input_feed[self.replace_input.name] = np.full((self.batch_size), data_utils.UNK_ID, dtype=np.int32)

    last_target = self.decoder_inputs[decoder_size].name
    input_feed[last_target] = np.zeros([self.batch_size], dtype=np.int32)
    output_feed = []
    for l in xrange(decoder_size):  # Output logits.
      output_feed.append(self.outputs[bucket_id][l])

    outputs = session.run(output_feed, input_feed)

    return outputs

  def get_batch(self, data, bucket_id):
    """Get a random batch of data from the specified bucket, prepare for step.

    To feed data in step(..) it must be a list of batch-major vectors, while
    data here contains single length-major cases. So the main logic of this
    function is to re-index data cases to be in the proper format for feeding.

    Args:
      data: a tuple of size len(self.buckets) in which each element contains
        lists of pairs of input and output data that we use to create a batch.
      bucket_id: integer, which bucket to get the batch for.

    Returns:
      The triple (encoder_inputs, decoder_inputs, target_weights) for
      the constructed batch that has the proper format to call step(...) later.
    """
    encoder_size, decoder_size = self.buckets[bucket_id]
    encoder_inputs, decoder_inputs = [], []

    # Get a random batch of encoder and decoder inputs from data,
    # pad them if needed, reverse encoder inputs and add GO to decoder.
    for _ in xrange(self.batch_size):
      encoder_input, decoder_input = random.choice(data[bucket_id])

      # Encoder inputs are padded and then reversed.
      encoder_pad = [data_utils.PAD_ID] * (encoder_size - len(encoder_input))
      encoder_inputs.append(list(reversed(encoder_input + encoder_pad)))

      # Decoder inputs get an extra "GO" symbol, and are padded then.
      decoder_pad_size = decoder_size - len(decoder_input) - 1
      decoder_inputs.append([data_utils.GO_ID] + decoder_input +
                            [data_utils.PAD_ID] * decoder_pad_size)

    # Now we create batch-major vectors from the data selected above.
    batch_encoder_inputs, batch_decoder_inputs, batch_weights = [], [], []

    # Batch encoder inputs are just re-indexed encoder_inputs.
    for length_idx in xrange(encoder_size):
      batch_encoder_inputs.append(
          np.array([encoder_inputs[batch_idx][length_idx]
                    for batch_idx in xrange(self.batch_size)], dtype=np.int32))

    # Batch decoder inputs are re-indexed decoder_inputs, we create weights.
    for length_idx in xrange(decoder_size):
      batch_decoder_inputs.append(
          np.array([decoder_inputs[batch_idx][length_idx]
                    for batch_idx in xrange(self.batch_size)], dtype=np.int32))

      # Create target_weights to be 0 for targets that are padding.
      batch_weight = np.ones(self.batch_size, dtype=np.float32)
      for batch_idx in xrange(self.batch_size):
        # We set weight to 0 if the corresponding target is a PAD symbol.
        # The corresponding target is decoder_input shifted by 1 forward.
        if length_idx < decoder_size - 1:
          target = decoder_inputs[batch_idx][length_idx + 1]
        if length_idx == decoder_size - 1 or target == data_utils.PAD_ID:
          batch_weight[batch_idx] = 0.0
      batch_weights.append(batch_weight)
    return batch_encoder_inputs, batch_decoder_inputs, batch_weights
