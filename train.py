from torch.utils.tensorboard import SummaryWriter
from torch.nn import DataParallel
from tqdm import tqdm
from model.resnet import ResNet
from utils.model_loader import save_state, load_state
from utils.utils import separate_bn_param
from torch import optim
from torch.optim.lr_scheduler import ReduceLROnPlateau
from data.data_loader import CustomDataLoader
from model.gender_head import GenderHead
from model.age_head import AgeHead
from model.race_head import RaceHead
import torch
from os import path
import numpy as np
from sklearn.metrics import mean_absolute_error
from optimizer.early_stop import EarlyStop
from torch.nn.functional import log_softmax, kl_div


class Train():
    def __init__(self, config):
        self.config = config
        print(self.config)
        self.save_file(self.config, 'config.txt')

        ATTR_HEAD = {'race': RaceHead, 'gender': GenderHead,
                     'age': AgeHead, 'recognition': self.config.recognition_head}

        self.writer = SummaryWriter(config.log_path)

        self.model = ResNet(self.config.depth, self.config.drop_ratio, self.config.net_mode)
        self.head = self.ATTR_HEAD[self.config.attribute]()

        paras_only_bn, paras_wo_bn = separate_bn_param(self.model)

        dummy_input = torch.zeros(1, 3, 112, 112)
        self.writer.add_graph(self.model, dummy_input)

        if torch.cuda.device_count() > 1:
            print(f"Model will use {torch.cuda.device_count()} GPUs!")
            self.model = DataParallel(self.model)
            self.head = DataParallel(self.head)

        self.model = self.model.to(self.config.device)
        self.head = self.head.to(self.config.device)

        self.train_loader = CustomDataLoader(self.config, self.config.train_source, self.config.train_list)

        self.weights = None
        if self.config.attribute in ['race', 'gender']:
            _, self.weights = np.unique(self.train_loader.dataset.targets, return_counts=True)
            self.weights = np.max(self.weights) / self.weights
            self.weights = torch.tensor(self.weights, dtype=torch.float, device=self.config.device)
            print(self.weights)

        if self.config.attribute != 'recognition':
            self.val_loader = CustomDataLoader(self.config, self.config.val_source,
                                               self.config.val_list, False, False, False)

        elif self.config.attribute == 'recognition':
            self.agedb_30, self.agedb_30_issame = get_val_pair(self.config.val_source, 'agedb_30')
            self.cfp_fp, self.cfp_fp_issame = get_val_pair(self.config.val_source, 'cfp_fp')
            self.lfw, self.lfw_issame = get_val_pair(self.config.val_source, 'lfw')

        self.optimizer = optim.SGD([{'params': paras_wo_bn,
                                     'weight_decay': self.config.weight_decay},
                                    {'params': self.head.parameters(),
                                     'weight_decay': self.config.weight_decay},
                                    {'params': paras_only_bn}],
                                   lr=self.config.lr, momentum=self.config.momentum)

        if self.config.resume:
            print(f'Resuming training from {self.config.resume}')
            load_state(self.model, self.head, self.optimizer, self.config.resume, False)

        if self.config.pretrained:
            print(f'Loading pretrained weights from {self.config.pretrained}')
            load_state(self.model, self.head, None, self.config.pretrained, True)

        print(self.optimizer)
        self.save_file(self.optimizer, 'optimizer.txt')

        self.tensorboard_loss_every = max(len(self.train_loader) // 100, 1)
        self.evaluate_every = max(len(self.train_loader) // 5, 1)
        self.save_every = max(len(self.train_loader) // 5, 1)

        if self.config.lr_plateau:
            self.scheduler = ReduceLROnPlateau(self.optimizer, mode=self.config.max_or_min, factor=0.1,
                                               patience=3, verbose=True, threshold=0.001, cooldown=1)

        self.early_stop = EarlyStop(mode=self.config.max_or_min)

    def run(self):
        self.model.train()
        self.head.train()
        running_loss = 0.
        step = 0
        val_acc = 0.
        val_loss = 0.

        best_step = 0
        best_acc = float('Inf')
        if self.config.max_or_min == 'max':
            best_acc *= -1

        for epoch in range(self.config.epochs):
            loop = tqdm(iter(self.train_loader))
            for imgs, labels in loop:
                imgs = imgs.to(self.config.device)
                labels = labels.to(self.config.device)

                self.optimizer.zero_grad()

                embeddings = self.model(imgs)
                outputs = self.head(embeddings)

                if self.weights is not None:
                    loss = self.config.loss(outputs, labels, weight=self.weights)
                else:
                    loss = self.config.loss(outputs, labels)

                loss.backward()
                running_loss += loss.item()

                self.optimizer.step()

                if step % self.tensorboard_loss_every == 0:
                    loss_board = running_loss / self.tensorboard_loss_every
                    self.writer.add_scalar('train_loss', loss_board, step)
                    running_loss = 0.

                if step % self.evaluate_every == 0 and step != 0:
                    val_acc, val_loss = self.evaluate(step)
                    self.model.train()
                    self.head.train()
                    best_acc, best_step = self.save_model(val_acc, best_acc, step, best_step)

                step += 1
                loop.set_description('Epoch {}/{}'.format(epoch + 1, self.config.epochs))
                loop.set_postfix(loss=loss.item(), val_acc=val_acc, val_loss=val_loss)

            if epoch in self.config.reduce_lr and not self.config.lr_plateau:
                self.reduce_lr()
            else:
                self.scheduler.step(val_acc)

            self.early_stop(val_acc)
            if self.early_stop.stop:
                print("Early stopping model...")
                break

        val_acc, val_loss = self.evaluate(step)
        best_acc = self.save_model(val_acc, best_acc, step, best_step)
        print(f'Best accuracy: {best_acc} at step {best_step}')

    def save_model(self, val_acc, best_acc, step, best_step):
        if (self.config.max_or_min == 'max' and val_acc > best_acc) or \
           (self.config.max_or_min == 'min' and val_acc < best_acc):
            best_acc = val_acc
            best_step = step
            save_state(self.model, self.head, self.optimizer, self.config, val_acc, step)

        return best_acc, best_step

    def reduce_lr(self):
        for params in self.optimizer.param_groups:
            params['lr'] /= 10

        print(self.optimizer)

    def tensorboard_val(self, accuracy, step, loss=0, dataset=''):
        self.writer.add_scalar('{}val_acc'.format(dataset), accuracy, step)

        if self.config.attribute != 'recognition':
            self.writer.add_scalar('val_loss', loss, step)

    def evaluate(self, step):
        if self.config.attribute != 'recognition':
            val_acc, val_loss = self.evaluate_attribute()
            self.tensorboard_val(val_acc, step, val_loss)

        elif self.config.attribute == 'recognition':
            # need to finish this
            agedb_30_accuracy = self.evaluate_recognition(self.agedb_30, self.agedb_30_issame)
            self.tensorboard_val(accuracy, step, dataset='agedb_30_')
            lfw_accuracy = self.evaluate_recognition(self.lfw, self.lfw_issame)
            self.tensorboard_val(accuracy, step, dataset='lfw_')
            cfp_fp_accuracy = self.evaluate_recognition(self.cfp_fp, self.cfp_fp_issame)
            self.tensorboard_val(accuracy, step, dataset='cfp_fp_')

            val_acc = (agedb_30_accuracy + lfw_accuracy + cfp_fp_accuracy) / 3
            self.tensorboard_val(accuracy, step)

            return self.evaluate_recognition()

        return val_acc, val_loss

    def evaluate_attribute(self):
        self.model.eval()
        self.head.eval()

        y_true = torch.tensor([], dtype=self.config.output_type, device=self.config.device)
        all_outputs = torch.tensor([], device=self.config.device)

        with torch.no_grad():
            for imgs, labels in iter(self.val_loader):
                imgs = imgs.to(self.config.device)
                labels = labels.to(self.config.device)

                embeddings = self.model(imgs)
                outputs = self.head(embeddings)

                y_true = torch.cat((y_true, labels), 0)
                all_outputs = torch.cat((all_outputs, outputs), 0)

            if self.weights is not None:
                loss = round(self.config.loss(outputs, labels, weight=self.weights).item(), 4)
            else:
                loss = round(self.config.loss(outputs, labels).item(), 4)

        y_true = y_true.cpu().numpy()

        if self.config.attribute == 'age':
            y_pred = all_outputs.cpu().numpy()
            y_pred = np.round(y_pred, 0)
            y_pred = np.sum(y_pred, axis=1)
            y_true = np.sum(y_true, axis=1)
            accuracy = round(mean_absolute_error(y_true, y_pred), 4)
        else:
            _, y_pred = torch.max(all_outputs, 1)
            y_pred = y_pred.cpu().numpy()

            accuracy = round(np.sum(y_true == y_pred) / len(y_pred), 4)

        return accuracy, loss

    def evaluate_recognition(self, samples, issame, nrof_folds=10, tta=False):
        self.model.eval()
        idx = 0
        embeddings = np.zeros([len(samples), self.config.embedding_size])

        with torch.no_grad():
            while idx + self.config.batch_size <= len(samples):
                batch = torch.tensor(samples[idx:idx + self.config.batch_size])
                embeddings[idx:idx + self.config.batch_size] = self.model(batch.to(self.config.device)).cpu()
                idx += self.config.batch_size

            if idx < len(samples):
                batch = torch.tensor(samples[idx:])
                embeddings[idx:] = self.model(batch.to(self.config.device)).cpu()

        tpr, fpr, accuracy, best_thresholds = evaluate(embeddings, issame, nrof_folds)

        return accuracy.mean(), 0

    def save_file(self, string, file_name):
        file = open(path.join(self.config.work_path, file_name), "w")
        file.write(str(string))
        file.close()
