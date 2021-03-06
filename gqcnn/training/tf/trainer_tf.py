# -*- coding: utf-8 -*-
"""
Copyright ©2017. The Regents of the University of California (Regents). All Rights Reserved.
Permission to use, copy, modify, and distribute this software and its documentation for educational,
research, and not-for-profit purposes, without fee and without a signed licensing agreement, is
hereby granted, provided that the above copyright notice, this paragraph and the following two
paragraphs appear in all copies, modifications, and distributions. Contact The Office of Technology
Licensing, UC Berkeley, 2150 Shattuck Avenue, Suite 510, Berkeley, CA 94720-1620, (510) 643-
7201, otl@berkeley.edu, http://ipira.berkeley.edu/industry-info for commercial licensing opportunities.

IN NO EVENT SHALL REGENTS BE LIABLE TO ANY PARTY FOR DIRECT, INDIRECT, SPECIAL,
INCIDENTAL, OR CONSEQUENTIAL DAMAGES, INCLUDING LOST PROFITS, ARISING OUT OF
THE USE OF THIS SOFTWARE AND ITS DOCUMENTATION, EVEN IF REGENTS HAS BEEN
ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

REGENTS SPECIFICALLY DISCLAIMS ANY WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
PURPOSE. THE SOFTWARE AND ACCOMPANYING DOCUMENTATION, IF ANY, PROVIDED
HEREUNDER IS PROVIDED "AS IS". REGENTS HAS NO OBLIGATION TO PROVIDE
MAINTENANCE, SUPPORT, UPDATES, ENHANCEMENTS, OR MODIFICATIONS.
"""
"""
Trains a GQCNN network using Tensorflow backend.
Author: Vishal Satish and Jeff Mahler
"""
import argparse
import collections
import copy
import cv2
import json
import logging
import matplotlib.pyplot as plt
import numpy as np
import cPickle as pkl
import os
import random
import scipy.misc as sm
import scipy.ndimage.filters as sf
import scipy.ndimage.morphology as snm
import scipy.stats as ss
import shutil
import signal
import skimage.draw as sd
import subprocess
import sys
import tensorflow as tf
import threading
import time
import yaml

from autolab_core import BinaryClassificationResult, RegressionResult, TensorDataset, YamlConfig
from autolab_core.constants import *
import autolab_core.utils as utils

from gqcnn.utils import ImageMode, TrainingMode, GripperMode, InputDepthMode, GeneralConstants
from gqcnn.utils import TrainStatsLogger
from gqcnn.utils import pose_dim, read_pose_data, weight_name_to_layer_name

class GQCNNTrainerTF(object):
    """ Trains GQCNN with Tensorflow backend """

    def __init__(self, gqcnn,
                 dataset_dir,
                 split_name,
                 output_dir,
                 config,
                 name=None):
        """
        Parameters
        ----------
        gqcnn : :obj:`GQCNN`
            grasp quality neural network to optimize
        dataset_dir : str
            path to the training / validation dataset
        split_name : str
            name of the split to train on
        output_dir : str
            path to save the model output
        config : dict
            dictionary of configuration parameters
        name : str
            name of the the model
        """
        self.gqcnn = gqcnn
        self.dataset_dir = dataset_dir
        self.split_name = split_name
        self.output_dir = output_dir
        self.cfg = config
        self.tensorboard_has_launched = False
        self.model_name = name
    
        # check default split
        if split_name is None:
            logging.warning('Using default image-wise split')
            self.split_name = 'image_wise'
        
        # update cfg for saving
        self.cfg['dataset_dir'] = self.dataset_dir
        self.cfg['split_name'] = self.split_name
            
    def _create_loss(self):
        """ Creates a loss based on config file

        Returns
        -------
        :obj:`tensorflow Tensor`
            loss
        """
        if self.cfg['loss'] == 'l2':
            return (1.0 / self.train_batch_size) * tf.nn.l2_loss(tf.subtract(tf.nn.sigmoid(self.train_net_output), self.train_labels_node))
        elif self.cfg['loss'] == 'sparse':
            if self._angular_bins > 0:
                log = tf.reshape(tf.dynamic_partition(self.train_net_output, self.train_pred_mask_node, 2)[1], (-1, 2))
                return tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(_sentinel=None, labels=self.train_labels_node,
                    logits=log))
            else:
                return tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(_sentinel=None, labels=self.train_labels_node, logits=self.train_net_output, name=None))
        elif self.cfg['loss'] == 'weighted_cross_entropy':
            return tf.reduce_mean(tf.nn.weighted_cross_entropy_with_logits(targets=tf.reshape(self.train_labels_node, [-1,1]),
                                                                           logits=self.train_net_output,
                                                                           pos_weight=self.pos_weight,
                                                                           name=None))

    def _create_optimizer(self, loss, batch, var_list, learning_rate):
        """ Create optimizer based on config file

        Parameters
        ----------
        loss : :obj:`tensorflow Tensor`
            loss to use, generated with _create_loss()
        batch : :obj:`tf.Variable`
            variable to keep track of the current gradient step number
        var_list : :obj:`lst`
            list of tf.Variable objects to update to minimize loss(ex. network weights)
        learning_rate : float
            learning rate for training

        Returns
        -------
        :obj:`tf.train.Optimizer`
            optimizer
        """    
        # instantiate optimizer
        if self.cfg['optimizer'] == 'momentum':
            optimizer = tf.train.MomentumOptimizer(learning_rate, self.momentum_rate)
        elif self.cfg['optimizer'] == 'adam':
            optimizer = tf.train.AdamOptimizer(learning_rate)
        elif self.cfg['optimizer'] == 'rmsprop':
            optimizer = tf.train.RMSPropOptimizer(learning_rate)
        else:
            raise ValueError('Optimizer %s not supported' %(self.cfg['optimizer']))

        # compute gradients
        gradients, variables = zip(*optimizer.compute_gradients(loss, var_list=var_list))
        # clip gradients to prevent exploding gradient problem
        gradients, global_grad_norm = tf.clip_by_global_norm(gradients, self.max_global_grad_norm)
        # generate op to apply gradients
        apply_grads = optimizer.apply_gradients(zip(gradients, variables), global_step=batch)

        return apply_grads, global_grad_norm

    def _check_dead_queue(self):
        """ Checks to see if the queue is dead and if so closes the tensorflow session and cleans up the variables """
        if self.dead_event.is_set():
            # close self.session
            self.sess.close()
            
            # cleanup
            for layer_weights in self.weights.values():
                del layer_weights
            del self.saver
            del self.sess

    def _launch_tensorboard(self):
        """ Launches Tensorboard to visualize training """
        FNULL = open(os.devnull, 'w')
        logging.info(
            "Launching Tensorboard, Please navigate to localhost:{} in your favorite web browser to view summaries".format(self._tensorboard_port))
        self._tensorboard_proc = subprocess.Popen(['tensorboard', '--port', str(self._tensorboard_port),'--logdir', self.summary_dir], stdout=FNULL)

    def _close_tensorboard(self):
        """ Closes Tensorboard """
        logging.info('Closing Tensorboard')
        self._tensorboard_proc.terminate()                        

    def train(self):
        """ Perform optimization """
        with self.gqcnn.tf_graph.as_default():
            self._train()
        
    def _train(self):
        """ Perform optimization """
        start_time = time.time()

        # run setup 
        self._setup()
        
        # build network
        self.gqcnn.initialize_network(self.input_im_node, self.input_pose_node)

        # optimize weights
        self._optimize_weights()

    def finetune(self, base_model_dir):
        """ Perform fine-tuning.
        
        Parameters
        ----------
        base_model_dir : str
            path to the base model to use
        """
        with self.gqcnn.tf_graph.as_default():
            self._finetune(base_model_dir)
        
    def _finetune(self, base_model_dir):
        """ Perform fine-tuning.
        
        Parameters
        ----------
        base_model_dir : str
            path to the base model to use
        """
        # run setup 
        self._setup()
        
        # build network
        self.gqcnn.set_base_network(base_model_dir)
        self.gqcnn.initialize_network(self.input_im_node, self.input_pose_node)
        
        # optimize weights
        self._optimize_weights(finetune=True)
        
    def _optimize_weights(self, finetune=False):
        """ Optimize the network weights. """
        start_time = time.time()

        # setup output
        self.train_net_output = self.gqcnn.output
        if self.training_mode == TrainingMode.CLASSIFICATION:
            if self.cfg['loss'] == 'weighted_cross_entropy':
                self.gqcnn.add_sigmoid_to_output()
            else:
                self.gqcnn.add_softmax_to_output()
        elif self.training_mode == TrainingMode.REGRESSION:
            self.gqcnn.add_sigmoid_to_output()
        else:
            raise ValueError('Training mode: {} not supported !'.format(self.training_mode))
        train_predictions = self.gqcnn.output
        drop_rate_in = self.gqcnn.input_drop_rate_node
        self.weights = self.gqcnn.weights
        
        # once weights have been initialized create tf Saver for weights
        self.saver = tf.train.Saver()

        # form loss
        with tf.name_scope('loss'):
            # part 1: error
            loss = self._create_loss()
            unregularized_loss = loss
            
            # part 2: regularization
            layer_weights = self.weights.values()
            with tf.name_scope('regularization'):
                regularizers = tf.nn.l2_loss(layer_weights[0])
                for w in layer_weights[1:]:
                    regularizers = regularizers + tf.nn.l2_loss(w)
            loss += self.train_l2_regularizer * regularizers

        # setup learning rate
        batch = tf.Variable(0)
        learning_rate = tf.train.exponential_decay(
            self.base_lr,                # base learning rate.
            batch * self.train_batch_size,  # current index into the dataset.
            self.decay_step,          # decay step.
            self.decay_rate,                # decay rate.
            staircase=True)

        # setup variable list
        var_list = self.weights.values()
        if finetune:
            var_list = []
            for weights_name, weights_val in self.weights.iteritems():
                layer_name = weight_name_to_layer_name(weights_name)
                if self.optimize_base_layers or layer_name not in self.gqcnn._base_layer_names:
                    var_list.append(weights_val)

        # create optimizer
        with tf.name_scope('optimizer'):
            apply_grad_op, global_grad_norm = self._create_optimizer(loss, batch, var_list, learning_rate)

        def handler(signum, frame):
            logging.info('caught CTRL+C, exiting...')
            self.term_event.set()

            ### Forcefully Exit ####
            # TODO: remove this and figure out why queue thread does not properly exit
            logging.info('Forcefully Exiting Optimization')
            self.forceful_exit = True

            # forcefully kill the session to terminate any current graph ops that are stalling because the enqueue op has ended
            self.sess.close()

            # close tensorboard
            self._close_tensorboard()

            # pause and wait for queue thread to exit before continuing
            logging.info('Waiting for Queue Thread to Exit')
            while not self.queue_thread_exited:
                pass

            logging.info('Cleaning and Preparing to Exit Optimization')
                
            # cleanup
            for layer_weights in self.weights.values():
                del layer_weights
            del self.saver
            del self.sess

            # exit
            logging.info('Exiting Optimization')

            # forcefully exit the script
            exit(0)

        signal.signal(signal.SIGINT, handler)

        # now that everything in our graph is set up we write the graph to the summary event so 
        # it can be visualized in tensorboard
        self.summary_writer.add_graph(self.gqcnn.tf_graph)

        # begin optimization loop
        try:
            self.queue_thread = threading.Thread(target=self._load_and_enqueue)
            self.queue_thread.start()

            # init and run tf self.sessions
            init = tf.global_variables_initializer()
            self.sess.run(init)
            logging.info('Beginning Optimization')

            # create a TrainStatsLogger object to log training statistics at certain intervals
            self.train_stats_logger = TrainStatsLogger(self.model_dir)

            # loop through training steps
            training_range = xrange(int(self.num_epochs * self.num_train) // self.train_batch_size)
            for step in training_range:
                # check for dead queue
                self._check_dead_queue()

                # run optimization
                step_start = time.time()
                if self._angular_bins > 0:
                    _, l, ur_l, lr, predictions, batch_labels, output, train_images, train_poses, pred_mask = self.sess.run([apply_grad_op, loss, unregularized_loss, learning_rate, train_predictions, self.train_labels_node, self.train_net_output, self.input_im_node, self.input_pose_node, self.train_pred_mask_node], feed_dict={drop_rate_in: self.drop_rate}, options=GeneralConstants.timeout_option)
 
                else:
                    _, l, ur_l, lr, predictions, batch_labels, output, train_images, train_poses = self.sess.run(
                        [apply_grad_op, loss, unregularized_loss, learning_rate, train_predictions, self.train_labels_node, self.train_net_output, self.input_im_node, self.input_pose_node], feed_dict={drop_rate_in: self.drop_rate}, options=GeneralConstants.timeout_option)
                step_stop = time.time()
                logging.info('Step took %.3f sec' %(step_stop-step_start))
                
                if self.training_mode == TrainingMode.REGRESSION:
                    logging.info('Max ' +  str(np.max(predictions)))
                    logging.info('Min ' + str(np.min(predictions)))
                elif self.cfg['loss'] != 'weighted_cross_entropy':
                    if self._angular_bins == 0:
                        ex = np.exp(output - np.tile(np.max(output, axis=1)[:,np.newaxis], [1,2]))
                        softmax = ex / np.tile(np.sum(ex, axis=1)[:,np.newaxis], [1,2])
		        
                        logging.info('Max ' +  str(np.max(softmax[:,1])))
                        logging.info('Min ' + str(np.min(softmax[:,1])))
                        logging.info('Pred nonzero ' + str(np.sum(softmax[:,1] > 0.5)))
                        logging.info('True nonzero ' + str(np.sum(batch_labels)))
                   
                else:
                    sigmoid = 1.0 / (1.0 + np.exp(-output))
                    logging.info('Max ' +  str(np.max(sigmoid)))
                    logging.info('Min ' + str(np.min(sigmoid)))
                    logging.info('Pred nonzero ' + str(np.sum(sigmoid > 0.5)))
                    logging.info('True nonzero ' + str(np.sum(batch_labels > 0.5)))

                if np.isnan(l) or np.any(np.isnan(train_poses)):
                    logging.info('Encountered NaN in loss or training poses!')
                    IPython.embed()
                    logging.info('Exiting...')
                    break
                    
                # log output
                if step % self.log_frequency == 0:
                    elapsed_time = time.time() - start_time
                    start_time = time.time()
                    logging.info('Step %d (epoch %.2f), %.1f s' %
                          (step, float(step) * self.train_batch_size / self.num_train,
                           1000 * elapsed_time / self.eval_frequency))
                    logging.info('Minibatch loss: %.3f, learning rate: %.6f' % (l, lr))
                    train_error = l
                    if self.training_mode == TrainingMode.CLASSIFICATION:
                        if self._angular_bins > 0:
                            predictions = predictions[pred_mask.astype(bool)].reshape((-1, 2))
                        classification_result = BinaryClassificationResult(predictions[:,1], batch_labels)
                        train_error = classification_result.error_rate
                        
                    logging.info('Minibatch error: %.3f' %(train_error))
                        
                    self.summary_writer.add_summary(self.sess.run(self.merged_log_summaries, feed_dict={self.minibatch_error_placeholder: train_error, self.minibatch_loss_placeholder: l, self.learning_rate_placeholder: lr}), step)
                    sys.stdout.flush()

                    # update the TrainStatsLogger
                    self.train_stats_logger.update(train_eval_iter=step, train_loss=l, train_error=train_error, total_train_error=None, val_eval_iter=None, val_error=None, learning_rate=lr)

                # evaluate validation error
                if step % self.eval_frequency == 0 and step > 0:
                    if self.cfg['eval_total_train_error']:
                        train_result = self._error_rate_in_batches(validation_set=False)
                        logging.info('Training error: %.3f' %(train_result.error_rate))

                        # update the TrainStatsLogger and save
                        self.train_stats_logger.update(train_eval_iter=None, train_loss=None, train_error=None, total_train_error=train_result.error_rate, total_train_loss=train_result.cross_entropy_loss, val_eval_iter=None, val_error=None, learning_rate=None)
                        self.train_stats_logger.log()
                    
                    if self.train_pct < 1.0:
                        val_result = self._error_rate_in_batches()
                        self.summary_writer.add_summary(self.sess.run(self.merged_eval_summaries, feed_dict={self.val_error_placeholder: val_result.error_rate}), step)
                        logging.info('Validation error: %.3f' %(val_result.error_rate))
			logging.info('Validation loss: %.3f' %(val_result.cross_entropy_loss))
                    sys.stdout.flush()

                    # update the TrainStatsLogger
                    if self.train_pct < 1.0:
                        self.train_stats_logger.update(train_eval_iter=None, train_loss=None, train_error=None, total_train_error=None, val_eval_iter=step, val_loss=val_result.cross_entropy_loss, val_error=val_result.error_rate, learning_rate=None)
                    else:
                        self.train_stats_logger.update(train_eval_iter=None, train_loss=None, train_error=None, total_train_error=None, val_eval_iter=step, learning_rate=None)

                    # save everything!
                    self.train_stats_logger.log()

                # save the model
                if step % self.save_frequency == 0 and step > 0:
                    self.saver.save(self.sess, os.path.join(self.model_dir, 'model_%05d.ckpt' %(step)))
                    self.saver.save(self.sess, os.path.join(self.model_dir, 'model.ckpt'))

                # launch tensorboard only after the first iteration
                if not self.tensorboard_has_launched:
                    self.tensorboard_has_launched = True
                    self._launch_tensorboard()

            # get final errors and flush the stdout pipeline
            final_val_result = self._error_rate_in_batches()
            logging.info('Final validation error: %.3f%%' %final_val_result.error_rate)
	    logging.info('Final validation loss: %.3f' %final_val_result.cross_entropy_loss)
            if self.cfg['eval_total_train_error']:
                final_train_result = self._error_rate_in_batches(validation_set=False)
                logging.info('Final training error: {}'.format(final_train_result.error_rate))
		logging.info('Final training loss: {}'.format(final_train_result.cross_entropy_loss))
            sys.stdout.flush()

            # update the TrainStatsLogger
            self.train_stats_logger.update(train_eval_iter=None, train_loss=None, train_error=None, total_train_error=None, val_eval_iter=step, val_loss=final_val_result.cross_entropy_loss, val_error=final_val_result.error_rate, learning_rate=None)

            # log & save everything!
            self.train_stats_logger.log()
            self.saver.save(self.sess, os.path.join(self.model_dir, 'model.ckpt'))

        except Exception as e:
            self.term_event.set()
            if not self.forceful_exit:
                self.sess.close() 
                for layer_weights in self.weights.values():
                    del layer_weights
                del self.saver
                del self.sess
            raise

        # check for dead queue
        self._check_dead_queue()

        # close sessions
        self.term_event.set()

        # close tensorboard
        self._close_tensorboard()

        # TODO: remove this and figure out why queue thread does not properly exit
        self.sess.close()

        # pause and wait for queue thread to exit before continuing
        logging.info('Waiting for Queue Thread to Exit')
        while not self.queue_thread_exited:
            pass

        logging.info('Cleaning and Preparing to Exit Optimization')
        self.sess.close()
            
        # cleanup
        for layer_weights in self.weights.values():
            del layer_weights
        del self.saver
        del self.sess

        # exit
        logging.info('Exiting Optimization')

    def _compute_data_metrics(self):
        """ Calculate image mean, image std, pose mean, pose std, normalization params """
        # subsample tensors (for faster runtime)
        random_file_indices = np.random.choice(self.num_tensors,
                                               size=self.num_random_files,
                                               replace=False)
        
        if self.gqcnn.input_depth_mode == InputDepthMode.POSE_STREAM:
            # compute image stats
            im_mean_filename = os.path.join(self.model_dir, 'im_mean.npy')
            im_std_filename = os.path.join(self.model_dir, 'im_std.npy')
            if os.path.exists(im_mean_filename) and os.path.exists(im_std_filename):
                self.im_mean = np.load(im_mean_filename)
                self.im_std = np.load(im_std_filename)
            else:
                self.im_mean = 0
                self.im_std = 0

                # compute mean
                logging.info('Computing image mean')
                num_summed = 0
                for k, i in enumerate(random_file_indices):
                    if k % self.preproc_log_frequency == 0:
                        logging.info('Adding file %d of %d to image mean estimate' %(k+1, random_file_indices.shape[0]))
                    im_data = self.dataset.tensor(self.im_field_name, i).arr
                    train_indices = self.train_index_map[i]
                    if train_indices.shape[0] > 0:
                        self.im_mean += np.sum(im_data[train_indices, ...])
                        num_summed += self.train_index_map[i].shape[0] * im_data.shape[1] * im_data.shape[2]
                self.im_mean = self.im_mean / num_summed

                # compute std
                logging.info('Computing image std')
                for k, i in enumerate(random_file_indices):
                    if k % self.preproc_log_frequency == 0:
                        logging.info('Adding file %d of %d to image std estimate' %(k+1, random_file_indices.shape[0]))
                    im_data = self.dataset.tensor(self.im_field_name, i).arr
                    train_indices = self.train_index_map[i]
                    if train_indices.shape[0] > 0:
                        self.im_std += np.sum((im_data[train_indices, ...] - self.im_mean)**2)
                self.im_std = np.sqrt(self.im_std / num_summed)

                # save
                np.save(im_mean_filename, self.im_mean)
                np.save(im_std_filename, self.im_std)

            # update gqcnn
            self.gqcnn.set_im_mean(self.im_mean)
            self.gqcnn.set_im_std(self.im_std)

            # compute pose stats
            pose_mean_filename = os.path.join(self.model_dir, 'pose_mean.npy')
            pose_std_filename = os.path.join(self.model_dir, 'pose_std.npy')
            if os.path.exists(pose_mean_filename) and os.path.exists(pose_std_filename):
                self.pose_mean = np.load(pose_mean_filename)
                self.pose_std = np.load(pose_std_filename)
            else:
                self.pose_mean = np.zeros(self.raw_pose_shape)
                self.pose_std = np.zeros(self.raw_pose_shape)

                # compute mean
                num_summed = 0
                logging.info('Computing pose mean')
                for k, i in enumerate(random_file_indices):
                    if k % self.preproc_log_frequency == 0:
                        logging.info('Adding file %d of %d to pose mean estimate' %(k+1, random_file_indices.shape[0]))
                    pose_data = self.dataset.tensor(self.pose_field_name, i).arr
                    train_indices = self.train_index_map[i]
                    if self.gripper_mode == GripperMode.SUCTION:
                        rand_indices = np.random.choice(pose_data.shape[0],
                                                        size=pose_data.shape[0]/2,
                                                        replace=False)
                        pose_data[rand_indices, 4] = -pose_data[rand_indices, 4]
                    elif self.gripper_mode == GripperMode.LEGACY_SUCTION:
                        rand_indices = np.random.choice(pose_data.shape[0],
                                                        size=pose_data.shape[0]/2,
                                                        replace=False)
                        pose_data[rand_indices, 3] = -pose_data[rand_indices, 3]
                    if train_indices.shape[0] > 0:
                        pose_data = pose_data[train_indices,:]
                        pose_data = pose_data[np.isfinite(pose_data[:,3]),:]
                        self.pose_mean += np.sum(pose_data, axis=0)
                        num_summed += pose_data.shape[0]
                self.pose_mean = self.pose_mean / num_summed

                # compute std
                logging.info('Computing pose std')
                for k, i in enumerate(random_file_indices):
                    if k % self.preproc_log_frequency == 0:
                        logging.info('Adding file %d of %d to pose std estimate' %(k+1, random_file_indices.shape[0]))
                    pose_data = self.dataset.tensor(self.pose_field_name, i).arr
                    train_indices = self.train_index_map[i]
                    if self.gripper_mode == GripperMode.SUCTION:
                        rand_indices = np.random.choice(pose_data.shape[0],
                                                        size=pose_data.shape[0]/2,
                                                        replace=False)
                        pose_data[rand_indices, 4] = -pose_data[rand_indices, 4]
                    elif self.gripper_mode == GripperMode.LEGACY_SUCTION:
                        rand_indices = np.random.choice(pose_data.shape[0],
                                                        size=pose_data.shape[0]/2,
                                                        replace=False)
                        pose_data[rand_indices, 3] = -pose_data[rand_indices, 3]
                    if train_indices.shape[0] > 0:
                        pose_data = pose_data[train_indices,:]
                        pose_data = pose_data[np.isfinite(pose_data[:,3]),:]
                        self.pose_std += np.sum((pose_data - self.pose_mean)**2, axis=0)
                self.pose_std = np.sqrt(self.pose_std / num_summed)
                self.pose_std[self.pose_std==0] = 1.0

                # save
                self.pose_mean = read_pose_data(self.pose_mean, self.gripper_mode)
                self.pose_std = read_pose_data(self.pose_std, self.gripper_mode)
                np.save(pose_mean_filename, self.pose_mean)
                np.save(pose_std_filename, self.pose_std)

            # update gqcnn
            self.gqcnn.set_pose_mean(self.pose_mean)
            self.gqcnn.set_pose_std(self.pose_std)

            # check for invalid values
            if np.any(np.isnan(self.pose_mean)) or np.any(np.isnan(self.pose_std)):
                logging.error('Pose mean or pose std is NaN! Check the input dataset')
                IPython.embed()
                exit(0)

        elif self.gqcnn.input_depth_mode == InputDepthMode.SUB:
            # compute (image - depth) stats
            im_depth_sub_mean_filename = os.path.join(self.model_dir, 'im_depth_sub_mean.npy')
            im_depth_sub_std_filename = os.path.join(self.model_dir, 'im_depth_sub_std.npy')
            if os.path.exists(im_depth_sub_mean_filename) and os.path.exists(im_depth_sub_std_filename):
                self.im_depth_sub_mean = np.load(im_depth_sub_mean_filename)
                self.im_depth_sub_std = np.load(im_depth_sub_std_filename)
            else:
                self.im_depth_sub_mean = 0
                self.im_depth_sub_std = 0

                # compute mean
                logging.info('Computing (image - depth) mean')
                num_summed = 0
                for k, i in enumerate(random_file_indices):
                    if k % self.preproc_log_frequency == 0:
                        logging.info('Adding file %d of %d to (image - depth) mean estimate' %(k+1, random_file_indices.shape[0]))
                    im_data = self.dataset.tensor(self.im_field_name, i).arr
                    depth_data = read_pose_data(self.dataset.tensor(self.pose_field_name, i).arr, self.gripper_mode)
                    sub_data = im_data - np.tile(np.reshape(depth_data, (-1, 1, 1, 1)), (1, im_data.shape[1], im_data.shape[2], 1))
                    train_indices = self.train_index_map[i]
                    if train_indices.shape[0] > 0:
                        self.im_depth_sub_mean += np.sum(sub_data[train_indices, ...])
                        num_summed += self.train_index_map[i].shape[0] * im_data.shape[1] * im_data.shape[2]
                self.im_depth_sub_mean = self.im_depth_sub_mean / num_summed

                # compute std
                logging.info('Computing (image - depth) std')
                for k, i in enumerate(random_file_indices):
                    if k % self.preproc_log_frequency == 0:
                        logging.info('Adding file %d of %d to (image - depth) std estimate' %(k+1, random_file_indices.shape[0]))
                    im_data = self.dataset.tensor(self.im_field_name, i).arr
                    depth_data = read_pose_data(self.dataset.tensor(self.pose_field_name, i).arr, self.gripper_mode)
                    sub_data = im_data - np.tile(np.reshape(depth_data, (-1, 1, 1, 1)), (1, im_data.shape[1], im_data.shape[2], 1))
                    train_indices = self.train_index_map[i]
                    if train_indices.shape[0] > 0:
                        self.im_depth_sub_std += np.sum((sub_data[train_indices, ...] - self.im_depth_sub_mean)**2)
                self.im_depth_sub_std = np.sqrt(self.im_depth_sub_std / num_summed)

                # save
                np.save(im_depth_sub_mean_filename, self.im_depth_sub_mean)
                np.save(im_depth_sub_std_filename, self.im_depth_sub_std)

            # update gqcnn
            self.gqcnn.set_im_depth_sub_mean(self.im_depth_sub_mean)
            self.gqcnn.set_im_depth_sub_std(self.im_depth_sub_std)

	elif self.gqcnn.input_depth_mode == InputDepthMode.IM_ONLY:
            # compute image stats
            im_mean_filename = os.path.join(self.model_dir, 'im_mean.npy')
            im_std_filename = os.path.join(self.model_dir, 'im_std.npy')
            if os.path.exists(im_mean_filename) and os.path.exists(im_std_filename):
                self.im_mean = np.load(im_mean_filename)
                self.im_std = np.load(im_std_filename)
            else:
                self.im_mean = 0
                self.im_std = 0

                # compute mean
                logging.info('Computing image mean')
                num_summed = 0
                for k, i in enumerate(random_file_indices):
                    if k % self.preproc_log_frequency == 0:
                        logging.info('Adding file %d of %d to image mean estimate' %(k+1, random_file_indices.shape[0]))
                    im_data = self.dataset.tensor(self.im_field_name, i).arr
                    train_indices = self.train_index_map[i]
                    if train_indices.shape[0] > 0:
                        self.im_mean += np.sum(im_data[train_indices, ...])
                        num_summed += self.train_index_map[i].shape[0] * im_data.shape[1] * im_data.shape[2]
                self.im_mean = self.im_mean / num_summed

                # compute std
                logging.info('Computing image std')
                for k, i in enumerate(random_file_indices):
                    if k % self.preproc_log_frequency == 0:
                        logging.info('Adding file %d of %d to image std estimate' %(k+1, random_file_indices.shape[0]))
                    im_data = self.dataset.tensor(self.im_field_name, i).arr
                    train_indices = self.train_index_map[i]
                    if train_indices.shape[0] > 0:
                        self.im_std += np.sum((im_data[train_indices, ...] - self.im_mean)**2)
                self.im_std = np.sqrt(self.im_std / num_summed)

                # save
                np.save(im_mean_filename, self.im_mean)
                np.save(im_std_filename, self.im_std)

            # update gqcnn
            self.gqcnn.set_im_mean(self.im_mean)
            self.gqcnn.set_im_std(self.im_std)
               
        # compute normalization parameters of the network
        pct_pos_train_filename = os.path.join(self.model_dir, 'pct_pos_train.npy')
        pct_pos_val_filename = os.path.join(self.model_dir, 'pct_pos_val.npy')
        if os.path.exists(pct_pos_train_filename) and os.path.exists(pct_pos_val_filename):
            pct_pos_train = np.load(pct_pos_train_filename)
            pct_pos_val = np.load(pct_pos_val_filename)
        else:
            logging.info('Computing metric stats')
            all_train_metrics = None
            all_val_metrics = None
    
            # read metrics
            for k, i in enumerate(random_file_indices):
                if k % self.preproc_log_frequency == 0:
                    logging.info('Adding file %d of %d to metric stat estimates' %(k+1, random_file_indices.shape[0]))
                metric_data = self.dataset.tensor(self.label_field_name, i).arr
                train_indices = self.train_index_map[i]
                val_indices = self.val_index_map[i]

                if train_indices.shape[0] > 0:
                    train_metric_data = metric_data[train_indices]
                    if all_train_metrics is None:
                        all_train_metrics = train_metric_data
                    else:
                        all_train_metrics = np.r_[all_train_metrics, train_metric_data]

                if val_indices.shape[0] > 0:
                    val_metric_data = metric_data[val_indices]
                    if all_val_metrics is None:
                        all_val_metrics = val_metric_data
                    else:
                        all_val_metrics = np.r_[all_val_metrics, val_metric_data]

            # compute train stats
            self.min_metric = np.min(all_train_metrics)
            self.max_metric = np.max(all_train_metrics)
            self.mean_metric = np.mean(all_train_metrics)
            self.median_metric = np.median(all_train_metrics)

            # save metrics
            pct_pos_train = float(np.sum(all_train_metrics > self.metric_thresh)) / all_train_metrics.shape[0]
            np.save(pct_pos_train_filename, np.array(pct_pos_train))

            if self.train_pct < 1.0:
                pct_pos_val = float(np.sum(all_val_metrics > self.metric_thresh)) / all_val_metrics.shape[0]
                np.save(pct_pos_val_filename, np.array(pct_pos_val))
                
        logging.info('Percent positive in train: ' + str(pct_pos_train))
        if self.train_pct < 1.0:
            logging.info('Percent positive in val: ' + str(pct_pos_val))

        if self._angular_bins > 0:
            logging.info('Calculating angular bin statistics...')
            bin_counts = np.zeros((self._angular_bins,))
            for m in range(self.num_tensors):
                pose_arr = self.dataset.tensor(self.pose_field_name, m).arr
                angles = pose_arr[:, 3]
                neg_ind = np.where(angles < 0)
                angles = np.abs(angles) % GeneralConstants.PI
                angles[neg_ind] *= -1
                g_90 = np.where(angles > (GeneralConstants.PI / 2))
                l_neg_90 = np.where(angles < (-1 * (GeneralConstants.PI / 2)))
                angles[g_90] -= GeneralConstants.PI
                angles[l_neg_90] += GeneralConstants.PI
                angles *= -1 # hack to fix reverse angle convention
                angles += (GeneralConstants.PI / 2)
                for i in range(angles.shape[0]):
                    bin_counts[int(angles[i] // self._bin_width)] += 1
            logging.info('Bin counts: {}'.format(bin_counts))

    def _compute_split_indices(self):
        """ Compute train and validation indices for each tensor to speed data accesses"""
        # read indices
        train_indices, val_indices, _ = self.dataset.split(self.split_name)

        # loop through tensors, assigning indices to each file
        self.train_index_map = {}
        for i in range(self.dataset.num_tensors):
            self.train_index_map[i] = []
            
        for i in train_indices:
            tensor_index = self.dataset.tensor_index(i)
            datapoint_indices = self.dataset.datapoint_indices_for_tensor(tensor_index)
            lowest = np.min(datapoint_indices)
            self.train_index_map[tensor_index].append(i - lowest)

        for i, indices in self.train_index_map.iteritems():
            self.train_index_map[i] = np.array(indices)
            
        self.val_index_map = {}
        for i in range(self.dataset.num_tensors):
            self.val_index_map[i] = []
            
        for i in val_indices:
            tensor_index = self.dataset.tensor_index(i)
            if tensor_index not in self.val_index_map.keys():
                self.val_index_map[tensor_index] = []
            datapoint_indices = self.dataset.datapoint_indices_for_tensor(tensor_index)
            lowest = np.min(datapoint_indices)
            self.val_index_map[tensor_index].append(i - lowest)

        for i, indices in self.val_index_map.iteritems():
            self.val_index_map[i] = np.array(indices)
            
    def _setup_output_dirs(self):
        """ Setup output directories """
        # create a directory for the model
        if self.model_name is None:
            model_id = utils.gen_experiment_id()
            self.model_name = 'model_%s' %(model_id)
        self.model_dir = os.path.join(self.output_dir, self.model_name)
        if not os.path.exists(self.model_dir):
            os.mkdir(self.model_dir)

        # create the summary dir
        self.summary_dir = os.path.join(self.model_dir, 'tensorboard_summaries')
        if not os.path.exists(self.summary_dir):
            os.mkdir(self.summary_dir)
        else:
            # if the summary directory already exists, clean it out by deleting all files in it
            # we don't want tensorboard to get confused with old logs while debugging with the same directory
            old_files = os.listdir(self.summary_dir)
            for f in old_files:
                os.remove(os.path.join(self.summary_dir, f))

        logging.info('Saving model to %s' %(self.model_dir))
            
    def _setup_logging(self):
        """ Copy the original config files """
        # save config
        out_config_filename = os.path.join(self.model_dir, 'config.json')
        tempOrderedDict = collections.OrderedDict()
        for key in self.cfg.keys():
            tempOrderedDict[key] = self.cfg[key]
        with open(out_config_filename, 'w') as outfile:
            json.dump(tempOrderedDict,
                      outfile,
                      indent=GeneralConstants.JSON_INDENT)

        # setup logging
        self.log_filename = os.path.join(self.model_dir, 'training.log')
        formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
        hdlr = logging.FileHandler(self.log_filename)
        hdlr.setFormatter(formatter)
        logging.getLogger().addHandler(hdlr)
            
        # save training script    
        this_filename = sys.argv[0]
        out_train_filename = os.path.join(self.model_dir, 'training_script.py')
        shutil.copyfile(this_filename, out_train_filename)

        # save architecture
        out_architecture_filename = os.path.join(self.model_dir, 'architecture.json')
        json.dump(self.cfg['gqcnn']['architecture'],
                  open(out_architecture_filename, 'w'),
                  indent=GeneralConstants.JSON_INDENT)
        
    def _read_training_params(self):
        """ Read training parameters from configuration file """
        # splits
        self.train_pct = self.cfg['train_pct']
        self.total_pct = self.cfg['total_pct']

        # training sizes
        self.train_batch_size = self.cfg['train_batch_size']
        self.val_batch_size = self.cfg['val_batch_size']
        self.max_files_eval = None
        if 'max_files_eval' in self.cfg.keys():
            self.max_files_eval = self.cfg['max_files_eval']
        
        # logging
        self.num_epochs = self.cfg['num_epochs']
        self.eval_frequency = self.cfg['eval_frequency']
        self.save_frequency = self.cfg['save_frequency']
        self.log_frequency = self.cfg['log_frequency']

        # optimization
        self.train_l2_regularizer = self.cfg['train_l2_regularizer']
        self.base_lr = self.cfg['base_lr']
        self.decay_step_multiplier = self.cfg['decay_step_multiplier']
        self.decay_rate = self.cfg['decay_rate']
        self.momentum_rate = self.cfg['momentum_rate']
        self.max_training_examples_per_load = self.cfg['max_training_examples_per_load']
        self.drop_rate = self.cfg['drop_rate']
        self.max_global_grad_norm = self.cfg['max_global_grad_norm']
        self.optimize_base_layers = False
        if 'optimize_base_layers' in self.cfg.keys():
            self.optimize_base_layers = self.cfg['optimize_base_layers']
        
        # metrics
        self.target_metric_name = self.cfg['target_metric_name']
        self.metric_thresh = self.cfg['metric_thresh']
        self.training_mode = self.cfg['training_mode']
        if self.training_mode != TrainingMode.CLASSIFICATION:
            raise ValueError('Training mode %s not currently supported!' %(self.training_mode))
        
        # tensorboad
        self._tensorboard_port = self.cfg['tensorboard_port']
        
        # preproc
        self.preproc_log_frequency = self.cfg['preproc_log_frequency']
        self.num_random_files = self.cfg['num_random_files']

        # re-weighting positives / negatives
        self.pos_weight = 0.0
        if 'pos_weight' in self.cfg.keys():
            self.pos_weight = self.cfg['pos_weight']
            self.pos_accept_prob = 1.0
            self.neg_accept_prob = 1.0
            if self.pos_weight > 1:
                self.neg_accept_prob = 1.0 / self.pos_weight
            else:
                self.pos_accept_prob = self.pos_weight
                
        if self.train_pct < 0 or self.train_pct > 1:
            raise ValueError('Train percentage must be in range [0,1]')

        if self.total_pct < 0 or self.total_pct > 1:
            raise ValueError('Total percentage must be in range [0,1]')

        # normalization
        self._norm_inputs = True
        if self.gqcnn.input_depth_mode == InputDepthMode.SUB:
            self._norm_inputs = False       
 
        # angular training
        self._angular_bins = self.gqcnn.angular_bins

        # during angular training, make sure symmetrization in denoising is turned off and also set the angular bin width
        if self._angular_bins > 0:
            assert not self.cfg['symmetrize'], 'Symmetrization denoising must be turned off during angular training'
            self._bin_width = GeneralConstants.PI / self._angular_bins

    def _setup_denoising_and_synthetic(self):
        """ Setup denoising and synthetic data parameters """
        # multiplicative denoising
        if self.cfg['multiplicative_denoising']:
            self.gamma_shape = self.cfg['gamma_shape']
            self.gamma_scale = 1.0 / self.gamma_shape
        # gaussian process noise    
        if self.cfg['gaussian_process_denoising']:
            self.gp_rescale_factor = self.cfg['gaussian_process_scaling_factor']
            self.gp_sample_height = int(self.im_height / self.gp_rescale_factor)
            self.gp_sample_width = int(self.im_width / self.gp_rescale_factor)
            self.gp_num_pix = self.gp_sample_height * self.gp_sample_width
            self.gp_sigma = self.cfg['gaussian_process_sigma']

    def _open_dataset(self):
        """ Open the dataset """
        # read in filenames of training data(poses, images, labels)
        self.dataset = TensorDataset.open(self.dataset_dir)
        self.num_datapoints = self.dataset.num_datapoints
        self.num_tensors = self.dataset.num_tensors
        self.datapoints_per_file = self.dataset.datapoints_per_file
        self.num_random_files = min(self.num_tensors, self.num_random_files)

        # read split
        if not self.dataset.has_split(self.split_name):
            logging.info('Training split: {} not found in dataset. Creating new split...'.format(self.split_name))
            self.dataset.make_split(self.split_name, train_pct=self.train_pct)
        else:
            logging.info('Training split: {} found in dataset.'.format(self.split_name))
        self._compute_split_indices()
        
    def _compute_data_params(self):
        """ Compute parameters of the dataset """
        # image params
        self.im_field_name = self.cfg['image_field_name']
        self.im_height = self.dataset.config['fields'][self.im_field_name]['height']
        self.im_width = self.dataset.config['fields'][self.im_field_name]['width']
        self.im_channels = self.dataset.config['fields'][self.im_field_name]['channels']
        self.im_center = np.array([float(self.im_height-1)/2, float(self.im_width-1)/2])

        # poses
        self.pose_field_name = self.cfg['pose_field_name']
        self.gripper_mode = self.gqcnn.gripper_mode
        self.pose_dim = pose_dim(self.gripper_mode)
        self.raw_pose_shape = self.dataset.config['fields'][self.pose_field_name]['height']
        
        # outputs
        self.label_field_name = self.target_metric_name
        self.num_categories = 2

        # compute the number of train and val examples
        self.num_train = 0
        self.num_val = 0
        for train_indices in self.train_index_map.values():
            self.num_train += train_indices.shape[0]
        for val_indices in self.train_index_map.values():
            self.num_val += val_indices.shape[0]

        # set params based on the number of training examples (convert epochs to steps)
        self.eval_frequency = int(np.ceil(self.eval_frequency * (float(self.num_train) / self.train_batch_size)))
        self.save_frequency = int(np.ceil(self.save_frequency * (float(self.num_train) / self.train_batch_size)))
        self.decay_step = self.decay_step_multiplier * self.num_train

    def _setup_tensorflow(self):
        """Setup Tensorflow placeholders, session, and queue """

        # setup nodes
        with tf.name_scope('train_data_node'):
            self.train_data_batch = tf.placeholder(tf.float32, (self.train_batch_size, self.im_height, self.im_width, self.im_channels))
        with tf.name_scope('train_pose_node'):
            self.train_poses_batch = tf.placeholder(tf.float32, (self.train_batch_size, self.pose_dim))
        if self.training_mode == TrainingMode.REGRESSION:
            train_label_dtype = tf.float32
            self.numpy_dtype = np.float32
        elif self.training_mode == TrainingMode.CLASSIFICATION:
            train_label_dtype = tf.int64
            self.numpy_dtype = np.int64
            if self.cfg['loss'] == 'weighted_cross_entropy':
                train_label_dtype = tf.float32
                self.numpy_dtype = np.float32            
        else:
            raise ValueError('Training mode %s not supported' %(self.training_mode))
        with tf.name_scope('train_labels_node'):
            self.train_labels_batch = tf.placeholder(train_label_dtype, (self.train_batch_size,))
        if self._angular_bins > 0:
            self.train_pred_mask_batch = tf.placeholder(tf.int32, (self.train_batch_size, self._angular_bins*2))

        # create queue
        with tf.name_scope('data_queue'):
            if self._angular_bins > 0:
                self.q = tf.FIFOQueue(GeneralConstants.QUEUE_CAPACITY, [tf.float32, tf.float32, train_label_dtype, tf.int32], shapes=[(self.train_batch_size, self.im_height, self.im_width, self.im_channels), (self.train_batch_size, self.pose_dim), (self.train_batch_size,), (self.train_batch_size, self._angular_bins * 2)])
                self.enqueue_op = self.q.enqueue([self.train_data_batch, self.train_poses_batch, self.train_labels_batch, self.train_pred_mask_batch])
                self.train_labels_node = tf.placeholder(train_label_dtype, (self.train_batch_size,))
                self.input_im_node, self.input_pose_node, self.train_labels_node, self.train_pred_mask_node = self.q.dequeue()
            else:
                self.q = tf.FIFOQueue(GeneralConstants.QUEUE_CAPACITY, [tf.float32, tf.float32, train_label_dtype], shapes=[(self.train_batch_size, self.im_height, self.im_width, self.im_channels), (self.train_batch_size, self.pose_dim), (self.train_batch_size,)])
                self.enqueue_op = self.q.enqueue([self.train_data_batch, self.train_poses_batch, self.train_labels_batch])
                self.train_labels_node = tf.placeholder(train_label_dtype, (self.train_batch_size,))
                self.input_im_node, self.input_pose_node, self.train_labels_node = self.q.dequeue()

        # get weights
        self.weights = self.gqcnn.weights
            
        # open a tf session for the gqcnn object and store it also as the optimizer session
        self.sess = self.gqcnn.open_session()

        # setup term event/dead event
        self.term_event = threading.Event()
        self.term_event.clear()
        self.dead_event = threading.Event()
        self.dead_event.clear()

    def _setup_summaries(self):
        """ Sets up placeholders for summary values and creates summary writer """
        # we create placeholders for our python values because summary_scalar expects
        # a placeholder, not simply a python value 
        self.val_error_placeholder = tf.placeholder(tf.float32, [])
        self.minibatch_error_placeholder = tf.placeholder(tf.float32, [])
        self.minibatch_loss_placeholder = tf.placeholder(tf.float32, [])
        self.learning_rate_placeholder = tf.placeholder(tf.float32, [])

        # we create summary scalars with tags that allow us to group them together so we can write different batches
        # of summaries at different intervals
        tf.summary.scalar('val_error', self.val_error_placeholder, collections=["eval_frequency"])
        tf.summary.scalar('minibatch_error', self.minibatch_error_placeholder, collections=["log_frequency"])
        tf.summary.scalar('minibatch_loss', self.minibatch_loss_placeholder, collections=["log_frequency"])
        tf.summary.scalar('learning_rate', self.learning_rate_placeholder, collections=["log_frequency"])
        self.merged_eval_summaries = tf.summary.merge_all("eval_frequency")
        self.merged_log_summaries = tf.summary.merge_all("log_frequency")

        # create a tf summary writer with the specified summary directory
        self.summary_writer = tf.summary.FileWriter(self.summary_dir)

        # initialize the variables again now that we have added some new ones
        with self.sess.as_default():
            tf.global_variables_initializer().run()
        
    def _setup(self):
        """ Setup for optimization """

        # set up logger
        logging.getLogger().setLevel(logging.INFO)

        # initialize thread exit booleans
        self.queue_thread_exited = False
        self.forceful_exit = False

        # set random seed for deterministic execution
        np.random.seed(self.cfg['seed'])
        random.seed(self.cfg['seed'])

        # setup output directories
        self._setup_output_dirs()

        # setup logging
        self._setup_logging()

        # read training parameters from config file
        self._read_training_params()

        # setup image and pose data files
        self._open_dataset() 

        # compute data parameters
        self._compute_data_params()
 
        # setup denoising and synthetic data parameters
        self._setup_denoising_and_synthetic()
          
        # compute means, std's, and normalization metrics
        self._compute_data_metrics()

        # setup tensorflow session/placeholders/queue
        self._setup_tensorflow()

        # setup summaries for visualizing metrics in tensorboard
        self._setup_summaries()

    def _load_and_enqueue(self):
        """ Loads and Enqueues a batch of images for training """
        # open dataset
        dataset = TensorDataset.open(self.dataset_dir)

        while not self.term_event.is_set():
            # sleep between reads
            time.sleep(GeneralConstants.QUEUE_SLEEP)

            # loop through data
            num_queued = 0
            start_i = 0
            end_i = 0
            file_num = 0
            queue_start = time.time()

            # init buffers
            train_images = np.zeros(
                [self.train_batch_size, self.im_height, self.im_width, self.im_channels]).astype(np.float32)
            train_poses = np.zeros([self.train_batch_size, self.pose_dim]).astype(np.float32)
            train_labels = np.zeros(self.train_batch_size).astype(self.numpy_dtype)
            if self._angular_bins > 0:
                train_pred_mask = np.zeros((self.train_batch_size, self._angular_bins*2), dtype=bool)
            
            while start_i < self.train_batch_size:
                # compute num remaining
                num_remaining = self.train_batch_size - num_queued
                
                # gen file index uniformly at random
                file_num = np.random.choice(self.num_tensors, size=1)[0]

                read_start = time.time()
                train_images_tensor = dataset.tensor(self.im_field_name, file_num)
                train_poses_tensor = dataset.tensor(self.pose_field_name, file_num)
                train_labels_tensor = dataset.tensor(self.label_field_name, file_num)
                read_stop = time.time()
                logging.debug('Reading data took %.3f sec' %(read_stop - read_start))
                logging.debug('File num: %d' %(file_num))
                
                # get batch indices uniformly at random
                train_ind = self.train_index_map[file_num]
                np.random.shuffle(train_ind)
                if self.gripper_mode == GripperMode.LEGACY_SUCTION:
                    tp_tmp = read_pose_data(train_poses_tensor.data, self.gripper_mode)
                    train_ind = train_ind[np.isfinite(tp_tmp[train_ind,1])]
                    
                # filter positives and negatives
                if self.training_mode == TrainingMode.CLASSIFICATION and self.pos_weight != 0.0:
                    labels = 1 * (train_labels_tensor.arr > self.metric_thresh)
                    np.random.shuffle(train_ind)
                    filtered_ind = []
                    for index in train_ind:
                        if labels[index] == 0 and np.random.rand() < self.neg_accept_prob:
                            filtered_ind.append(index)
                        elif labels[index] == 1 and np.random.rand() < self.pos_accept_prob:
                            filtered_ind.append(index)
                    train_ind = np.array(filtered_ind)

                # samples train indices
                upper = min(num_remaining, train_ind.shape[0], self.max_training_examples_per_load)
                ind = train_ind[:upper]
                num_loaded = ind.shape[0]
                if num_loaded == 0:
                    logging.debug('Loaded zero examples!!!!')
                    continue
                
                # subsample data
                train_images_arr = train_images_tensor.arr[ind, ...]
                train_poses_arr = train_poses_tensor.arr[ind, ...]
                angles = train_poses_arr[:, 3]
                train_label_arr = train_labels_tensor.arr[ind]
                num_images = train_images_arr.shape[0]

                # resize images
                rescale_factor = float(self.im_height) / train_images_arr.shape[1]
                if rescale_factor != 1.0:
                    resized_train_images_arr = np.zeros([num_images,
                                                         self.im_height,
                                                         self.im_width,
                                                         self.im_channels]).astype(np.float32)
                    for i in range(num_images):
                        for c in range(train_images_arr.shape[3]):
                            resized_train_images_arr[i,:,:,c] = sm.imresize(train_images_arr[i,:,:,c],
                                                                            rescale_factor,
                                                                            interp='bicubic', mode='F')
                    train_images_arr = resized_train_images_arr
                
                # add noises to images
                train_images_arr, train_poses_arr = self._distort(train_images_arr, train_poses_arr)

                # slice poses
                train_poses_arr = read_pose_data(train_poses_arr,
                                                 self.gripper_mode)

                # standardize inputs and outputs
                if self._norm_inputs:
                    train_images_arr = (train_images_arr - self.im_mean) / self.im_std
		    if self.gqcnn.input_depth_mode == InputDepthMode.POSE_STREAM:
                    	train_poses_arr = (train_poses_arr - self.pose_mean) / self.pose_std
                train_label_arr = 1 * (train_label_arr > self.metric_thresh)
                train_label_arr = train_label_arr.astype(self.numpy_dtype)

                if self._angular_bins > 0:
                    bins = np.zeros_like(train_label_arr)
                    # form prediction mask to use when calculating loss
                    neg_ind = np.where(angles < 0)
                    angles = np.abs(angles) % GeneralConstants.PI
                    angles[neg_ind] *= -1
                    g_90 = np.where(angles > (GeneralConstants.PI / 2))
                    l_neg_90 = np.where(angles < (-1 * (GeneralConstants.PI / 2)))
                    angles[g_90] -= GeneralConstants.PI
                    angles[l_neg_90] += GeneralConstants.PI
                    angles *= -1 # hack to fix reverse angle convention
                    angles += (GeneralConstants.PI / 2)
                    train_pred_mask_arr = np.zeros((train_label_arr.shape[0], self._angular_bins*2))
                    for i in range(angles.shape[0]):
                        bins[i] = angles[i] // self._bin_width
                        train_pred_mask_arr[i, int((angles[i] // self._bin_width)*2)] = 1
                        train_pred_mask_arr[i, int((angles[i] // self._bin_width)*2 + 1)] = 1

                # compute the number of examples loaded
                num_loaded = train_images_arr.shape[0]
                end_i = start_i + num_loaded
                    
                # enqueue training data batch
                train_images[start_i:end_i, ...] = train_images_arr.copy()
                train_poses[start_i:end_i,:] = train_poses_arr.copy()
                train_labels[start_i:end_i] = train_label_arr.copy()
                if self._angular_bins > 0:
                    train_pred_mask[start_i:end_i] = train_pred_mask_arr.copy()

                del train_images_arr
                del train_poses_arr
                del train_label_arr
		
                # update start index
                start_i = end_i
                num_queued += num_loaded

            # send data to queue
            if not self.term_event.is_set():
                try:
                    if self._angular_bins > 0:
                        self.sess.run(self.enqueue_op, feed_dict={self.train_data_batch: train_images,
                                                              self.train_poses_batch: train_poses,
                                                              self.train_labels_batch: train_labels,
                                                              self.train_pred_mask_batch: train_pred_mask})                       
                    else:
                        self.sess.run(self.enqueue_op, feed_dict={self.train_data_batch: train_images,
                                                              self.train_poses_batch: train_poses,
                                                              self.train_labels_batch: train_labels})
                    queue_stop = time.time()
                    logging.debug('Queue batch took %.3f sec' %(queue_stop - queue_start))
                except:
                    pass
        del train_images
        del train_poses
        del train_labels
        self.dead_event.set()
        logging.info('Queue Thread Exiting')
        self.queue_thread_exited = True

    def _distort(self, image_arr, pose_arr):
        """ Adds noise to a batch of images """
        # read params
        num_images = image_arr.shape[0]
        
        # denoising and synthetic data generation
        if self.cfg['multiplicative_denoising']:
            mult_samples = ss.gamma.rvs(self.gamma_shape, scale=self.gamma_scale, size=num_images)
            mult_samples = mult_samples[:,np.newaxis,np.newaxis,np.newaxis]
            image_arr = image_arr * np.tile(mult_samples, [1, self.im_height, self.im_width, self.im_channels])

        # add correlated Gaussian noise
        if self.cfg['gaussian_process_denoising']:
            for i in range(num_images):
                if np.random.rand() < self.cfg['gaussian_process_rate']:
                    train_image = image_arr[i,:,:,0]
                    gp_noise = ss.norm.rvs(scale=self.gp_sigma, size=self.gp_num_pix).reshape(self.gp_sample_height, self.gp_sample_width)
                    gp_noise = sm.imresize(gp_noise, self.gp_rescale_factor, interp='bicubic', mode='F')
                    train_image[train_image > 0] += gp_noise[train_image > 0]
                    image_arr[i,:,:,0] = train_image

        # symmetrize images
        if self.cfg['symmetrize']:
            for i in range(num_images):
                train_image = image_arr[i,:,:,0]
                # rotate with 50% probability
                if np.random.rand() < 0.5:
                    theta = 180.0
                    rot_map = cv2.getRotationMatrix2D(tuple(self.im_center), theta, 1)
                    train_image = cv2.warpAffine(train_image, rot_map, (self.im_height, self.im_width), flags=cv2.INTER_NEAREST)

                    if self.gripper_mode == GripperMode.LEGACY_SUCTION:
                        pose_arr[:,3] = -pose_arr[:,3]
                    elif self.gripper_mode == GripperMode.SUCTION:
                        pose_arr[:,4] = -pose_arr[:,4]
                # reflect left right with 50% probability
                if np.random.rand() < 0.5:
                    train_image = np.fliplr(train_image)
                # reflect up down with 50% probability
                if np.random.rand() < 0.5:
                    train_image = np.flipud(train_image)

                    if self.gripper_mode == GripperMode.LEGACY_SUCTION:
                        pose_arr[:,3] = -pose_arr[:,3]
                    elif self.gripper_mode == GripperMode.SUCTION:
                        pose_arr[:,4] = -pose_arr[:,4]
                image_arr[i,:,:,0] = train_image
        return image_arr, pose_arr

    def _error_rate_in_batches(self, num_files_eval=None, validation_set=True):
        """ Compute error and loss over either training or validation set

        Returns
        -------
        :obj:'autolab_core.BinaryClassificationResult`
            validation error
        """
        all_predictions = []
        all_labels = []

        # subsample files
        file_indices = np.arange(self.num_tensors)
        if num_files_eval is None:
            num_files_eval = self.max_files_eval
        np.random.shuffle(file_indices)
        if self.max_files_eval is not None and num_files_eval > 0:
            file_indices = file_indices[:num_files_eval]

        for i in file_indices:
            # load next file
            images = self.dataset.tensor(self.im_field_name, i).arr
            poses = self.dataset.tensor(self.pose_field_name, i).arr
            raw_poses = np.array(poses, copy=True)
            labels = self.dataset.tensor(self.label_field_name, i).arr

            # if no datapoints from this file are in validation then just continue
            if validation_set:
                indices = self.val_index_map[i]
            else:
                indices = self.train_index_map[i]                    
            if len(indices) == 0:
                continue

            images = images[indices,...]
            poses = read_pose_data(poses[indices,:],
                                   self.gripper_mode)
            raw_poses = raw_poses[indices, :]
            labels = labels[indices]

            if self.training_mode == TrainingMode.CLASSIFICATION:
                labels = 1 * (labels > self.metric_thresh)
                labels = labels.astype(np.uint8)

            if self._angular_bins > 0:
                # form mask to extract predictions from ground-truth angular bins
                angles = raw_poses[:, 3]
                neg_ind = np.where(angles < 0)
                angles = np.abs(angles) % GeneralConstants.PI
                angles[neg_ind] *= -1
                g_90 = np.where(angles > (GeneralConstants.PI / 2))
                l_neg_90 = np.where(angles < (-1 * (GeneralConstants.PI / 2)))
                angles[g_90] -= GeneralConstants.PI
                angles[l_neg_90] += GeneralConstants.PI
                angles *= -1 # hack to fix reverse angle convention
                angles += (GeneralConstants.PI / 2)
                pred_mask = np.zeros((labels.shape[0], self._angular_bins*2), dtype=bool)
                for i in range(angles.shape[0]):
                    pred_mask[i, int((angles[i] // self._bin_width)*2)] = True
                    pred_mask[i, int((angles[i] // self._bin_width)*2 + 1)] = True
                
            # get predictions
            predictions = self.gqcnn.predict(images, poses)

            if self._angular_bins > 0:
                predictions = predictions[pred_mask].reshape((-1, 2))            

            # update
            all_predictions.extend(predictions[:,1].tolist())
            all_labels.extend(labels.tolist())
                            
            # clean up
            del images
            del poses

        # get learning result
        result = None
        if self.training_mode == TrainingMode.CLASSIFICATION:
            result = BinaryClassificationResult(all_predictions, all_labels)
        else:
            result = RegressionResult(all_predictions, all_labels)
        return result
