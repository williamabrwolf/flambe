import math
from copy import deepcopy
from typing import Dict, List, Optional, Any, Tuple, Iterator

import torch
from torch.optim.optimizer import Optimizer
from torch.optim.lr_scheduler import _LRScheduler, ReduceLROnPlateau
from torch.nn.utils.clip_grad import clip_grad_norm_, clip_grad_value_

from flambe.dataset import Dataset
from flambe.compile import Schema, State, Component
from flambe.nn import Module
from flambe.sampler import Sampler
from flambe.metric import Metric
from flambe.logging import log


class Trainer(Component):
    """Implement a Trainer block.

    A `Trainer` takes as input data, model and optimizer,
    and executes training incrementally in `run`.

    Note that it is important that a trainer run be long enough
    to not increase overhead, so at least a few seconds, and ideally
    multiple minutes.

    """

    def __init__(self,
                 dataset: Dataset,
                 train_sampler: Sampler,
                 val_sampler: Sampler,
                 model: Module,
                 loss_fn: Metric,
                 metric_fn: Metric,
                 optimizer: Optimizer,
                 scheduler: Optional[_LRScheduler] = None,
                 device: Optional[str] = None,
                 max_steps: int = 10,
                 epoch_per_step: float = 1.0,
                 iter_per_step: Optional[int] = None,
                 batches_per_iter: int = 1,
                 lower_is_better: bool = False,
                 max_grad_norm: Optional[float] = None,
                 max_grad_abs_val: Optional[float] = None,
                 extra_validation_metrics: Optional[List[Metric]] = None) -> None:
        """Initialize an instance of Trainer

        Parameters
        ----------
        dataset : Dataset
            The dataset to use in training the model
        train_sampler : Sampler
            The sampler to use over training examples during training
        val_sampler : Sampler
            The sampler to use over validation examples
        model : Module
            The model to train
        loss_fn: Metric
            The loss function to use in training the model
        metric_fn: Metric
            The metric function to use in evaluation
        optimizer : torch.optim.Optimizer
            The optimizer to use
        scheduler : torch.optim.lr_scheduler._LRScheduler, optional
            An optional learning rate scheduler
        device: str, optional
            The device to use in the computation.
        max_steps : int, optional
            The maximum number of training steps to run
        epoch_per_step : float, optional
            Fraction of an epoch to perform in a single training step
            (i.e before a checkpoint.) Defaults to 1.
            Overridden by `iter_per_step`, if given.
        iter_per_step : int, optional
            Number of iterations to perform in a single training step.
            Overrides `epoch_per_step` if given.
        batches_per_iter : int, optional
            Number of batches to pass through the model before
            calling optimizer.step. Requires the sampler to have
            drop_last set to True. (default set to 1 so optimizer.step
            is called after every batch)
        lower_is_better : bool, optional
            If true, the lowest val metric is considered best,
            otherwise the highest. Defaults to False.
        max_grad_norm : float, optional
            Maximum Euclidean norm of gradient after clipping.
        max_grad_abs_val: float, optional
            Maximum absolute value of all gradient vector components
            after clipping.
        extra_validation_metrics: Optional[List[Metric]]
            A list with extra metrics to show in each step
            but which don't guide the training procedures
            (i.e model selection through early stopping)

        """
        self.dataset = dataset
        self.train_sampler = train_sampler
        self.val_sampler = val_sampler
        self.model = model
        self.loss_fn = loss_fn
        self.metric_fn = metric_fn
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.lower_is_better = lower_is_better
        self.max_grad_norm = max_grad_norm
        self.max_grad_abs_val = max_grad_abs_val
        self.extra_validation_metrics = extra_validation_metrics or []

        # By default, no prefix applied to tb logs
        self.tb_log_prefix = None

        # Select right device
        if device is not None:
            self.device = device
        else:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"

        if (not getattr(self.train_sampler, 'drop_last', False) and batches_per_iter != 1):
            raise ValueError(f'batches_per_iter cannot be set to {batches_per_iter} '
                             'if the sampler does not have `drop_last` set to True')

        self.batches_per_iter = batches_per_iter
        n_batches = self.train_sampler.length(dataset.train)

        if iter_per_step is None:
            # Compute epoch per step
            if self.batches_per_iter > n_batches:
                raise Exception(f'Please set batches_per_iter ({self.batches_per_iter}) '
                                f'to be ≤ the length of your train_sampler '
                                f'({n_batches})')
            iter_per_epoch = n_batches // self.batches_per_iter
            iter_per_step = math.ceil(epoch_per_step * iter_per_epoch)
        else:
            # Iter per step takes precedent over epoch_per_step
            epoch_per_step = iter_per_step / n_batches

        self.iter_per_step = iter_per_step
        self.max_steps = max_steps

        self._step = 0
        self._best_metric = None
        self._best_model = None
        self.register_attrs('_step', '_best_metric', '_best_model')

        n_epochs = math.ceil(epoch_per_step * max_steps)

        self._train_iterator = self.train_sampler.sample(dataset.train, n_epochs)

    def _batch_to_device(self, batch: Tuple[torch.Tensor, ...]) -> Tuple[torch.Tensor, ...]:
        """Move the current batch on the correct device.

        Can be overriden if a batch doesn't follow the expected
        structure. For example if the batch is a dictionary.

        Parameters
        ----------
        batch: Tuple[torch.Tensor, ...]
            The batch to train on.

        """
        batch = tuple(t.to(self.device) for t in batch)
        return batch

    def _compute_loss(self, batch: Tuple[torch.Tensor, ...]) -> torch.Tensor:
        """Compute the loss given a single batch

        Parameters
        ----------
        batch: Tuple[torch.Tensor, ...]
            The batch to train on.

        """
        pred, target = self.model(*batch)
        loss = self.loss_fn(pred, target)
        return loss

    def _train_step(self) -> None:
        """Run a training step over the training data."""
        self.model.train()

        tb_prefix = f"{self.tb_log_prefix} " if self.tb_log_prefix else ""

        with torch.enable_grad():
            for i in range(self.iter_per_step):
                # Zero the gradients and clear the accumulated loss
                self.optimizer.zero_grad()
                accumulated_loss = 0.0
                for _ in range(self.batches_per_iter):
                    # Get next batch
                    batch = next(self._train_iterator)
                    batch = self._batch_to_device(batch)

                    # Compute loss
                    loss = self._compute_loss(batch) / self.batches_per_iter
                    accumulated_loss += loss.item()
                    loss.backward()

                # Log loss
                global_step = (self.iter_per_step * self._step) + i

                # Clip gradients if necessary
                if self.max_grad_norm:
                    clip_grad_norm_(self.model.parameters(), self.max_grad_norm)
                if self.max_grad_abs_val:
                    clip_grad_value_(self.model.parameters(), self.max_grad_abs_val)

                log(f'{tb_prefix}Training/Loss', accumulated_loss, global_step)
                log(f'{tb_prefix}Training/Gradient_Norm', self.model.gradient_norm, global_step)
                log(f'{tb_prefix}Training/Parameter_Norm', self.model.parameter_norm, global_step)

                # Optimize
                self.optimizer.step()

            # Zero the gradients when exiting a train step
            self.optimizer.zero_grad()

    def _aggregate_preds(self, data_iterator: Iterator) -> Tuple[torch.Tensor, torch.Tensor]:
        """Aggregate the predicitons and targets for the dataset.

        Parameters
        ----------
        data_iterator: Iterator
            Batches of data.

        Returns
        -------
        Tuple[torch.tensor, torch.tensor]
            The predictions and targets.

        """
        preds, targets = [], []
        for batch in data_iterator:
            batch = self._batch_to_device(batch)
            pred, target = self.model(*batch)
            preds.append(pred.cpu())
            targets.append(target.cpu())

        preds = torch.cat(preds, dim=0)
        targets = torch.cat(targets, dim=0)
        return preds, targets

    def _eval_step(self) -> None:
        """Run an evaluation step over the validation data."""
        self.model.eval()

        # Initialize a 1-epoch iteration through the validation set
        val_iterator = self.val_sampler.sample(self.dataset.val)

        with torch.no_grad():

            preds, targets = self._aggregate_preds(val_iterator)
            val_loss = self.loss_fn(preds, targets).item()
            val_metric = self.metric_fn(preds, targets).item()

        # Update best model
        sign = (-1)**(self.lower_is_better)
        if self._best_metric is None or (sign * val_metric > sign * self._best_metric):
            self._best_metric = val_metric
            self._best_model = deepcopy(self.model.state_dict())

        # Update scheduler
        if self.scheduler is not None:
            if isinstance(self.scheduler, ReduceLROnPlateau):
                self.scheduler.step(val_loss)
            else:
                # torch's _LRScheduler.step DOES have a default value
                # so passing in no args is fine; it will automatically
                # compute the current epoch
                self.scheduler.step()  # type: ignore

        tb_prefix = f"{self.tb_log_prefix} " if self.tb_log_prefix else ""

        # Log metrics
        log(f'{tb_prefix}Validation/Loss', val_loss, self._step)
        log(f'{tb_prefix}Validation/{self.metric_fn}', val_metric, self._step)
        log(f'{tb_prefix}Best/{self.metric_fn}', self._best_metric, self._step)  # type: ignore
        for metric in self.extra_validation_metrics:
            log(f'{tb_prefix}Validation/{metric}',
                metric(preds, targets).item(), self._step)  # type: ignore

    def run(self) -> bool:
        """Evaluate and then train until the next checkpoint

        Returns
        ------
        bool
            Whether the component should continue running.

        """
        self._eval_step()
        if self._step < self.max_steps:
            self._train_step()

        # Simple stopping rule, if we exceed the max number of steps
        self._step += 1
        continue_ = self._step < self.max_steps
        if not continue_:
            self._eval_step()
            self.model.load_state_dict(self._best_model)

        return continue_

    def metric(self) -> Optional[float]:
        """Override this method to enable scheduling.

        Returns
        -------
        float
            The metric to compare computable variants.

        """
        return self._best_metric

    def _state(self,
               state_dict: State,
               prefix: str,
               local_metadata: Dict[str, Any]) -> State:
        state_dict[prefix + 'optimizer'] = self.optimizer.state_dict()
        if self.scheduler is not None:
            state_dict[prefix + 'scheduler'] = self.scheduler.state_dict()
        return state_dict

    def _load_state(self,
                    state_dict: State,
                    prefix: str,
                    local_metadata: Dict[str, Any],
                    strict: bool,
                    missing_keys: List[Any],
                    unexpected_keys: List[Any],
                    error_msgs: List[Any]) -> None:
        self.optimizer.load_state_dict(state_dict[prefix + 'optimizer'])
        if self.scheduler is not None:
            self.scheduler.load_state_dict(state_dict[prefix + 'scheduler'])
        # Useful when loading the model after training
        done = self._step >= self.max_steps
        if done:
            self.model.load_state_dict(self._best_model)

    @classmethod
    def precompile(cls, **kwargs):
        """Override initialization.

        Ensure that the model is compiled and pushed to the right
        device before its parameters are passed to the optimizer.

        """
        # Select right device
        device = kwargs.get('device', None)
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"

        # Compile all objects and push Modules to the device
        for k, obj in kwargs.items():
            obj = obj() if isinstance(obj, Schema) else obj
            if isinstance(obj, torch.nn.Module):
                obj.to(device)
