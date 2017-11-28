from encoder import Encoder
from Layers.dpp import DPP
from Layers.input_layer import InputLayer
from Model.FLIC.flic import FLIC
from Util.data_helper import store, load
from Layers.deep_sets import PermEquivLayer
from adaptive_learning_rate import adaptive_learning_rate
from Util.data_io import training_sample, get_strings_from_samples
from Util.nn.basic import EmbeddingLayer, apply_dropout
from Util.nn.initialization import tanh, linear, ReLU, sigmoid
from Util.nn import create_optimization_updates
from Util.user_signals import GracefullExit
from Util.nn.basic import Layer
from Util.nn.basic import LSTM
import numpy as np
import os
import sys
import theano.tensor as T
import theano
from theano.compile.nanguardmode import NanGuardMode
from timeit import default_timer as timer

class Model:
    def __init__(self, args, sent_emb_dim, flic_dim, load_model=None, epochs_done=0):
        """Initializes the model and constructs the Theano Computation Graph."""
        self.args = args
        self.sig_handler = GracefullExit()
        self.best_val_error = sys.float_info.max

        if self.args.sample_all_sentences:
            print("We sample all items from the generator in each iteration.")
        else:
            print("We sample {} sets from the generator in each iteration.".format(args.num_samples))

        self.sent_emb_dim = sent_emb_dim


        #---------------------------------------------------------------------------------------------------------------
        # Construct computation graph
        #---------------------------------------------------------------------------------------------------------------
        print("Constructing computation graph.")

        # (Input) Tensors
        sent_embs_context_t = T.matrix('sent_embs_context', dtype=theano.config.floatX)
        sent_embs_target_t = T.matrix('sent_embs_target_2', dtype=theano.config.floatX)
        y_t = T.scalar('y', dtype=theano.config.floatX)

        # Product embeddings
        self.num_emb_layers = self.args.gen_num_hidden_layers
        emb_layers = []
        # Deep Set version
        print("Deep Set version")
        for i in range(self.num_emb_layers):
            if i == 0:
                emb_layers.append(PermEquivLayer(self.sent_emb_dim, self.args.gen_num_hidden_units, activation=sigmoid, has_bias=False))
            else:
                emb_layers.append(PermEquivLayer(self.args.gen_num_hidden_units, self.args.gen_num_hidden_units, activation=sigmoid, has_bias=False))
        # Apply permutations-equivarient layers and a max-pooling over all sentences in the reviews.
        context_emb = sent_embs_context_t
        target_emb = sent_embs_target_t
        for layer in emb_layers:
            context_emb = layer.forward(context_emb.dimshuffle('x', 0, 1)).dimshuffle(1, 2)
            target_emb = layer.forward(target_emb.dimshuffle('x', 0, 1)).dimshuffle(1, 2)
        prod_embs_context_t = T.max(context_emb, axis=0)
        prod_embs_target_t = T.max(target_emb, axis=0)


        if args.model_type == 'Logreg_SentEmbs_1_layer':
            print("Logreg SentEmbs with 1 layer")
            # Neural network with 1 layer and logistic regression output.
            feature_vector_t = T.concatenate([prod_embs_context_t, prod_embs_target_t])
            linear_layer_t = Layer(n_in=2*self.args.gen_num_hidden_units, n_out=1, activation=linear)
            scalar_output_t = linear_layer_t.forward(feature_vector_t)[0]
            self.layers = emb_layers + [linear_layer_t]
            prob_t = 1.0 / (1.0 + T.exp(-scalar_output_t))

        else:
            print("Logreg SentEmbs with 2 layers")
            # Neural network with 2 layers and logistic regression output.
            feature_vector_t = T.concatenate([prod_embs_context_t, prod_embs_target_t])
            ff_layer_t = Layer(n_in=2*self.args.gen_num_hidden_units, n_out=self.args.enc_num_hidden_units, activation=tanh)
            linear_layer_t = Layer(n_in=self.args.enc_num_hidden_units, n_out=1, activation=linear)
            hidden_state_t = ff_layer_t.forward(feature_vector_t)
            scalar_output_t = linear_layer_t.forward(hidden_state_t)[0]
            self.layers = emb_layers + [ff_layer_t, linear_layer_t]
            prob_t = 1.0/(1.0 + T.exp(-scalar_output_t))

        # Cross entropy error
        cost_t = -(y_t * T.log(prob_t) + (1 - y_t) * T.log(1 - prob_t))


        # Updates model parameters
        self.params = [param for layer in self.layers for param in layer.params]
        updates, self.lr, gnorm, self.params_opt = create_optimization_updates(
            cost=cost_t,
            params=self.params,
            method=self.args.learning,
            lr=self.args.learning_rate_encoder
        )[:4]

        if self.args.adaptive_lrs:
            self.adaptive_learning_rate = adaptive_learning_rate(lr_1=self.lr)
        else:
            self.adaptive_learning_rate = adaptive_learning_rate()

        # Compile training graph
        self.train_model_t = theano.function(inputs=[sent_embs_context_t, sent_embs_target_t, y_t],
                                             outputs=[prob_t, cost_t],
                                             updates=updates.items(),
                                             allow_input_downcast=True,
                                             # mode=NanGuardMode(nan_is_error=True, inf_is_error=True, big_is_error=False),
                                             # mode='DebugMode',
                                             on_unused_input='warn'
                                             )

        # Compile graph for validation data
        self.validate_t = theano.function(inputs=[sent_embs_context_t, sent_embs_target_t, y_t],
                                          outputs=[prob_t, cost_t],
                                          updates=[],
                                          allow_input_downcast=True,
                                          on_unused_input='warn'
                                          )
        # Load pretrained model
        if load_model:
            self.load(load_model)
        elif self.args.load_model:
            self.load(args.load_model)
        self.epochs_done = epochs_done


    def validation_cost(self, D_valid, D_C_valid, prod_to_sent, flic, num_samples = 10):
        probs_pos = 0
        probs_neg = 0
        probs_pos_count = 0
        probs_neg_count = 0
        tot_cost = 0
        num_errors = 0.0

        for context, x, y in training_sample(D_valid, D_C_valid):

            sent_embs_context = prod_to_sent[context][0]
            sent_embs_target = prod_to_sent[x][0]

            mean_prob, mean_cost = self.validate_t(sent_embs_context, sent_embs_target, y)

            if abs(mean_prob - y) > 0.5:
                num_errors += 1

            tot_cost +=  mean_cost
            if y == 1:
                probs_pos += mean_prob
                probs_pos_count += 1
            else:
                probs_neg += mean_prob
                probs_neg_count += 1

        count = probs_pos_count + probs_neg_count

        if probs_pos_count > 0 and probs_neg_count > 0:
            return 1 - num_errors / count, tot_cost / count, probs_pos / probs_pos_count, probs_neg / probs_neg_count
        else:
            return 0, 0, 0, 0

    def testset_evaluation(self, D_test, D_C_test, prod_to_sent, flic, num_samples = 20, random_sampling=False):

        probs_pos = 0
        probs_neg = 0
        probs_pos_count = 0
        probs_neg_count = 0
        tot_cost = 0
        sampled_sentences = []
        costs = []
        num_errors = 0.0

        for context, x, y in training_sample(D_test, D_C_test):
            sent_embs_context = prod_to_sent[context][0]
            sent_embs_target = prod_to_sent[x][0]

            mean_prob, mean_cost = self.validate_t(sent_embs_context, sent_embs_target, y)
            costs.append(mean_cost)

            tot_cost +=  mean_cost
            if y == 1:
                probs_pos += mean_prob
                probs_pos_count += 1
            else:
                probs_neg += mean_prob
                probs_neg_count += 1

            if abs(mean_prob - y) > 0.5:
                num_errors += 1

        return sampled_sentences, 1 - num_errors / (probs_pos_count + probs_neg_count),  \
               (tot_cost / (probs_pos_count + probs_neg_count), probs_pos / probs_pos_count, probs_neg / probs_neg_count)

    def _print_scores(self, valid_scores, train_scores):

        if train_scores:
            train_cost, train_pos_score, train_neg_score = train_scores
            print("Average cost on the training set: {}".format(train_cost))
            print("Average probabilities of true links: {}".format(train_pos_score))
            print("Average probabilities of false links: {}".format(train_neg_score))

        if valid_scores:
            valid_cost, valid_pos_score, valid_neg_score = valid_scores
            print("Average cost on the validation set: {}".format(valid_cost))
            print("Average probabilities of true links: {}".format(valid_pos_score))
            print("Average probabilities of false links: {}".format(valid_neg_score))

    def train(self, D, D_C, D_valid, D_C_valid, flic, prod_to_sent):

        #---------------------------------------------------------------------------------------------------------------
        # Looping through the data (i.e. the 'Training-For-Loop').
        #---------------------------------------------------------------------------------------------------------------

        # For time measurements
        t_sampling = 0.0
        t_generator = 0.0
        t_encoder = 0.0
        t_gen_training = 0.0
        t_enc_training = 0.0


        print("Start training.")
        print("Num links in training set = {}".format(len(D) + len(D_C)))
        for epoch in range(self.args.max_epochs):
            epoch += self.epochs_done
            iteration = 0
            cost_epoch = 0
            cost_tmp = 0
            t_training = 0.0
            probs_pos = 0; probs_neg = 0; probs_pos_count = 0; probs_neg_count = 0
            probs_pos_tmp = 0; probs_neg_tmp = 0; probs_pos_count_tmp = 0; probs_neg_count_tmp = 0

            for context, x, y in training_sample(D, D_C):

                #-------------------------------------------------------------------------------------------------------
                # Train model
                #-------------------------------------------------------------------------------------------------------
                sent_embs_context = prod_to_sent[context][0]
                sent_embs_product = prod_to_sent[x][0]

                t_0 = timer()
                mean_prob, mean_cost = self.train_model_t(sent_embs_context, sent_embs_product, y)
                t_training += (timer() - t_0)

                #-------------------------------------------------------------------------------------------------------
                # Output of the training and validation scores (only sometimes)
                #-------------------------------------------------------------------------------------------------------

                cost_epoch += mean_cost
                cost_tmp += mean_cost
                if y == 1:
                    probs_pos += mean_prob
                    probs_pos_tmp += mean_prob
                    probs_pos_count += 1
                    probs_pos_count_tmp += 1
                else:
                    probs_neg += mean_prob
                    probs_neg_tmp += mean_prob
                    probs_neg_count += 1
                    probs_neg_count_tmp += 1

                iteration += 1

                # All 1000 datapoints
                if iteration % 1000 == 0:
                    print("Cost on the training set after {} iterations: {}".format(iteration, cost_tmp /
                                                                            (probs_pos_count_tmp + probs_neg_count_tmp)))

                if iteration % 10000 == 0 and self.args.save_model:
                    self.store(self.args.save_model, epoch, iteration)
                    print("Model saved. Epoch = {}, Iteration = {}".format(epoch, iteration))

                if self.sig_handler.exit == True:
                    if self.best_val_error == sys.float_info.max and self.args.save_model:
                        self.best_val_error = -1
                        self.store(self.args.save_model + '/best_valerr_' + str("{0:3f}.pkl".format(self.best_val_error)))
                    return (self.best_val_error, "SIGNAL")


            # ----------------------------------------------------------------------------------------------------------
            # Output per epoch
            # ----------------------------------------------------------------------------------------------------------

            if self.args.save_model:
                self.store(self.args.save_model, epoch, 0)
                print("\nEpoch {} done. Model saved! ".format(epoch))
            else:
                print("\nEpoch {} done.".format(epoch))

            # Timing output
            if self.args.measure_timing:
                print("Time Training = {}".format(t_training / iteration))

            print("Calculating the average cost in this epoch.")
            if D_valid is not None and D_C_valid is not None:
                accuracy, valid_cost, valid_pos_score, valid_neg_score = self.validation_cost(D_valid, D_C_valid,
                                                                                              prod_to_sent, flic)
                self._print_scores((valid_cost, valid_pos_score, valid_neg_score),
                                   (cost_epoch / (probs_pos_count + probs_neg_count),
                                    probs_pos / probs_pos_count, probs_neg / probs_neg_count))
                if valid_cost < self.best_val_error and self.args.save_model:
                    self.best_val_error = min(self.best_val_error, valid_cost)
                    self.store(self.args.save_model + '/best_valerr_' + str("{0:3f}.pkl".format(valid_cost)))

                # Decreasing the learning rate and reloading the best model if the validation cost
                # is not decreasing anymore.
                if self.args.adaptive_lrs:
                    lrs_adapted = self.adaptive_learning_rate.adapt_learning_rate(valid_cost, self.lr)
                if self.args.save_model and self.args.adaptive_lrs and lrs_adapted:
                    lr_value = self.lr.get_value()
                    self.load(
                        self.args.save_model + '/best_valerr_' + str("{0:3f}.pkl".format(self.best_val_error)))
                    self.lr.set_value(lr_value)
                    print("Best model loaded.")

                # If the learning rates are very small, we stop our training.
                if self.lr.get_value() < 1e-5:
                    return (self.best_val_error, "END")
            else:
                self._print_scores(None, (cost_epoch / (probs_pos_count + probs_neg_count),
                                          probs_pos / probs_pos_count,
                                          probs_neg / probs_neg_count))
            print("")

            # Resets all temporary variables.
            cost_epoch = 0
            if epoch + 1 >= self.args.max_epochs:
                return (self.best_val_error, "END")
            if (epoch + 1) % 10 == 0:
                self.epochs_done += 10
                self.store(self.args.save_model + '/after_10_epochs_' + str("{0:3f}.pkl".format(valid_cost)))
                return (valid_cost, "10_epochs")
            probs_neg = 0
            probs_pos = 0
            probs_pos_count = 0
            probs_neg_count = 0
        return (self.best_val_error, "END")


    def store(self, path, epoch = None, iter = None):
        """ Stores the model with the filename 'path'.

        :type path: String
        :param path: The filename under which the model should be saved. If 'epoch' is not None and 'iter' is not None,
        then the model will be saved in the folder given by 'path' and the filename will be constructed from 'epoch' and 'iter'.

        """
        if epoch is not None and iter is not None:
            path = path + '/wedim_' + str(self.sent_emb_dim) + '_fdim_' + \
                   '_epoch_' + str(epoch) + '_iter_' + str(iter) + '.pkl'
        obj = [(self.sent_emb_dim, self.args.gen_num_hidden_layers, self.args.gen_num_hidden_units,
                self.args.enc_num_hidden_layers, self.args.enc_num_hidden_units)]
        obj.append(self.params)
        obj.append(self.params_opt)
        obj.append(self.adaptive_learning_rate.val_error)
        obj.append(self.best_val_error)
        store(obj, path)


        store(obj, path)

    def load(self, file):
        print("Loading trained model from file.")
        params = load(file)

        # Test if the parameters have the right format
        sent_emb_dim, gen_num_layers, gen_num_units, enc_num_layers, enc_num_units = params[0]
        if sent_emb_dim != self.sent_emb_dim or gen_num_layers != self.args.gen_num_hidden_layers or \
            gen_num_units != self.args.gen_num_hidden_units or enc_num_layers != self.args.enc_num_hidden_layers or \
            enc_num_units != self.args.enc_num_hidden_units:
            raise(ValueError, "The dimension of the loaded model are not consistent "
                              "with the dimensions of the command line arguments.")

        start = 0
        for layer in self.layers:
            end = start + len(layer.params)
            layer.params = params[1][start:end]
            start = end

        params_opt = params[2]
        for model_param, stored_param in zip(self.params_opt, params_opt):
            model_param.set_value(stored_param.get_value())

        self.adaptive_learning_rate.val_error = params[3]
        self.best_val_error = params[4]
