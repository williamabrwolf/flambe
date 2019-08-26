import os
from typing import Optional

import torch

from flambe.compile import Component
from flambe.dataset import Dataset
from flambe.nn import Module
from flambe.metric import Metric
from flambe.sampler import Sampler
from flambe.logging import log


class Evaluator(Component):
    """Implement an Evaluator block.

    An `Evaluator` takes as input data, and a model and executes
    the evaluation. This is a single step `Component` object.

    Parameters
    ----------
    dataset : Dataset
        The dataset to run evaluation on
    eval_sampler : Sampler
        The sampler to use over validation examples
    model : Module
        The model to train
    metric_fn: Metric
        The metric to use for evaluation
    eval_data: str
        The data split to evaluate on: one of train, val or test
    device: str, optional
        The device to use in the computation.

    """

    def __init__(self,
                 dataset: Dataset,
                 eval_sampler: Sampler,
                 model: Module,
                 metric_fn: Metric,
                 output_path: str,
                 eval_data: str = 'test',
                 device: Optional[str] = None) -> None:
        self.eval_sampler = eval_sampler
        self.model = model
        self.metric_fn = metric_fn
        self.output_path = os.path.expanduser(output_path)
        self.eval_metric = None
        self.dataset = dataset

        # Select right device
        if device is not None:
            self.device = device
        else:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"

        data = getattr(dataset, eval_data)
        self._eval_iterator = self.eval_sampler.sample(data)

        # By default, no prefix applied to tb logs
        self.tb_log_prefix = None

    def run(self, block_name: str = None) -> bool:
        """Run the evaluation.

        Returns
        ------
        bool
            Whether the computable has completed.

        """
        self.model.to(self.device)
        self.model.eval()
        with torch.no_grad():
            preds, targets = [], []

            for batch in self._eval_iterator:
                pred, target = self.model(*[t.to(self.device) for t in batch])
                preds.append(pred.cpu())
                targets.append(target.cpu())

            preds = torch.cat(preds, dim=0)  # type: ignore
            targets = torch.cat(targets, dim=0)  # type: ignore
            self.eval_metric = self.metric_fn(preds, targets).item()

            with open(self.output_path, 'w') as f:
                for target, pred in zip(targets, preds):
                    target = target.item()
                    pred = [str(p) for p in pred.tolist()]
                    # TODO: write the `id`
                    line = f"T{target} {' '.join(pred)}\n"
                    f.write(line)

            tb_prefix = f"{self.tb_log_prefix} " if self.tb_log_prefix else ""

            log(f'{tb_prefix}Eval {self.metric_fn}',  # type: ignore
                self.eval_metric, global_step=0)

        continue_ = False  # Single step so don't continue
        return continue_

    def metric(self) -> Optional[float]:
        """Override this method to enable scheduling.

        Returns
        -------
        float
            The metric to compare computable varients

        """
        return self.eval_metric
