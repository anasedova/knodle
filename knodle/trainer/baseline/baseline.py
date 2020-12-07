from torch.nn import Module
from torch import Tensor
import numpy as np

from knodle.final_label_decider.FinalLabelDecider import get_majority_vote_probabilities
from knodle.model import LogisticRegressionModel
import logging

from knodle.trainer import TrainerConfig
from knodle.trainer.ds_model_trainer.ds_model_trainer import DsModelTrainer
from knodle.trainer.utils.utils import print_section

logger = logging.getLogger(__name__)


class SimpleDsModelTrainer(DsModelTrainer):
    """
    The baseline class implements a baseline model for labeling data with weak supervision.
        A simple majority vote is used for this purpose.
    """

    def __init__(self, model: Module, trainer_config: TrainerConfig = None):
        super().__init__(model, trainer_config)

    def train(self, inputs: Tensor, rule_matches: np.ndarray, epochs: int, **kwargs):
        """
        This function gets final labels with a majority vote approach and trains the provided model.
        Args:
            inputs: Input tensors. These tensors will be fed to the provided model (instaces x features)
            rule_matches: All rule matches (instances x rules)
            epochs: Epochs to train
        """

        assert len(inputs) == len(rule_matches), (
            "Length of inputs and rule matches have to be the same but they are: inputs: {} | "
            "rule_matches: {}".format(len(inputs), len(rule_matches))
        )

        assert epochs > 0, "Epochs has to be set with a positive number"

        self.model.train()
        labels = get_majority_vote_probabilities(
            rule_matches=rule_matches,
            output_classes=self.trainer_config.output_classes,
        )

        labels = Tensor(labels)
        print_section("Training starts", logger)

        for current_epoch in range(epochs):
            logger.info("Epoch: {}".format(current_epoch))
            self.model.zero_grad()
            predictions = self.model(inputs)
            loss = self.trainer_config.criterion(predictions, labels)
            logger.info("Loss is: {}".format(loss.detach()))
            loss.backward()
            self.trainer_config.optimizer.step()

        print_section("Training done", logger)

    def denoise_rule_matches(self, rule_matches: np.ndarray, **kwargs) -> np.ndarray:
        """
        The baseline model trainer doesn't denoise the rule_matches. Therefore, the same array is returned
        Returns:

        """
        return rule_matches


if __name__ == "__main__":
    logistic_regression = LogisticRegressionModel(10, 2)
    obj = SimpleDsModelTrainer(logistic_regression)