import os
from collections import Counter
from pathlib import Path

import torch
import torchvision
from typing import Tuple, Optional, Callable, Any, List, Dict, Iterable, Union
from data_utils import GenerateBackground, VOCDistancingImageLoader
from torch_datasets import VOCImageFolder
import numpy as np
from torch.utils.data import DataLoader, random_split
import json
from models import models


class RunModel:
    def __init__(self,
                 train_root_path: str,
                 val_root_path: str,
                 test_root_path: str,
                 dataset_name: str,
                 target_distances: List[float],
                 training_mode: str = 'stb',
                 n_distances: int = 5,
                 background: Callable = GenerateBackground(bg_type='fft'),
                 size: Tuple[int, int] = (150, 150),
                 cls_to_use: List[str] = None,
                 model_name: str = 'alexnet',
                 epochs: int = 200,
                 resize_method: str = 'long',
                 batch_size: int = 128,
                 val_size: float = 1,
                 val_target: str = 'avg',
                 num_workers: int = 4,
                 n_folds: int = None,
                 device: str = "cuda:0" if torch.cuda.is_available() else "cpu",
                 random_seed: int = 40,
                 result_dirpath: str = Path(__file__).parent / "datasets/results",
                 save_checkpoints: bool = True,
                 verbose: int = 1):
        """

        :param train_root_path: root path for training data.
        :param test_root_path: root path for testing data
        :param dataset_name: name of the dataset.
            VOC:
                Expect train_root_path folder structure:
                    train_root_path/
                    |-- root/
                    |   |-- class1
                    |   |   |-- img1.jpg
                    |   |-- class2
                    |   |   |-- img2.jpg
                    |-- Annotation/
                    |   |-- class1
                    |   |   |-- img1.xml
                    |   |-- class2
                    |   |   |-- img2.xml
        :param target_distances:
                                list: a list of target distances, ordered by training_mode
                                float: single target distances
        :param training_mode: for each distance specified in the target_distances list:
                                stb:
                                bts:
                                llo (leave last out):
                                random: all random, specified by target_distances
                                single: single distances -- equivalent to n_distances == 0
        :param n_distances: if not None/greater than 0, will use n different ratios before the target ratios, order specified by training_mode
                            if 0: single distance
                            if None: use distance ratios specified in target_distances
        :param background: callable
        :param result_dirpath: path to directory for saving model_name results. If None, model_name will not be saved
        """
        self.training_root_path = train_root_path
        self.val_root_path = val_root_path
        self.test_root_path = test_root_path
        self.dataset_name = dataset_name
        self.target_distances = target_distances
        self.training_mode = training_mode
        self.n_distances = n_distances
        self.background = background
        self.size = size
        self.cls_to_use = cls_to_use
        self.batch_size = batch_size
        self.val_size = val_size
        self.epochs = epochs
        self.resize_method = resize_method
        self.n_folds = n_folds
        self.val_target = val_target,
        self.num_workers = num_workers
        self.model_name = model_name
        self.device = device
        self.random_seed = random_seed
        self.result_dirpath = result_dirpath
        self.save_checkpoints = save_checkpoints
        self.verbose = verbose

        print(' ------ Pipeline with following parameters ------')
        for key, value in {k: v for k, v in self.__dict__.items() if not k.startswith('__')}.items():
            print(key, ": ", value)
        self.train_datasets = None
        self.test_datasets = None
        self.val_datasets = None
        self.num_classes = None
        self.all_training_loss = {}
        self.all_val_loss = {}
        self.all_val_acc_top1 = {}
        self.test_acc_top1 = {}
        self.models_statedict = []

    def combine_datasets(self, distance: int,
                         train_annotation_root_path: str,
                         val_annotation_root_path: str,
                         train_image_root_path: str,
                         val_image_root_path: str,
                         transform: torchvision.transforms.Compose):

        train_loader = VOCDistancingImageLoader(self.size, p=distance,
                                                background_generator=self.background,
                                                annotation_root_path=train_annotation_root_path)
        train_dataset = VOCImageFolder(cls_to_use=self.cls_to_use, root=train_image_root_path,
                                       transform=transform, loader=train_loader)
        val_loader = VOCDistancingImageLoader(self.size, p=distance,
                                              background_generator=self.background,
                                              annotation_root_path=val_annotation_root_path)
        val_dataset = VOCImageFolder(cls_to_use=self.cls_to_use, root=val_image_root_path,
                                     transform=transform, loader=val_loader)

        dataset = torch.utils.data.ConcatDataset([train_dataset, val_dataset])
        return dataset

    def load_datasets(self):

        test_annotation_root_path = os.path.join(self.test_root_path, 'annotations')
        train_annotation_root_path = os.path.join(self.training_root_path, 'annotations')
        val_annotation_root_path = os.path.join(self.val_root_path, 'annotations')
        train_image_root_path = os.path.join(self.training_root_path, 'root')
        val_image_root_path = os.path.join(self.val_root_path, 'root')
        test_image_root_path = os.path.join(self.test_root_path, 'root')

        training_len = len(VOCImageFolder(cls_to_use=self.cls_to_use, root=train_image_root_path))
        # testing_len = len(VOCImageFolder(cls_to_use=self.cls_to_use, root=test_image_root_path))
        val_len = len(VOCImageFolder(cls_to_use=self.cls_to_use, root=val_image_root_path))

        val_size = 1 / self.n_folds
        num_train = training_len + val_len
        cv_indices = list(range(num_train))
        np.random.seed(self.random_seed)
        np.random.shuffle(cv_indices)
        split = int(np.floor(val_size * num_train))

        transform = torchvision.transforms.Compose([
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Normalize(
                mean=[0.485, 0.456, 0.406],
                std=[0.229, 0.224, 0.225]
            )
        ])

        if self.dataset_name == 'VOC':
            for target_distances in self.target_distances:
                test_loader = VOCDistancingImageLoader(self.size, p=target_distances,
                                                       background_generator=self.background,
                                                       annotation_root_path=test_annotation_root_path)
                test_dataset = VOCImageFolder(cls_to_use=self.cls_to_use, root=test_image_root_path,
                                              transform=transform,
                                              loader=test_loader)
                self.test_datasets.append(
                    (str(target_distances), test_dataset))

        self.num_classes = len(self.test_datasets[0][1].classes)
        for fold in range(self.n_folds):

            split1 = int(np.floor(fold * split))
            split2 = int(np.floor((fold + 1) * split))
            val_idx = cv_indices[split1:split2]
            train_idx = np.append(cv_indices[:split1], cv_indices[split2:])
            # train_idx = train_idx.astype('int32')
            train_sampler = torch.utils.data.SubsetRandomSampler(train_idx)
            val_sampler = torch.utils.data.SubsetRandomSampler(val_idx)

            train_datasets, val_datasets = [], []
            if self.dataset_name == 'VOC':

                # get validation data loaders

                for target_distances in self.target_distances:
                    dataset = self.combine_datasets(distance=target_distances,
                                                    train_annotation_root_path=train_annotation_root_path,
                                                    val_annotation_root_path=val_annotation_root_path,
                                                    train_image_root_path=train_image_root_path,
                                                    val_image_root_path=val_image_root_path,
                                                    transform=transform)
                    val_dataloader = torch.utils.data.DataLoader(
                        dataset, sampler=val_sampler, batch_size=self.batch_size,
                        num_workers=self.num_workers)
                    val_datasets.append((target_distances, val_dataloader))

                if self.n_distances is None:
                    if self.training_mode == 'stb_endsame':
                        self.target_distances = sorted(self.target_distances)
                        for i in range(len(self.target_distances)):
                            train_distances_sequence = self.target_distances[i:]
                            sub_sequence = []
                            for train_distance in train_distances_sequence:
                                dataset = self.combine_datasets(distance=train_distance,
                                                                train_annotation_root_path=train_annotation_root_path,
                                                                val_annotation_root_path=val_annotation_root_path,
                                                                train_image_root_path=train_image_root_path,
                                                                val_image_root_path=val_image_root_path,
                                                                transform=transform)

                                train_dataloader = torch.utils.data.DataLoader(
                                    dataset, sampler=train_sampler, batch_size=self.batch_size,
                                    num_workers=self.num_workers)
                                sub_sequence.append((str(train_distance), train_dataloader))

                            train_datasets.append((str(train_distances_sequence), sub_sequence))
                    elif self.training_mode == 'stb_startsame':
                        self.target_distances = sorted(self.target_distances)
                        for i in range(len(self.target_distances)):
                            train_distances_sequence = self.target_distances[:i + 1]
                            sub_sequence = []
                            for train_distance in train_distances_sequence:
                                dataset = self.combine_datasets(distance=train_distance,
                                                                train_annotation_root_path=train_annotation_root_path,
                                                                val_annotation_root_path=val_annotation_root_path,
                                                                train_image_root_path=train_image_root_path,
                                                                val_image_root_path=val_image_root_path,
                                                                transform=transform)

                                train_dataloader = torch.utils.data.DataLoader(
                                    dataset, sampler=train_sampler, batch_size=self.batch_size,
                                    num_workers=self.num_workers)

                                sub_sequence.append((str(train_distance), train_dataloader))
                            train_datasets.append((str(train_distances_sequence), sub_sequence))
                    elif self.training_mode == 'bts_endsame':
                        self.target_distances = sorted(self.target_distances, reverse=True)
                        for i in range(len(self.target_distances)):
                            train_distances_sequence = self.target_distances[i:]
                            sub_sequence = []
                            for train_distance in train_distances_sequence:
                                dataset = self.combine_datasets(distance=train_distance,
                                                                train_annotation_root_path=train_annotation_root_path,
                                                                val_annotation_root_path=val_annotation_root_path,
                                                                train_image_root_path=train_image_root_path,
                                                                val_image_root_path=val_image_root_path,
                                                                transform=transform)

                                train_dataloader = torch.utils.data.DataLoader(
                                    dataset, sampler=train_sampler, batch_size=self.batch_size,
                                    num_workers=self.num_workers)
                                sub_sequence.append((str(train_distance), train_dataloader))
                            train_datasets.append((str(train_distances_sequence), sub_sequence))
                    elif self.training_mode == 'bts_startsame':
                        self.target_distances = sorted(self.target_distances, reverse=True)
                        for i in range(len(self.target_distances)):
                            train_distances_sequence = self.target_distances[:i + 1]
                            sub_sequence = []
                            for train_distance in train_distances_sequence:
                                dataset = self.combine_datasets(distance=train_distance,
                                                                train_annotation_root_path=train_annotation_root_path,
                                                                val_annotation_root_path=val_annotation_root_path,
                                                                train_image_root_path=train_image_root_path,
                                                                val_image_root_path=val_image_root_path,
                                                                transform=transform)

                                train_dataloader = torch.utils.data.DataLoader(
                                    dataset, sampler=train_sampler, batch_size=self.batch_size,
                                    num_workers=self.num_workers)
                                sub_sequence.append((str(train_distance), train_dataloader))
                            train_datasets.append((str(train_distances_sequence), sub_sequence))
                    elif self.training_mode == 'random':
                        datasets = []

                        for train_distance in self.target_distances:
                            dataset = self.combine_datasets(distance=train_distance,
                                                            train_annotation_root_path=train_annotation_root_path,
                                                            val_annotation_root_path=val_annotation_root_path,
                                                            train_image_root_path=train_image_root_path,
                                                            val_image_root_path=val_image_root_path,
                                                            transform=transform)

                            datasets.append(dataset)

                        combined_datasets = torch.utils.data.ConcatDataset(datasets)
                        indices = np.arange(len(combined_datasets))
                        np.random.seed(self.random_seed)
                        np.random.shuffle(indices)
                        indices_dataset = np.array_split(indices, len(self.target_distances))
                        train_datasets = [(str([f'random{i}' for i in range(len(indices_dataset))]),
                                           [(f'random{i}', torch.utils.data.DataLoader(
                                               torch.utils.data.Subset(combined_datasets, idx),
                                               sampler=train_sampler, batch_size=self.batch_size,
                                               num_workers=self.num_workers))
                                            for i, idx in enumerate(indices_dataset)])]

                    elif self.training_mode == 'llo':
                        for i in range(len(self.target_distances)):
                            random_distances = self.target_distances[:i] + self.target_distances[i + 1:]
                            datasets = []
                            for random_distance in random_distances:
                                dataset = self.combine_datasets(distance=random_distance,
                                                                train_annotation_root_path=train_annotation_root_path,
                                                                val_annotation_root_path=val_annotation_root_path,
                                                                train_image_root_path=train_image_root_path,
                                                                val_image_root_path=val_image_root_path,
                                                                transform=transform)

                                datasets.append(dataset)

                            combined_datasets = torch.utils.data.ConcatDataset(datasets)
                            indices = np.arange(len(combined_datasets))
                            np.random.seed(self.random_seed)
                            np.random.shuffle(indices)
                            indices_dataset = np.array_split(indices, len(self.target_distances) - 1)
                            sub_sequence = [(f'llo_{self.target_distances[i]}_random{j}',
                                             torch.utils.data.DataLoader(
                                                 torch.utils.data.Subset(combined_datasets, idx),
                                                 sampler=train_sampler, batch_size=self.batch_size,
                                                 num_workers=self.num_workers))
                                            for j, idx in enumerate(indices_dataset)]

                            target_dataset = self.combine_datasets(distance=self.target_distances[i],
                                                                   train_annotation_root_path=train_annotation_root_path,
                                                                   val_annotation_root_path=val_annotation_root_path,
                                                                   train_image_root_path=train_image_root_path,
                                                                   val_image_root_path=val_image_root_path,
                                                                   transform=transform)
                            target_dataloader = torch.utils.data.DataLoader(target_dataset,
                                                                            sampler=train_sampler,
                                                                            batch_size=self.batch_size,
                                                                            num_workers=self.num_workers)
                            sub_sequence.append((str(self.target_distances[i]), target_dataloader))
                            train_datasets.append((str([j[0] for j in sub_sequence]), sub_sequence))

                    elif self.training_mode == 'single':
                        for i in self.target_distances:
                            dataset = self.combine_datasets(distance=i,
                                                            train_annotation_root_path=train_annotation_root_path,
                                                            val_annotation_root_path=val_annotation_root_path,
                                                            train_image_root_path=train_image_root_path,
                                                            val_image_root_path=val_image_root_path,
                                                            transform=transform)
                            train_dataloader = torch.utils.data.DataLoader(dataset,
                                                                           sampler=train_sampler,
                                                                           batch_size=self.batch_size,
                                                                           num_workers=self.num_workers)
                            sub_sequence = [(str(i), train_dataloader)]
                            train_datasets.append((str([i]), sub_sequence))

                    elif self.training_mode == 'random-permute':
                        shuffler = np.random.default_rng(40)

                    elif self.training_mode == 'as_is':
                        sub_sequence = []
                        for train_distance in self.target_distances:
                            dataset = self.combine_datasets(distance=train_distance,
                                                            train_annotation_root_path=train_annotation_root_path,
                                                            val_annotation_root_path=val_annotation_root_path,
                                                            train_image_root_path=train_image_root_path,
                                                            val_image_root_path=val_image_root_path,
                                                            transform=transform)
                            train_dataloader = torch.utils.data.DataLoader(dataset,
                                                                           sampler=train_sampler,
                                                                           batch_size=self.batch_size,
                                                                           num_workers=self.num_workers)
                            sub_sequence.append((train_distance, train_dataloader))
                        train_datasets.append((str(self.target_distances), sub_sequence))

                    self.train_datasets.append(train_datasets)
                    self.val_datasets.append(val_datasets)

                else:
                    pass

    def run(self,
            criterion_object: Callable,
            optimizer_object: Callable,
            early_stopping: bool = True,
            patience: int = 2,
            optim_kwargs: dict = None) -> None:
        """

        :param patience: tolerance for loss increase before early stopping
        :param criterion_object:
        :param optimizer_object:
        :param early_stopping:
        :param kwargs: only for optimizer
        :return:
        """
        assert self.train_datasets is not None and self.test_datasets is not None and self.num_classes is not None, \
            "Datasets is None. Please run RunModel.load_datasets() first "

        best_state_dict = {}

        for fold, content in enumerate(self.train_datasets):
            for name, sequence in content:
                print(f"----- Training {self.model_name} with sequence: {name} -----")
                model = eval('models.' + self.model_name + f'(num_classes={self.num_classes}, pretrained={False})')
                model = model.to(self.device)
                criterion = criterion_object()
                optimizer = optimizer_object(model.parameters(), **optim_kwargs)

                self.all_training_loss[str(name)] = []
                self.all_val_loss[str(name)] = []
                self.all_val_acc_top1[str(name)] = []
                epochs_per_distance = int(np.ceil(self.epochs / len(sequence)))
                distances_seq = eval(name)

                for seq_idx, (distance, dataset) in enumerate(sequence):

                    if str(distances_seq[:seq_idx + 1]) in best_state_dict:
                        print(f"Sequence {distances_seq[:seq_idx + 1]} already in state dictionary, jumped")

                    else:
                        if str(distances_seq[:seq_idx]) in best_state_dict:
                            model.load_state_dict(best_state_dict[str(distances_seq[:seq_idx])][0])
                            optimizer.load_state_dict(best_state_dict[str(distances_seq[:seq_idx])][1])

                        elif seq_idx == 0:
                            pass
                        else:
                            raise ValueError("Uhhhhhh Something went wrong... the previous sequence is not in the state "
                                             "dict...")

                        train_loader = DataLoader(dataset, batch_size=self.batch_size,
                                                  shuffle=True,
                                                  num_workers=self.num_workers)

                        sub_training_loss, sub_val_loss, val_top1_acc = [], {d[0]: [] for d in self.val_datasets}, {d[0]: []
                                                                                                                    for d in
                                                                                                                    self.val_datasets}
                        sub_val_loss['avg'], val_top1_acc['avg'] = [], []

                        # current group's best losses
                        patience_count = 0
                        best_epoch = 0
                        best_val_acc = 0
                        best_val_loss = np.inf
                        # TODO: validation set for separate distances?
                        print(f'Current group: {distance}')
                        for epoch in range(epochs_per_distance):

                            # training
                            model.train()

                            training_loss_per_pass = 0
                            for i, (images, labels) in enumerate(train_loader):
                                images = images.to(self.device)
                                labels = labels.to(self.device)

                                outputs = model(images)
                                loss = criterion(outputs, labels)
                                optimizer.zero_grad()
                                loss.backward()
                                optimizer.step()
                                training_loss_per_pass += loss.item()
                            sub_training_loss.append(training_loss_per_pass)

                            # validation
                            val_loss_per_epoch, val_top1acc_per_epoch = {}, {}
                            predicted_all = []
                            for target_distance, val_dataset in self.val_datasets:
                                correct = 0
                                total = 0
                                val_loss_per_pass = 0
                                model.eval()
                                val_batch_size = min(len(val_dataset), self.batch_size)
                                val_dataloader = DataLoader(val_dataset, batch_size=val_batch_size,
                                                            shuffle=True,
                                                            num_workers=self.num_workers)
                                with torch.no_grad():
                                    for images, labels in val_dataloader:
                                        images = images.to(self.device)
                                        labels = labels.to(self.device)
                                        outputs = model(images)
                                        _, predicted = torch.max(outputs.data, 1)
                                        total += labels.size(0)
                                        correct += (predicted == labels).sum().item()
                                        val_loss = criterion(outputs, labels)
                                        val_loss_per_pass += val_loss.item()
                                        predicted_all.extend(predicted.cpu().detach().tolist())
                                acc = correct / total
                                val_top1_acc[target_distance].append(acc)
                                sub_val_loss[target_distance].append(val_loss_per_pass)
                                val_loss_per_epoch[target_distance] = val_loss_per_pass
                                val_top1acc_per_epoch[target_distance] = acc
                            val_loss_avg, val_top1acc_avg = np.mean(list(val_loss_per_epoch.values())), np.mean(
                                list(val_top1acc_per_epoch.values()))
                            val_top1_acc['avg'].append(val_top1acc_avg)
                            sub_val_loss['avg'].append(val_loss_avg)
                            if self.verbose >= 1:

                                predicted_count = sorted(Counter(predicted_all).items())
                                print(f"predicted label frequency: {predicted_count}")
                                for k, v in val_loss_per_epoch.items():
                                    print(k, ": ", v)

                            print('Epoch [{}/{}], Training Loss: {:.4f}, Validation Loss: {:.4f}, Validation Top1 '
                                  'Accuracy: {:.4f}'
                                  .format(epoch + 1, epochs_per_distance, training_loss_per_pass, val_loss_avg,
                                          val_top1acc_avg))
                            if val_loss_avg <= best_val_loss:
                                best_val_loss = val_loss_avg
                                best_state_dict[str(distances_seq[:seq_idx + 1])] = (
                                    model.state_dict(), optimizer.state_dict())

                                patience_count = 0
                                best_epoch = epoch + 1
                                best_val_acc = val_top1acc_avg
                            else:
                                patience_count += 1
                                if early_stopping and patience_count >= patience:
                                    print(" --- Early Stopped ---")
                                    break
                        print(
                            f"Patch distance: {distance} finished training. Best epoch: {best_epoch} Best val accuracy: {best_val_acc} Best val loss: {best_val_loss}")
                        print('\n')
                        self.all_training_loss[str(name)].append((str(distance), sub_training_loss))
                        self.all_val_loss[str(name)].append((str(distance), sub_val_loss))
                        self.all_val_acc_top1[str(name)].append((str(distance), val_top1_acc))
                self.models_statedict.append((name, model.state_dict()))
        print('-' * 20, 'All training done', '-' * 20)

    def evaluate(self, suffix: str = ''):
        test_dataloaders = [
            (name, DataLoader(dataset=dataset, batch_size=self.batch_size, shuffle=True, num_workers=self.num_workers))
            for name, dataset in self.test_datasets]

        if not os.path.isdir(self.result_dirpath):
            os.mkdir(self.result_dirpath)
        sub_dir = os.path.join(self.result_dirpath,
                               f"{self.dataset_name}_{self.model_name}_{self.training_mode}_{self.background}_{self.num_classes}class_{suffix}")
        os.mkdir(sub_dir)

        for name, model_params in self.models_statedict:
            print(f'---- Testing model trained on sequence: {name} ----')
            if self.save_checkpoints:
                torch.save(model_params, os.path.join(sub_dir, '-'.join([str(i) for i in name])))

            self.test_acc_top1[name] = []
            model = eval('models.' + self.model_name + f'(num_classes={self.num_classes}, pretrained={False})')
            model = model.to(self.device)
            model.load_state_dict(model_params)
            model.to(self.device)
            model.eval()

            for distance, test_dataloader in test_dataloaders:

                correct = 0
                total = 0
                acc = -1
                with torch.no_grad():
                    for images, labels in test_dataloader:
                        images = images.to(self.device)
                        labels = labels.to(self.device)
                        outputs = model(images)
                        _, predicted = torch.max(outputs.data, 1)
                        total += labels.size(0)
                        correct += (predicted == labels).sum().item()
                        acc = correct / total
                    self.test_acc_top1[name].append((distance, acc))
                    print(f"Test set distance: {distance} Top 1 Accuracy: {acc}")

        result = {
            'all_training_loss': self.all_training_loss,
            'all_val_loss': self.all_val_loss,
            'all_val_acc_top1': self.all_val_acc_top1,
            'test_acc_top1': self.test_acc_top1}
        with open(os.path.join(sub_dir, 'acc_n_loss.json'), 'w') as f:
            json.dump(result, f)
