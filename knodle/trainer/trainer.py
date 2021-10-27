import copy
import os
import logging
import statistics
from typing import Dict, Tuple

from torch.nn.modules.loss import _Loss
from tqdm.auto import tqdm
from abc import ABC, abstractmethod

import numpy as np
from sklearn.metrics import classification_report

import torch
from torch import Tensor
from torch.nn import Module
from torch.utils.data import TensorDataset, DataLoader

from knodle.trainer.utils.validation_with_cv import get_val_cv_dataset
from knodle.evaluation.other_class_metrics import classification_report_other_class
from knodle.model.EarlyStopping import EarlyStopping
from knodle.transformation.torch_input import input_labels_to_tensordataset
from knodle.transformation.rule_reduction import reduce_rule_matches
from knodle.evaluation.plotting import draw_loss_accuracy_plot

from knodle.trainer.config import TrainerConfig, BaseTrainerConfig
from knodle.trainer.utils.utils import log_section, accuracy_of_probs
from knodle.trainer.utils.checks import check_other_class_id


logger = logging.getLogger(__name__)


class Trainer(ABC):
    def __init__(
            self,
            model: Module,
            mapping_rules_labels_t: np.ndarray,
            model_input_x: TensorDataset,
            rule_matches_z: np.ndarray,
            dev_model_input_x: TensorDataset = None,
            dev_gold_labels_y: TensorDataset = None,
            trainer_config: TrainerConfig = None,
    ):
        """
        Constructor for each Trainer.
            Args:
                model: PyTorch model which will be used for final classification.
                mapping_rules_labels_t: Mapping of rules to labels, binary encoded. Shape: rules x classes
                model_input_x: Input tensors. These tensors will be fed to the provided model.
                rule_matches_z: Binary encoded array of which rules matched. Shape: instances x rules
                trainer_config: Config for different parameters like loss function, optimizer, batch size.
        """
        self.model = model
        self.mapping_rules_labels_t = mapping_rules_labels_t
        self.model_input_x = model_input_x
        self.rule_matches_z = rule_matches_z
        self.dev_model_input_x = dev_model_input_x
        self.dev_gold_labels_y = dev_gold_labels_y

        if trainer_config is None:
            self.trainer_config = TrainerConfig(model)
        else:
            self.trainer_config = trainer_config

    @abstractmethod
    def train(
            self,
            model_input_x: TensorDataset = None, rule_matches_z: np.ndarray = None,
            dev_model_input_x: TensorDataset = None, dev_gold_labels_y: TensorDataset = None
    ):
        pass

    @abstractmethod
    def test(self, test_features: TensorDataset, test_labels: TensorDataset):
        pass

    def initialise_optimizer(self):
        try:
            return self.trainer_config.optimizer(params=self.model.parameters(), lr=self.trainer_config.lr)
        except TypeError:
            logger.info("Wrong optimizer parameters. Optimizer should belong to torch.optim class or be PyTorch "
                        "compatible.")


class BaseTrainer(Trainer):

    def __init__(
            self,
            model: Module,
            mapping_rules_labels_t: np.ndarray,
            model_input_x: TensorDataset,
            rule_matches_z: np.ndarray,
            **kwargs):
        if kwargs.get("trainer_config", None) is None:
            kwargs["trainer_config"] = BaseTrainerConfig()
        super().__init__(model, mapping_rules_labels_t, model_input_x, rule_matches_z, **kwargs)

        check_other_class_id(self.trainer_config, self.mapping_rules_labels_t)

    def _load_train_params(
            self,
            model_input_x: TensorDataset = None, rule_matches_z: np.ndarray = None,
            dev_model_input_x: TensorDataset = None, dev_gold_labels_y: TensorDataset = None
    ):
        if model_input_x is not None and rule_matches_z is not None:
            self.model_input_x = model_input_x
            self.rule_matches_z = rule_matches_z
        if dev_model_input_x is not None and dev_gold_labels_y is not None:
            self.dev_model_input_x = dev_model_input_x
            self.dev_gold_labels_y = dev_gold_labels_y

    def _apply_rule_reduction(self):
        reduced_dict = reduce_rule_matches(
            rule_matches_z=self.rule_matches_z, mapping_rules_labels_t=self.mapping_rules_labels_t,
            drop_rules=self.trainer_config.drop_rules, max_rules=self.trainer_config.max_rules,
            min_coverage=self.trainer_config.min_coverage)
        self.rule_matches_z = reduced_dict["train_rule_matches_z"]
        self.mapping_rules_labels_t = reduced_dict["mapping_rules_labels_t"]

    def _make_dataloader(
            self, dataset: TensorDataset, shuffle: bool = True
    ) -> DataLoader:
        dataloader = DataLoader(
            dataset,
            batch_size=self.trainer_config.batch_size,
            drop_last=False,
            shuffle=shuffle,
        )
        return dataloader

    def _load_batch(self, batch):

        input_batch = [inp.to(self.trainer_config.device) for inp in batch[0: -1]]
        label_batch = batch[-1].to(self.trainer_config.device)

        return input_batch, label_batch

    def _train_loop(
            self, feature_label_dataloader: DataLoader, use_sample_weights: bool = False, draw_plot: bool = False,
            verbose: bool = True
    ):
        log_section("Training starts", logger)

        if self.trainer_config.early_stopping and self.dev_model_input_x is not None:
            es = EarlyStopping(
                save_model_path=self.trainer_config.save_model_path,
                save_model_name=self.trainer_config.save_model_name
            )
        elif self.trainer_config.early_stopping and self.dev_model_input_x is None:
            logger.info("Early stopping won't be performed since there is no dev set provided.")

        self.model.to(self.trainer_config.device)
        self.model.train()

        train_losses, train_acc = [], []
        if self.dev_model_input_x is not None:
            dev_losses, dev_acc = [], []

        for current_epoch in range(self.trainer_config.epochs):

            if verbose:
                logger.info("Epoch: {}".format(current_epoch))

            epoch_loss, epoch_acc, steps = 0.0, 0.0, 0

            for batch in feature_label_dataloader:
                input_batch, label_batch = self._load_batch(batch)
                steps += 1

                if use_sample_weights:
                    input_batch, sample_weights = input_batch[:-1], input_batch[-1]

                # forward pass
                self.trainer_config.optimizer.zero_grad()
                outputs = self.model(*input_batch)
                if isinstance(outputs, torch.Tensor):
                    logits = outputs
                else:
                    logits = outputs[0]

                if use_sample_weights:
                    loss = self.calculate_loss_with_sample_weights(logits, label_batch, sample_weights)
                else:
                    loss = self.calculate_loss(logits, label_batch)

                # backward pass
                loss.backward()
                if isinstance(self.trainer_config.grad_clipping, (int, float)):
                    torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.trainer_config.grad_clipping)

                self.trainer_config.optimizer.step()
                acc = accuracy_of_probs(logits, label_batch)

                epoch_loss += loss.detach().item()
                epoch_acc += acc.item()

                # print epoch loss and accuracy after each 10% of training is done
                if verbose:
                    try:
                        if steps % (int(round(len(feature_label_dataloader) / 10))) == 0:
                            logger.info(f"Train loss: {epoch_loss/steps:.3f}, Train accuracy: {epoch_acc/steps:.3f}")
                    except ZeroDivisionError:
                        continue

            avg_loss = epoch_loss / len(feature_label_dataloader)
            avg_acc = epoch_acc / len(feature_label_dataloader)
            train_losses.append(avg_loss)
            train_acc.append(avg_acc)

            if verbose:
                logger.info("Epoch train loss: {}".format(avg_loss))
                logger.info("Epoch train accuracy: {}".format(avg_acc))

            if self.dev_model_input_x is not None:
                dev_clf_report, dev_loss = self.test_with_loss(self.dev_model_input_x, self.dev_gold_labels_y)
                dev_losses.append(dev_loss)
                dev_acc.append(dev_clf_report["accuracy"])
                logger.info("Epoch development accuracy: {}".format(dev_clf_report["accuracy"]))

                if self.trainer_config.early_stopping:
                    es(dev_loss, self.model)
                    if es.early_stop:
                        logger.info("The model performance on validation training does not change -> early stopping.")
                        break

            self.model.train()
            self.model.to(self.trainer_config.device)

        logger.info("Train avg loss: {}".format(sum(train_losses)/len(train_losses)))
        logger.info("Train avg accuracy: {}".format(sum(train_acc)/len(train_acc)))

        log_section("Training done", logger)

        if draw_plot:
            if self.dev_model_input_x:
                draw_loss_accuracy_plot(
                    {"train loss": train_losses, "train acc": train_acc, "dev loss": dev_losses, "dev acc": dev_acc}
                )
            else:
                draw_loss_accuracy_plot({"train loss": train_losses, "train acc": train_acc})

        self.model.eval()

        # load the best model for further evaluation
        if self.trainer_config.early_stopping:
            self.load_model(self.trainer_config.save_model_name, self.trainer_config.save_model_path)
            logger.info("The best model on dev set will be used for evaluation. ")

    def _prediction_loop(
            self, feature_label_dataloader: DataLoader, loss_calculation: bool = False
    ) -> Tuple[np.ndarray, np.ndarray, float]:

        self.model.to(self.trainer_config.device)
        self.model.eval()
        predictions_list, label_list = [], []
        dev_loss, dev_acc = 0.0, 0.0

        # Loop over predictions
        with torch.no_grad():
            for batch in tqdm(feature_label_dataloader):
                input_batch, label_batch = self._load_batch(batch)

                # forward pass
                self.trainer_config.optimizer.zero_grad()
                outputs = self.model(*input_batch)
                prediction_vals = outputs[0] if not isinstance(outputs, torch.Tensor) else outputs

                if loss_calculation:
                    dev_loss += self.calculate_loss(prediction_vals, label_batch.long())

                # add predictions and labels
                predictions = np.argmax(prediction_vals.detach().cpu().numpy(), axis=-1)
                predictions_list.append(predictions)
                label_list.append(label_batch.detach().cpu().numpy())

        predictions = np.squeeze(np.hstack(predictions_list))
        gold_labels = np.squeeze(np.hstack(label_list))

        return predictions, gold_labels, dev_loss

    def test(self, features_dataset: TensorDataset, labels: TensorDataset) -> Dict:
        """
        The function tests the trained model on the test set and returns the classification report
        :param features_dataset: features_dataset: TensorDataset with test samples
        :param labels: true labels
        :return: classification report (either with respect to other class or not)
        """

        gold_labels = labels.tensors[0].cpu().numpy()
        feature_label_dataset = input_labels_to_tensordataset(features_dataset, gold_labels)
        feature_label_dataloader = self._make_dataloader(feature_label_dataset, shuffle=False)
        predictions, gold_labels, dev_loss = self._prediction_loop(feature_label_dataloader)

        clf_report = self.collect_report(predictions, gold_labels)

        return clf_report

    def test_with_loss(self, features_dataset: TensorDataset, labels: TensorDataset) -> Tuple[Dict, float]:
        """
        The function tests the trained model on the test set and returns the classification report and average loss.
        :param features_dataset: TensorDataset with test samples
        :param labels: true labels
        :return: classification report (either with respect to other class or not) + average test loss
        """

        gold_labels = labels.tensors[0].cpu().numpy()

        feature_label_dataset = input_labels_to_tensordataset(features_dataset, gold_labels)
        feature_label_dataloader = self._make_dataloader(feature_label_dataset, shuffle=False)
        predictions, gold_labels, dev_loss = self._prediction_loop(feature_label_dataloader, loss_calculation=True)

        clf_report = self.collect_report(predictions, gold_labels)
        avg_los = dev_loss / len(feature_label_dataloader)

        return clf_report, avg_los

    def load_model(self, save_model_name: str = None, save_model_path: str = None) -> None:
        if not save_model_name:
            save_model_name = "checkpoint_best"
        save_model_name = save_model_name + "_best.pt"

        if not save_model_path:
            save_model_path = "trained_models"

        model_path = os.path.join(save_model_path, save_model_name)
        try:
            self.model.load_state_dict(torch.load(model_path))
        except FileNotFoundError:
            logger.info(
                f"The saved model in {save_model_path} wasn't found.The latest trained model will be validated instead."
            )

    def collect_report(self, predictions: np.ndarray, gold_labels: np.ndarray) -> Dict:
        """
        Collects the classification report (in sklearn format)
        :param predictions: predicted labels
        :param gold_labels: true labels
        :return: Dictionary of format: {class 0: {prec, recall, f1}, class 1: {...}, ..., acc, macro & weighted metrics}
        """
        if self.trainer_config.evaluate_with_other_class:
            return classification_report_other_class(
                y_true=gold_labels, y_pred=predictions, ids2labels=self.trainer_config.ids2labels,
                other_class_id=self.trainer_config.other_class_id
            )
        else:
            return classification_report(y_true=gold_labels, y_pred=predictions, output_dict=True)

    def calculate_loss_with_sample_weights(self, logits: Tensor, gold_labels: Tensor, sample_weights: Tensor) -> float:
        if isinstance(self.trainer_config.criterion, type) and issubclass(self.trainer_config.criterion, _Loss):
            criterion = self.trainer_config.criterion(
                weight=self.trainer_config.class_weights, reduction="none"
            ).cuda() if self.trainer_config.device == torch.device("cuda") else self.trainer_config.criterion(
                weight=self.trainer_config.class_weights, reduction="none"
            )
            loss_no_reduction = criterion(logits, gold_labels)
        else:
            loss_no_reduction = self.trainer_config.criterion(
                logits, gold_labels, weight=self.trainer_config.class_weights, reduction="none"
            )
        return (loss_no_reduction * sample_weights).mean()

    def calculate_loss(self, logits: Tensor, gold_labels: Tensor) -> float:
        if isinstance(self.trainer_config.criterion, type) and issubclass(self.trainer_config.criterion, _Loss):
            criterion = self.trainer_config.criterion(
                weight=self.trainer_config.class_weights
            ).cuda() if self.trainer_config.device == torch.device("cuda") else self.trainer_config.criterion(
                weight=self.trainer_config.class_weights
            )
            return criterion(logits, gold_labels)
        else:
            return self.trainer_config.criterion(logits, gold_labels, weight=self.trainer_config.class_weights)

    def _validate_with_cv(self, features, noisy_labels, folds: int = 10) -> Tuple[float, float, float, float, float]:

        trained_model = copy.deepcopy(self.model)

        arr_train_datasets, arr_test_features, arr_test_labels = \
            get_val_cv_dataset(
                features, self.rule_matches_z, noisy_labels, folds=folds, seed=self.trainer_config.seed
            )

        dev_acc, dev_prec, dev_rec, dev_f1, dev_losses = [], [], [], [], []

        i = 1
        for (train_data, test_features, test_labels) in zip(arr_train_datasets, arr_test_features, arr_test_labels):
            logger.info(f"Validation run {i}")
            self.model = copy.deepcopy(trained_model)
            train_features = self._make_dataloader(train_data)

            self._train_loop(train_features)
            clf_report, dev_loss = self.test_with_loss(test_features, test_labels)

            dev_acc.append(clf_report['accuracy'])
            dev_prec.append(clf_report['precision'])
            dev_rec.append(clf_report['recall'])
            dev_f1.append(clf_report['f1'])
            dev_losses.append(dev_loss)

            logger.info(f"Clf_report: {clf_report}, Loss: {dev_loss}")
            i += 1

        avg_acc = statistics.mean(dev_acc)
        avg_prec = statistics.mean(dev_prec)
        avg_rec = statistics.mean(dev_rec)
        avg_f1 = statistics.mean(dev_f1)
        avg_loss = statistics.mean(dev_losses)

        logger.info(f"Average dev accuracy: {avg_acc}")
        logger.info(f"Average dev precision: {avg_prec}")
        logger.info(f"Average dev recall: {avg_rec}")
        logger.info(f"Average dev f1: {avg_f1}")
        logger.info(f"Average dev loss: {avg_loss}")

        self.model = trained_model

        return avg_acc, avg_prec, avg_rec, avg_f1, avg_loss
