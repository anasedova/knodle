from typing import Callable
import os

from snorkel.classification import cross_entropy_with_probs
import torch
from torch import Tensor
from torch.optim.optimizer import Optimizer
from knodle.trainer.utils.utils import check_and_return_device


class TrainerConfig:
    def __init__(
            self,
            criterion: Callable[[Tensor, Tensor], float] = cross_entropy_with_probs,
            batch_size: int = 32,
            optimizer: Optimizer = None,
            output_classes: int = 2,
            epochs: int = 35,
            seed: int = 42,
            output_dir_path: str = None
    ):
        self.criterion = criterion
        self.batch_size = batch_size

        if epochs <= 0:
            raise ValueError("Epochs needs to be positive")
        self.epochs = epochs

        if optimizer is None:
            raise ValueError("An optimizer needs to be provided")
        else:
            self.optimizer = optimizer
        self.output_classes = output_classes
        self.device = check_and_return_device()
        self.seed = seed
        torch.manual_seed(self.seed)

        self.output_dir_path = output_dir_path
        if output_dir_path is not None:
            os.makedirs(self.output_dir_path, exist_ok=True)



class MajorityConfig(TrainerConfig):
    def __init__(
            self,
            filter_non_labelled: bool = True,
            use_probabilistic_labels: bool = True,
            other_class_id: int = None,
            **kwargs
    ):
        super().__init__(**kwargs)
        self.filter_non_labelled = filter_non_labelled
        self.use_probabilistic_labels = use_probabilistic_labels
        self.other_class_id = other_class_id
