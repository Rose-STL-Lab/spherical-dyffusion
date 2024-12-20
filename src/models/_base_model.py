from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union

import hydra
import numpy as np
import torch
import xarray as xr
from omegaconf import DictConfig
from pytorch_lightning import LightningModule
from torch import Tensor

from src.losses.losses import get_loss
from src.utilities.utils import (
    disable_inference_dropout,
    enable_inference_dropout,
    get_logger,
)


class BaseModel(LightningModule):
    r"""This is a template base class, that should be inherited by any stand-alone ML model.
    Methods that need to be implemented by your concrete ML model (just as if you would define a :class:`torch.nn.Module`):
        - :func:`__init__`
        - :func:`forward`

    The other methods may be overridden as needed.
    It is recommended to define the attribute
        >>> self.example_input_array = torch.randn(<YourModelInputShape>)  # batch dimension can be anything, e.g. 7


    .. note::
        Please use the function :func:`predict` at inference time for a given input tensor, as it postprocesses the
        raw predictions from the function :func:`raw_predict` (or model.forward or model())!

    Args:
        name (str): optional string with a name for the model
        verbose (bool): Whether to print/log or not

    Read the docs regarding LightningModule for more information:
        https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html
    """

    def __init__(
        self,
        num_input_channels: int = None,
        num_output_channels: int = None,
        num_output_channels_raw: int = None,  # actual channels. output_channels may be larger when stacking dims
        num_conditional_channels: int = 0,
        spatial_shape_in: Union[Sequence[int], int] = None,
        spatial_shape_out: Union[Sequence[int], int] = None,
        loss_function: str = "mean_squared_error",
        loss_function_weights: Optional[Dict[str, float]] = None,
        datamodule_config: Optional[DictConfig] = None,
        debug_mode: bool = False,
        name: str = "",
        verbose: bool = True,
    ):
        super().__init__()
        # The following saves all the args that are passed to the constructor to self.hparams
        #   e.g. access them with self.hparams.monitor
        self.save_hyperparameters(ignore=["verbose", "model"])
        # Get a logger
        self.log_text = get_logger(name=self.__class__.__name__ if name == "" else name)
        self.name = name
        self.verbose = verbose
        if not self.verbose:  # turn off info level logging
            self.log_text.setLevel(logging.WARN)

        self.num_input_channels = num_input_channels
        self.num_output_channels = num_output_channels
        self.num_output_channels_raw = num_output_channels_raw
        self.num_conditional_channels = num_conditional_channels
        self.spatial_shape_in = spatial_shape_in
        self.spatial_shape_out = spatial_shape_out
        self.datamodule_config = datamodule_config

        if loss_function is not None:
            # Get the loss function
            loss_function_name = (
                loss_function if isinstance(loss_function, str) else loss_function.get("_target_", "").split(".")[-1]
            )
            self.loss_function_name = loss_function_name.lower()
            self.loss_function_weights = loss_function_weights if loss_function_weights is not None else {}
            for k in self.loss_function_weights.keys():
                assert k in ["preds"], f"Invalid loss function key: {k}"

            criterion = self.get_loss_callable()
            print_text = (
                f"Criterion: {criterion} with weights: {self.loss_function_weights}"
                if loss_function_weights
                else f"Criterion: {criterion}"
            )
            self.log_text.info(print_text)
            # Using a dictionary for the criterion, so that we can have multiple loss functions if needed
            if isinstance(criterion, torch.nn.ModuleDict):
                self.criterion = criterion
            elif isinstance(criterion, dict):
                if any(isinstance(v, torch.nn.Module) for v in criterion.values()):
                    self.criterion = torch.nn.ModuleDict(criterion)
                else:
                    self.criterion = criterion
            elif isinstance(criterion, torch.nn.Module):
                self.criterion = torch.nn.ModuleDict({"preds": criterion})
            else:
                self.criterion = {"preds": criterion}

        self._channel_dim = None
        self.ema_scope = None  # EMA scope for the model. May be set by the BaseExperiment instance
        # self._parent_module = None    # BaseExperiment instance (only needed for edge cases)

    @property
    def short_description(self) -> str:
        return self.name if self.name else self.__class__.__name__

    def get_parameters(self) -> list:
        """Return the parameters for the optimizer."""
        return list(self.parameters())

    def _get_loss_callable_from_name_or_config(self, loss_function: str, **kwargs):
        """Return the loss function"""
        if isinstance(loss_function, str):
            loss = get_loss(loss_function, **kwargs)
        elif isinstance(loss_function, dict):
            loss = {k: get_loss(v, **kwargs) for k, v in loss_function.items()}
        else:
            loss = hydra.utils.instantiate(loss_function)
        return loss

    def get_loss_callable(self, reduction: str = "mean", **kwargs):
        """Return the loss function"""
        loss_function = self.hparams.loss_function
        loss = self._get_loss_callable_from_name_or_config(loss_function, reduction=reduction, **kwargs)
        return loss

    @property
    def num_params(self):
        """Returns the number of parameters in the model"""
        return sum(p.numel() for p in self.get_parameters() if p.requires_grad)

    @property
    def channel_dim(self):
        if self._channel_dim is None:
            self._channel_dim = 1
        return self._channel_dim

    def evaluation_results_to_xarray(self, results: Dict[str, np.ndarray], **kwargs) -> Dict[str, xr.DataArray]:
        """Convert the evaluation results to a xarray dataset"""
        raise NotImplementedError(f"Please implement ``evaluation_results_to_xarray`` for {self.__class__.__name__}")

    def forward(self, X: Tensor, condition: Tensor = None, **kwargs):
        r"""Standard ML model forward pass (to be implemented by the specific ML model).

        Args:
            X (Tensor): Input data tensor of shape :math:`(B, *, C_{in})`
        Shapes:
            - Input: :math:`(B, *, C_{in})`,

            where :math:`B` is the batch size, :math:`*` is the spatial dimension(s) of the data,
            and :math:`C_{in}` is the number of input features/channels.
        """
        raise NotImplementedError("Base model is an abstract class!")

    def concat_condition_if_needed(self, inputs: Tensor, condition: Tensor = None, static_condition: Tensor = None):
        if self.num_conditional_channels > 0:
            # exactly one of condition or static_condition should be not None
            if condition is None and static_condition is None:
                raise ValueError(
                    f"condition and static_condition are both None but num_conditional_channels is {self.num_conditional_channels}"
                )
            elif condition is not None and static_condition is not None:
                condition = torch.cat((condition, static_condition), dim=1)
            elif condition is None:
                assert static_condition is not None, "condition and static_condition are both None"
                condition = static_condition
            else:
                assert static_condition is None, "condition and static_condition are both not None"

            if hasattr(self, "upsample_condition"):
                condition = self.upsample_condition(condition)
            try:
                # log.info(f"{inputs.shape=}, {condition.shape=}")
                x = torch.cat((inputs, condition), dim=1)
            except RuntimeError as e:
                raise RuntimeError(f"inputs.shape: {inputs.shape}, condition.shape: {condition.shape}") from e
        else:
            x = inputs
            assert condition is None, "condition is not None but num_conditional_channels is 0"
            assert static_condition is None, "static_condition is not None but num_conditional_channels is 0"
        return x

    def get_loss(
        self,
        inputs: Tensor,
        targets: Tensor,
        raw_targets: Tensor = None,
        condition: Tensor = None,
        metadata: Any = None,
        predictions_mask: Optional[Tensor] = None,
        # targets_mask: Optional[Tensor] = None,
        return_predictions: bool = False,
        predictions_post_process: Optional[Callable] = None,
        targets_pre_process: Optional[Callable] = None,
        **kwargs,
    ) -> Union[Tensor, Tuple[Tensor, Tensor]]:
        """Get the loss for the given inputs and targets.

        Args:
            inputs (Tensor): Input data tensor of shape :math:`(B, *, C_{in})`
            targets (Tensor): Target data tensor of shape :math:`(B, *, C_{out})`
            raw_targets (Tensor): Raw target data tensor of shape :math:`(B, *, C_{out})`
            condition (Tensor): Conditional data tensor of shape :math:`(B, *, C_{cond})`
            metadata (Any): Optional metadata
            predictions_mask (Tensor): Mask for the predictions, before computing the loss. Default: None (no mask)
            return_predictions (bool): Whether to return the predictions or not. Default: False.
                                    Note: this will return all the predictions, not just the masked ones (if any).
        """

        def mask_data(data):
            if predictions_mask is not None:
                return data[..., predictions_mask]
            return data

        # Predict
        if torch.is_tensor(inputs):
            predictions = self(inputs, condition=condition, **kwargs)
        else:
            predictions = self(**inputs, condition=condition, **kwargs)

        if torch.is_tensor(predictions):
            if predictions_post_process is not None:
                predictions = predictions_post_process(predictions)
            predictions = mask_data(predictions)
            targets = mask_data(targets)
            assert (
                predictions.shape == targets.shape
            ), f"Be careful: Predictions shape {predictions.shape} != targets shape {targets.shape}. Missing singleton dimensions after batch dim. can be fatal."
            loss = self.criterion["preds"](predictions, targets)
            assert len(self.loss_function_weights) == 0, "Loss function weights are not supported for this case"
            loss_dict = dict(loss=loss)
        else:
            if predictions_post_process is not None:
                # Do post-processing of the predictions (but not other outputs of the model)
                predictions["preds"] = predictions_post_process(predictions["preds"])
            loss = 0.0
            loss_dict = dict()
            # For example, base_keys = ["preds"]
            # With corresponding preds & targets shapes: (B, *, C_out, H, W)
            for k in targets.keys():
                base_key = k.replace("inputs", "preds")
                loss_weight_k = self.loss_function_weights.get(base_key, 1.0)
                predictions_k = mask_data(predictions[base_key])
                targets_k = mask_data(targets[k])
                loss_k = self.criterion[base_key](predictions_k, targets_k)
                loss += loss_weight_k * loss_k
                loss_dict[f"loss/{base_key}"] = loss_k.item()
            loss_dict["loss"] = loss  # total loss, used to backpropagate

        if return_predictions:
            return loss_dict, predictions
        return loss_dict

    def predict_forward(self, *inputs: Tensor, metadata: Any = None, **kwargs):
        """Forward pass for prediction. Usually the same as the forward pass,
        but can be different for some models (e.g. sampling in probabilistic models).
        """
        y = self(*inputs, **kwargs)
        return y

    # Auxiliary methods
    @contextmanager
    def inference_dropout_scope(self, condition: bool, context=None):
        assert isinstance(condition, bool), f"Condition must be a boolean, got {condition}"
        if condition:
            enable_inference_dropout(self)
            if context is not None:
                self.log_text.info(f"{context}: Switched to enabled inference dropout")
        try:
            yield None
        finally:
            if condition:
                disable_inference_dropout(self)
                if context is not None:
                    self.log_text.info(f"{context}: Switched to disabled inference dropout")

    def enable_inference_dropout(self):
        """Set all dropout layers to training mode"""
        enable_inference_dropout(self)

    def disable_inference_dropout(self):
        """Set all dropout layers to eval mode"""
        disable_inference_dropout(self)

    def register_buffer_dummy(self, name, tensor, **kwargs):
        try:
            self.register_buffer(name, tensor, **kwargs)
        except TypeError:  # old pytorch versions do not have the arg 'persistent'
            self.register_buffer(name, tensor)
