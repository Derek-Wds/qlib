# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.


from __future__ import division
from __future__ import print_function

import os
import numpy as np
import pandas as pd
import copy
from sklearn.metrics import roc_auc_score, mean_squared_error
import logging
from ...utils import (
    unpack_archive_with_buffer,
    save_multiple_parts_file,
    create_save_path,
    drop_nan_by_y_index,
)
from ...log import get_module_logger, TimeInspector

import torch
import torch.nn as nn
import torch.optim as optim

from ...model.base import Model
from ...data.dataset import DatasetH
from ...data.dataset.handler import DataHandlerLP


class TCTS(Model):
    """TCTS Model

    Parameters
    ----------
    d_feat : int
        input dimension for each time step
    metric: str
        the evaluate metric used in early stop
    optimizer : str
        optimizer name
    GPU : str
        the GPU ID(s) used for training
    """

    def __init__(
        self,
        d_feat=6,
        hidden_size=64,
        num_layers=2,
        dropout=0.0,
        n_epochs=200,
        batch_size=2000,
        early_stop=20,
        loss="mse",
        fore_optimizer="adam",
        weight_optimizer="adam",
        output_dim=5,
        fore_lr=5e-7,
        weight_lr=5e-7,
        steps=3,
        GPU=0,
        seed=None,
        target_label=0,
        **kwargs
    ):
        # Set logger.
        self.logger = get_module_logger("TCTS")
        self.logger.info("TCTS pytorch version...")

        # set hyper-parameters.
        self.d_feat = d_feat
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.dropout = dropout
        self.n_epochs = n_epochs
        self.batch_size = batch_size
        self.early_stop = early_stop
        self.loss = loss
        self.device = torch.device("cuda:%d" % (GPU) if torch.cuda.is_available() else "cpu")
        self.use_gpu = torch.cuda.is_available()
        self.seed = seed
        self.output_dim = output_dim
        self.fore_lr = fore_lr
        self.weight_lr = weight_lr
        self.steps = steps
        self.target_label = target_label

        self.logger.info(
            "TCTS parameters setting:"
            "\nd_feat : {}"
            "\nhidden_size : {}"
            "\nnum_layers : {}"
            "\ndropout : {}"
            "\nn_epochs : {}"
            "\nbatch_size : {}"
            "\nearly_stop : {}"
            "\nloss_type : {}"
            "\nvisible_GPU : {}"
            "\nuse_GPU : {}"
            "\nseed : {}".format(
                d_feat,
                hidden_size,
                num_layers,
                dropout,
                n_epochs,
                batch_size,
                early_stop,
                loss,
                GPU,
                self.use_gpu,
                seed,
            )
        )

        if self.seed is not None:
            np.random.seed(self.seed)
            torch.manual_seed(self.seed)

        self.fore_model = GRUModel(
            d_feat=self.d_feat,
            hidden_size=self.hidden_size,
            num_layers=self.num_layers,
            dropout=self.dropout,
        )
        self.weight_model = MLPModel(
            d_feat=360 + 2 * self.output_dim + 1,
            hidden_size=self.hidden_size,
            num_layers=self.num_layers,
            dropout=self.dropout,
            output_dim=self.output_dim,
        )
        if fore_optimizer.lower() == "adam":
            self.fore_optimizer = optim.Adam(self.fore_model.parameters(), lr=self.fore_lr)
        elif fore_optimizer.lower() == "gd":
            self.fore_optimizer = optim.SGD(self.fore_model.parameters(), lr=self.fore_lr)
        else:
            raise NotImplementedError("optimizer {} is not supported!".format(fore_optimizer))
        if weight_optimizer.lower() == "adam":
            self.weight_optimizer = optim.Adam(self.weight_model.parameters(), lr=self.weight_lr)
        elif weight_optimizer.lower() == "gd":
            self.weight_optimizer = optim.SGD(self.weight_model.parameters(), lr=self.weight_lr)
        else:
            raise NotImplementedError("optimizer {} is not supported!".format(weight_optimizer))

        self.fitted = False
        self.fore_model.to(self.device)
        self.weight_model.to(self.device)

    def loss_fn(self, pred, label, weight):

        loc = torch.argmax(weight, 1)
        loss = (pred - label[np.arange(weight.shape[0]), loc]) ** 2
        return torch.mean(loss)

    def train_epoch(self, x_train, y_train, x_valid, y_valid):

        x_train_values = x_train.values
        y_train_values = np.squeeze(y_train.values)

        indices = np.arange(len(x_train_values))
        np.random.shuffle(indices)

        init_fore_model = copy.deepcopy(self.fore_model)
        for p in init_fore_model.parameters():
            p.init_fore_model = False

        self.fore_model.train()
        self.weight_model.train()

        for p in self.weight_model.parameters():
            p.requires_grad = False
        for p in self.fore_model.parameters():
            p.requires_grad = True

        for i in range(self.steps):
            for i in range(len(indices))[:: self.batch_size]:

                if len(indices) - i < self.batch_size:
                    break

                feature = torch.from_numpy(x_train_values[indices[i : i + self.batch_size]]).float().to(self.device)
                label = torch.from_numpy(y_train_values[indices[i : i + self.batch_size]]).float().to(self.device)

                init_pred = init_fore_model(feature)
                pred = self.fore_model(feature)

                dis = init_pred - label.transpose(0, 1)
                weight_feature = torch.cat((feature, dis.transpose(0, 1), label, init_pred.view(-1, 1)), 1)
                weight = self.weight_model(weight_feature)

                loss = self.loss_fn(pred, label, weight)  # hard

                self.fore_optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_value_(self.fore_model.parameters(), 3.0)
                self.fore_optimizer.step()

        x_valid_values = x_valid.values
        y_valid_values = np.squeeze(y_valid.values)

        indices = np.arange(len(x_valid_values))
        np.random.shuffle(indices)
        for p in self.weight_model.parameters():
            p.requires_grad = True
        for p in self.fore_model.parameters():
            p.requires_grad = False

        # fix forecasting model and valid weight model
        for i in range(len(indices))[:: self.batch_size]:

            if len(indices) - i < self.batch_size:
                break

            feature = torch.from_numpy(x_valid_values[indices[i : i + self.batch_size]]).float().to(self.device)
            label = torch.from_numpy(y_valid_values[indices[i : i + self.batch_size]]).float().to(self.device)

            pred = self.fore_model(feature)
            dis = pred - label.transpose(0, 1)
            weight_feature = torch.cat((feature, dis.transpose(0, 1), label, pred.view(-1, 1)), 1)
            weight = self.weight_model(weight_feature)
            loc = torch.argmax(weight, 1)
            valid_loss = torch.mean((pred - label[:, 0]) ** 2)
            loss = torch.mean(-valid_loss * torch.log(weight[np.arange(weight.shape[0]), loc]))

            self.weight_optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_value_(self.weight_model.parameters(), 3.0)
            self.weight_optimizer.step()

    def test_epoch(self, data_x, data_y):

        # prepare training data
        x_values = data_x.values
        y_values = np.squeeze(data_y.values)

        self.fore_model.eval()

        scores = []
        losses = []

        indices = np.arange(len(x_values))

        for i in range(len(indices))[:: self.batch_size]:

            if len(indices) - i < self.batch_size:
                break

            feature = torch.from_numpy(x_values[indices[i : i + self.batch_size]]).float().to(self.device)
            label = torch.from_numpy(y_values[indices[i : i + self.batch_size]]).float().to(self.device)

            pred = self.fore_model(feature)
            loss = torch.mean((pred - label[:, abs(self.target_label)]) ** 2)
            losses.append(loss.item())

        return np.mean(losses)

    def fit(
        self,
        dataset: DatasetH,
        evals_result=dict(),
        verbose=True,
        save_path=None,
    ):

        df_train, df_valid, df_test = dataset.prepare(
            ["train", "valid", "test"],
            col_set=["feature", "label"],
            data_key=DataHandlerLP.DK_L,
        )

        x_train, y_train = df_train["feature"], df_train["label"]
        x_valid, y_valid = df_valid["feature"], df_valid["label"]
        x_test, y_test = df_test["feature"], df_test["label"]

        if save_path == None:
            save_path = create_save_path(save_path)

        best_loss = np.inf
        best_epoch = 0
        stop_round = 0
        fore_best_param = copy.deepcopy(self.fore_optimizer.state_dict())
        weight_best_param = copy.deepcopy(self.weight_optimizer.state_dict())

        for epoch in range(self.n_epochs):
            print("Epoch:", epoch)

            print("training...")
            self.train_epoch(x_train, y_train, x_valid, y_valid)
            print("evaluating...")
            val_loss = self.test_epoch(x_valid, y_valid)
            test_loss = self.test_epoch(x_test, y_test)

            print("valid %.6f, test %.6f" % (val_loss, test_loss))

            if val_loss < best_loss:
                best_loss = val_loss
                stop_round = 0
                best_epoch = epoch
                torch.save(copy.deepcopy(self.fore_model.state_dict()), save_path + "_fore_model.bin")
                torch.save(copy.deepcopy(self.weight_model.state_dict()), save_path + "_weight_model.bin")

            else:
                stop_round += 1
                if stop_round >= self.early_stop:
                    print("early stop")
                    break

        print("best loss:", best_loss, "@", best_epoch)
        best_param = torch.load(save_path + "_fore_model.bin")
        self.fore_model.load_state_dict(best_param)
        best_param = torch.load(save_path + "_weight_model.bin")
        self.weight_model.load_state_dict(best_param)
        self.fitted = True

        if self.use_gpu:
            torch.cuda.empty_cache()

    def predict(self, dataset):
        if not self.fitted:
            raise ValueError("model is not fitted yet!")

        x_test = dataset.prepare("test", col_set="feature")
        index = x_test.index
        self.fore_model.eval()
        x_values = x_test.values
        sample_num = x_values.shape[0]
        preds = []

        for begin in range(sample_num)[:: self.batch_size]:

            if sample_num - begin < self.batch_size:
                end = sample_num
            else:
                end = begin + self.batch_size

            x_batch = torch.from_numpy(x_values[begin:end]).float().to(self.device)

            with torch.no_grad():
                if self.use_gpu:
                    pred = self.fore_model(x_batch).detach().cpu().numpy()
                else:
                    pred = self.fore_model(x_batch).detach().numpy()

            preds.append(pred)

        return pd.Series(np.concatenate(preds), index=index)


class MLPModel(nn.Module):
    def __init__(self, d_feat, hidden_size=256, num_layers=3, dropout=0.0, output_dim=1):
        super().__init__()

        self.mlp = nn.Sequential()
        self.softmax = nn.Softmax(dim=1)

        for i in range(num_layers):
            if i > 0:
                self.mlp.add_module("drop_%d" % i, nn.Dropout(dropout))
            self.mlp.add_module("fc_%d" % i, nn.Linear(d_feat if i == 0 else hidden_size, hidden_size))
            self.mlp.add_module("relu_%d" % i, nn.ReLU())

        self.mlp.add_module("fc_out", nn.Linear(hidden_size, output_dim))

    def forward(self, x):
        # feature
        # [N, F]
        out = self.mlp(x).squeeze()
        out = self.softmax(out)
        return out


class GRUModel(nn.Module):
    def __init__(self, d_feat=6, hidden_size=64, num_layers=2, dropout=0.0):
        super().__init__()

        self.rnn = nn.GRU(
            input_size=d_feat,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout,
        )
        self.fc_out = nn.Linear(hidden_size, 1)

        self.d_feat = d_feat

    def forward(self, x):
        # x: [N, F*T]
        x = x.reshape(len(x), self.d_feat, -1)  # [N, F, T]
        x = x.permute(0, 2, 1)  # [N, T, F]
        out, _ = self.rnn(x)
        return self.fc_out(out[:, -1, :]).squeeze()
