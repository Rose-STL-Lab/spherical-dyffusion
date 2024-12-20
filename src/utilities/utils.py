"""
Author: Salva Rühling Cachay
"""

from __future__ import annotations

import functools
import logging
import os
import random
import re
import subprocess
from difflib import SequenceMatcher
from inspect import isfunction
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import xarray as xr
from einops import rearrange
from omegaconf import DictConfig, OmegaConf
from tensordict import TensorDict, TensorDictBase
from torch import Tensor

from src.models.modules.drop_path import DropPath


def get_logger(name=__name__, level=logging.INFO) -> logging.Logger:
    """Initializes multi-GPU-friendly python logger."""
    from pytorch_lightning.utilities import rank_zero_only

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # this ensures all logging levels get marked with the rank zero decorator
    # otherwise logs would get multiplied for each GPU process in multi-GPU setup
    for level in ("debug", "info", "warning", "error", "exception", "fatal", "critical"):
        setattr(logger, level, rank_zero_only(getattr(logger, level)))

    return logger


log = get_logger(__name__)


def no_op(*args, **kwargs):
    pass


def identity(X, *args, **kwargs):
    return X


def get_identity_callable(*args, **kwargs) -> Callable:
    return identity


def exists(x):
    return x is not None


def default(val, d):
    if val is not None:
        return val
    return d() if isfunction(d) else d


distribution_params_to_edit = ["loc", "scale"]


def torch_to_numpy(x: Union[Tensor, Dict[str, Tensor]]) -> Union[np.ndarray, Dict[str, np.ndarray]]:
    if isinstance(x, Tensor):
        return x.detach().cpu().numpy()
    elif isinstance(x, TensorDictBase):
        return {k: torch_to_numpy(v) for k, v in x.items()}
        # return x.detach().cpu()   # numpy() not implemented for TensorDict
    elif isinstance(x, dict):
        return {k: torch_to_numpy(v) for k, v in x.items()}
    elif isinstance(x, torch.distributions.Distribution):
        # only move the parameters to cpu
        for k in distribution_params_to_edit:
            if hasattr(x, k):
                setattr(x, k, getattr(x, k).detach().cpu())
        return x
    else:
        return x


def numpy_to_torch(x: Union[np.ndarray, Dict[str, np.ndarray]]) -> Union[Tensor, Dict[str, Tensor]]:
    if isinstance(x, np.ndarray):
        return torch.from_numpy(x)
    elif isinstance(x, dict):
        return {k: numpy_to_torch(v) for k, v in x.items()}
    # if it's a namedtuple, convert each element
    elif isinstance(x, tuple) and hasattr(x, "_fields"):
        return type(x)(*[numpy_to_torch(v) for v in x])
    elif torch.is_tensor(x):
        return x
    # if is simple int, float, etc., return as is
    elif isinstance(x, (int, float, str)):
        return x
    else:
        raise ValueError(f"Cannot convert {type(x)} to torch.")


def to_torch_and_device(x, device):
    x = x.values if isinstance(x, (xr.Dataset, xr.DataArray)) else x
    x = torch.from_numpy(x) if isinstance(x, np.ndarray) else x
    return x.to(device) if x is not None else None


def rrearrange(
    data: Union[Tensor, torch.distributions.Distribution, TensorDictBase],
    pattern: str,
    find_batch_size_max: bool = True,
    **axes_lengths,
):
    """Extend einops.rearrange to work with distributions."""
    if torch.is_tensor(data) or isinstance(data, np.ndarray):
        return rearrange(data, pattern, **axes_lengths)
    elif isinstance(data, torch.distributions.Distribution):
        dist_params = {
            k: rearrange(getattr(data, k), pattern, **axes_lengths)
            for k in distribution_params_to_edit
            if hasattr(data, k)
        }
        return type(data)(**dist_params)
    elif isinstance(data, TensorDictBase):
        new_data = {k: rrearrange(v, pattern, **axes_lengths) for k, v in data.items()}
        return to_tensordict(new_data, find_batch_size_max=find_batch_size_max)
    elif isinstance(data, dict):
        return {k: rrearrange(v, pattern, **axes_lengths) for k, v in data.items()}
    else:
        raise ValueError(f"Cannot rearrange {type(data)}")


def multiply_by_scalar(x: Union[Dict[str, Any], Any], scalar: float) -> Union[Dict[str, Any], Any]:
    """Multiplies the given scalar to the given scalar or dict."""
    if isinstance(x, dict):
        return {k: multiply_by_scalar(v, scalar) for k, v in x.items()}
    else:
        return x * scalar


def add(a, b):
    if isinstance(a, (TensorDictBase, dict)):
        return {key: add(a[key], b[key]) for key in a.keys()}
    else:
        return a + b


def subtract(a, b):
    if isinstance(a, (TensorDictBase, dict)):
        return {key: subtract(a[key], b[key]) for key in a.keys()}
    else:
        return a - b


def multiply(a, b):
    if isinstance(a, (TensorDictBase, dict)):
        return {key: multiply(a[key], b[key]) for key in a.keys()}
    else:
        return a * b


def divide(a, b):
    if isinstance(a, (TensorDictBase, dict)):
        return {key: divide(a[key], b[key]) for key in a.keys()}
    else:
        return a / b


def torch_select(input: Tensor, dim: int, index: int):
    """Extends torch.select to work with distributions."""
    if isinstance(input, torch.distributions.Distribution):
        dist_params = {
            k: torch.select(getattr(input, k), dim, index) for k in distribution_params_to_edit if hasattr(input, k)
        }
        return type(input)(**dist_params)
    else:
        return torch.select(input, dim, index)


def ellipsis_torch_dict_boolean_tensor(input_dict: TensorDictBase, mask: Tensor) -> TensorDictBase:
    """Ellipsis indexing for TensorDict with boolean mask as replacement for torch_dict[..., mask]"""
    if torch.is_tensor(input_dict):
        return input_dict[..., mask]
    # Simply doing [..., mask] will not work, we need to select with : as many times as the number of dimensions
    # in the input tensor (- the length of the mask shape)
    mask_len = len(mask.shape)
    ellipsis_str = (", :" * (len(input_dict.shape) - mask_len)).lstrip(", ")
    output_dict = dict()
    for k, v in input_dict.items():
        output_dict[k] = eval(f"v[{ellipsis_str}, mask]")
    log.info("shape value1=", list(output_dict.values())[0].shape)
    return to_tensordict(output_dict, find_batch_size_max=True)
    # TensorDict({k: eval(f"input[k][{ellipsis_str}, mask]") for k in input.keys()}, batch_size=mask.shape)


def extract_into_tensor(a, t, x_shape):
    """Extracts the values of tensor, a, at the given indices, t.
    Then, add dummy dimensions to broadcast to x_shape."""
    b, *_ = t.shape
    out = a.gather(-1, t)
    return out.reshape(b, *((1,) * (len(x_shape) - 1)))


def noise_like(shape, device, repeat=False):
    def repeat_noise():
        return torch.randn((1, *shape[1:]), device=device).repeat(shape[0], *((1,) * (len(shape) - 1)))

    def noise():
        return torch.randn(shape, device=device)

    return repeat_noise() if repeat else noise()


def get_activation_function(name: str, functional: bool = False, num: int = 1):
    """Returns the activation function with the given name."""
    name = name.lower().strip()

    def get_functional(s: str) -> Optional[Callable]:
        return {
            "softmax": F.softmax,
            "relu": F.relu,
            "tanh": torch.tanh,
            "sigmoid": torch.sigmoid,
            "identity": nn.Identity(),
            None: None,
            "swish": F.silu,
            "silu": F.silu,
            "elu": F.elu,
            "gelu": F.gelu,
            "prelu": nn.PReLU(),
        }[s]

    def get_nn(s: str) -> Optional[Callable]:
        return {
            "softmax": nn.Softmax(dim=1),
            "relu": nn.ReLU(),
            "tanh": nn.Tanh(),
            "sigmoid": nn.Sigmoid(),
            "identity": nn.Identity(),
            "silu": nn.SiLU(),
            "elu": nn.ELU(),
            "prelu": nn.PReLU(),
            "swish": nn.SiLU(),
            "gelu": nn.GELU(),
        }[s]

    if num == 1:
        return get_functional(name) if functional else get_nn(name)
    else:
        return [get_nn(name) for _ in range(num)]


def get_normalization_layer(name, dims, num_groups=None, *args, **kwargs):
    """Returns the normalization layer with the given name.

    Args:
        name: name of the normalization layer. Must be one of ['batch_norm', 'layer_norm' 'group', 'instance', 'none']
    """
    if not isinstance(name, str) or name.lower() == "none":
        return None
    elif "batch_norm" == name:
        return nn.BatchNorm2d(num_features=dims, *args, **kwargs)
    elif "layer_norm" == name:
        return nn.LayerNorm(dims, *args, **kwargs)
    elif "rms_layer_norm" == name:
        from src.utilities.normalization import RMSLayerNorm

        return RMSLayerNorm(dims, *args, **kwargs)
    elif "instance" in name:
        return nn.InstanceNorm1d(num_features=dims, *args, **kwargs)
    elif "group" in name:
        if num_groups is None:
            # find an appropriate divisor (not robust against weird dims!)
            pos_groups = [int(dims / N) for N in range(2, 17) if dims % N == 0]
            if len(pos_groups) == 0:
                raise NotImplementedError(f"Group norm could not infer the number of groups for dim={dims}")
            num_groups = max(pos_groups)
        return nn.GroupNorm(num_groups=num_groups, num_channels=dims)
    else:
        raise ValueError("Unknown normalization name", name)


def count_params(model, verbose=False):
    total_params = sum(p.numel() for p in model.parameters())
    if verbose:
        log.info(f"{model.__class__.__name__} has {total_params * 1.e-6:.2f} M params.")
    return total_params


def to_dict(obj: Optional[Union[dict, SimpleNamespace]]):
    if obj is None:
        return dict()
    elif isinstance(obj, dict):
        return obj
    else:
        return vars(obj)


def to_DictConfig(obj: Optional[Union[List, Dict]]):
    """Tries to convert the given object to a DictConfig."""
    if isinstance(obj, DictConfig):
        return obj

    if isinstance(obj, list):
        try:
            dict_config = OmegaConf.from_dotlist(obj)
        except ValueError:
            dict_config = OmegaConf.create(obj)

    elif isinstance(obj, dict):
        dict_config = OmegaConf.create(obj)

    else:
        dict_config = OmegaConf.create()  # empty

    return dict_config


def get_dotted_key_from_dict(d: dict, key: str):
    """Returns the value from the given dictionary with the given key, which can be a dotted key."""
    keys = key.split(".")
    value = d
    for k in keys:
        if k not in value:
            return None
        value = value[k]
    return value


def keep_dict_or_tensordict(new_dict_like: dict, original: Union[Dict, TensorDictBase]) -> Union[Dict, TensorDictBase]:
    """Returns the given object if it is a dict or TensorDict, otherwise returns an empty dict."""
    if isinstance(original, TensorDictBase):
        # Return class of original
        return type(original)(new_dict_like, batch_size=original.batch_size)
    elif isinstance(original, dict):
        return new_dict_like
    else:
        raise ValueError(f"Expected a dict or TensorDict, but got {type(original)}")


def replace_substrings(string: str, replacements: Dict[str, str], ignore_case: bool = False):
    """
    Given a string and a replacement map, it returns the replaced string.
    :param str string: string to execute replacements on
    :param dict replacements: replacement dictionary {value to find: value to replace}
    :param bool ignore_case: whether the match should be case-insensitive
    :rtype: str
    """
    if not replacements:
        # Edge case that'd produce a funny regex and cause a KeyError
        return string

    # If case-insensitive, we need to normalize the old string so that later a replacement
    # can be found. For instance with {"HEY": "lol"} we should match and find a replacement for "hey",
    # "HEY", "hEy", etc.
    if ignore_case:

        def normalize_old(s):
            return s.lower()

        re_mode = re.IGNORECASE

    else:

        def normalize_old(s):
            return s

        re_mode = 0

    replacements = {normalize_old(key): val for key, val in replacements.items()}

    # Place longer ones first to keep shorter substrings from matching where the longer ones should take place
    # For instance given the replacements {'ab': 'AB', 'abc': 'ABC'} against the string 'hey abc', it should produce
    # 'hey ABC' and not 'hey ABc'
    rep_sorted = sorted(replacements, key=len, reverse=True)
    rep_escaped = map(re.escape, rep_sorted)

    # Create a big OR regex that matches any of the substrings to replace
    pattern = re.compile("|".join(rep_escaped), re_mode)

    # For each match, look up the new string in the replacements, being the key the normalized old string
    return pattern.sub(lambda match: replacements[normalize_old(match.group(0))], string)


#####
# The following two functions extend setattr and getattr to support chained objects, e.g. rsetattr(cfg, optim.lr, 1e-4)
# From https://stackoverflow.com/questions/31174295/getattr-and-setattr-on-nested-subobjects-chained-properties
def rsetattr(obj, attr, val):
    pre, _, post = attr.rpartition(".")
    return setattr(rgetattr(obj, pre) if pre else obj, post, val)


def rgetattr(obj, attr, *args):
    def _getattr(obj, attr):
        return getattr(obj, attr, *args)

    return functools.reduce(_getattr, [obj] + attr.split("."))


def rhasattr(obj, attr, *args):
    def _hasattr(obj, attr):
        return hasattr(obj, attr, *args)

    return functools.reduce(_hasattr, [obj] + attr.split("."))


def to_tensordict(
    x: Dict[str, torch.Tensor], find_batch_size_max: bool = False, force_same_device: bool = False, device=None
) -> TensorDict:
    """Converts a dictionary of tensors to a TensorDict."""
    if torch.is_tensor(x):
        return x
    elif isinstance(x, np.ndarray):
        return torch.from_numpy(x)
    any_batch_example = x[list(x.keys())[0]]
    device = any_batch_example.device if force_same_device else device
    shared_batch_size = any_batch_example.shape
    if find_batch_size_max:
        # Find maximum number of dimensions that are the same for all tensors
        for t in x.values():
            if t.shape[: len(shared_batch_size)] != shared_batch_size:
                # Find the maximum number of dimensions that are the same for all tensors
                for i, (a, b) in enumerate(zip(t.shape, shared_batch_size)):
                    if a != b:
                        shared_batch_size = shared_batch_size[:i]
                        break
    return TensorDict(x, batch_size=shared_batch_size, device=device)


# Errors
def raise_error_if_invalid_value(value: Any, possible_values: Sequence[Any], name: str = None):
    """Raises an error if the given value (optionally named by `name`) is not one of the possible values."""
    if value not in possible_values:
        name = name or (value.__name__ if hasattr(value, "__name__") else "value")
        raise ValueError(f"{name} must be one of {possible_values}, but was {value} (type={type(value)})")
    return value


def raise_error_if_has_attr_with_invalid_value(obj: Any, attr: str, possible_values: Sequence[Any]):
    if hasattr(obj, attr):
        raise_error_if_invalid_value(getattr(obj, attr), possible_values, name=f"{obj.__class__.__name__}.{attr}")


def raise_error_if_invalid_type(value: Any, possible_types: Sequence[Any], name: str = None):
    """Raises an error if the given value (optionally named by `name`) is not one of the possible types."""
    if all([not isinstance(value, t) for t in possible_types]):
        name = name or (value.__name__ if hasattr(value, "__name__") else "value")
        raise ValueError(f"{name} must be an instance of either of {possible_types}, but was {type(value)}")
    return value


def raise_if_invalid_shape(
    value: Union[np.ndarray, Tensor],
    expected_shape: Sequence[int] | int,
    axis: int = None,
    name: str = None,
):
    if isinstance(expected_shape, int):
        if value.shape[axis] != expected_shape:
            name = name or (value.__name__ if hasattr(value, "__name__") else "value")
            raise ValueError(f"{name} must have shape {expected_shape} along axis {axis}, but shape={value.shape}")
    else:
        if value.shape != expected_shape:
            name = name or (value.__name__ if hasattr(value, "__name__") else "value")
            raise ValueError(f"{name} must have shape {expected_shape}, but was {value.shape}")


class AlreadyLoggedError(Exception):
    pass


# allow checkpointing via USR1
def melk(trainer, ckptdir: str):
    def actual_melk(*args, **kwargs):
        # run all checkpoint hooks
        if trainer.global_rank == 0:
            log.info("Summoning checkpoint.")
            # log.info("Is file: last.ckpt ?", os.path.isfile(os.path.join(ckptdir, "last.ckpt")))
            ckpt_path = os.path.join(ckptdir, "last.ckpt")
            trainer.save_checkpoint(ckpt_path)

    return actual_melk


def divein(trainer):
    def actual_divein(*args, **kwargs):
        if trainer.global_rank == 0:
            import pudb

            pudb.set_trace()

    return actual_divein


# Random seed (if not using pytorch-lightning)
def set_seed(seed, device="cuda"):
    """
    Sets the random seed for the given device.
    If using pytorch-lightning, preferably to use pl.seed_everything(seed) instead.
    """
    # setting seeds
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if device != "cpu":
        torch.cuda.manual_seed(seed)


def auto_gpu_selection(
    usage_max: float = 0.2,
    mem_max: float = 0.6,
    num_gpus: int = 1,
    raise_error_if_insufficient_gpus: bool = True,
    verbose: bool = False,
):
    """Auto set CUDA_VISIBLE_DEVICES for gpu  (based on utilization)

    Args:
        usage_max: max percentage of GPU memory
        mem_max: max percentage of GPU utility
        num_gpus: number of GPUs to use
        raise_error_if_insufficient_gpus: raise error if no (not enough) GPU is available
    """
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    try:
        log_output = str(subprocess.check_output("nvidia-smi", shell=True)).split(r"\n")[6:-1]
    except subprocess.CalledProcessError as e:
        print(
            f"Error with code {e.returncode}. There's likely an issue with nvidia-smi."
            f" Returning without setting CUDA_VISIBLE_DEVICES"
        )
        return

    # Maximum of GPUS, 8 is enough for most
    gpu_to_utilization, gpu_to_mem = dict(), dict()
    gpus_available = torch.cuda.device_count()
    gpu_to_usage = dict()
    for gpu in range(gpus_available):
        idx = gpu * 4 + 3
        if idx > log_output.__len__() - 1:
            break
        inf = log_output[idx].split("|")
        if inf.__len__() < 3:
            break

        try:
            usage = int(inf[3].split("%")[0].strip())
        except ValueError:
            print("Error with code. Returning without setting CUDA_VISIBLE_DEVICES")
            return
        mem_now = int(str(inf[2].split("/")[0]).strip()[:-3])
        mem_all = int(str(inf[2].split("/")[1]).strip()[:-3])

        gpu_to_usage[gpu] = f"Memory:[{mem_now}/{mem_all}MiB] , GPU-Util:[{usage}%]"
        if usage < 100 * usage_max and mem_now < mem_max * mem_all:
            gpu_to_utilization[gpu] = usage
            gpu_to_mem[gpu] = mem_now
            # os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu)
            if verbose:
                log.info(f"GPU {gpu} is vacant: Memory:[{mem_now}/{mem_all}MiB] , GPU-Util:[{usage}%]")
        else:
            if verbose:
                log.info(
                    f"GPU {gpu} is busy: Memory:[{mem_now}/{mem_all}MiB] , GPU-Util:[{usage}%] (> {usage_max * 100}%)"
                )

    if len(gpu_to_utilization) >= num_gpus:
        least_utilized_gpus = sorted(gpu_to_utilization, key=gpu_to_utilization.get)[:num_gpus]
        sorted(gpu_to_mem, key=gpu_to_mem.get)[:num_gpus]
        if len(gpu_to_utilization) == 1:
            os.environ["CUDA_VISIBLE_DEVICES"] = str(least_utilized_gpus[0])
        else:
            os.environ["CUDA_VISIBLE_DEVICES"] = ",".join([str(gpu) for gpu in least_utilized_gpus])
        log.info(f"Set os.environ['CUDA_VISIBLE_DEVICES'] = {os.environ['CUDA_VISIBLE_DEVICES']}")
        for gpu in least_utilized_gpus:
            log.info(f"Use GPU {gpu} with utilization {gpu_to_usage[gpu]}")
        if num_gpus > 1:
            log.info(f"Use GPUs {least_utilized_gpus} based on least utilization")
    else:
        if raise_error_if_insufficient_gpus:
            raise ValueError("No vacant GPU")
        log.info("\nNo vacant GPU, use CPU instead\n")
        os.environ["CUDA_VISIBLE_DEVICES"] = "-1"


def print_gpu_memory_usage(
    prefix: str = "",
    tqdm_bar=None,
    add_description: bool = True,
    keep_old: bool = False,
    empty_cache: bool = False,
    log_func: Optional[Callable] = None,
):
    """Use this function to print the GPU memory usage (logged or in a tqdm bar).
    Use this to narrow down memory leaks, by printing the GPU memory usage before and after a function call
    and checking if the available memory is the same or not.
    Recommended to use with 'empty_cache=True' to get the most accurate results during debugging.
    """
    if torch.cuda.is_available():
        if empty_cache:
            torch.cuda.empty_cache()
        used, allocated = torch.cuda.mem_get_info()
        prefix = f"{prefix} GPU mem free/allocated" if add_description else prefix
        info_str = f"{prefix} {used / 1e9:.2f}/{allocated / 1e9:.2f}GB"
        if tqdm_bar is not None:
            if keep_old:
                tqdm_bar.set_postfix_str(f"{tqdm_bar.postfix} | {info_str}")
            else:
                tqdm_bar.set_postfix_str(info_str)
        elif log_func is not None:
            log_func(info_str)
        else:
            log.info(info_str)


def get_pl_trainer_kwargs_for_evaluation(
    trainer_config: DictConfig = None,
) -> (Dict[str, Any], torch.device):
    """Get kwargs for pytorch-lightning Trainer for evaluation and select <=1 GPU if available"""
    # GPU or not:
    if torch.cuda.is_available() and (trainer_config is None or trainer_config.accelerator != "cpu"):
        accelerator, devices, reload_to_device = "gpu", 1, torch.device("cuda:0")
        auto_gpu_selection(usage_max=0.6, mem_max=0.75, num_gpus=devices)
    else:
        accelerator, devices, reload_to_device = "cpu", "auto", torch.device("cpu")
    return dict(accelerator=accelerator, devices=devices, strategy="auto"), reload_to_device


def infer_main_batch_key_from_dataset(dataset: torch.utils.data.Dataset) -> str:
    ds = dataset
    main_data_key = None
    if hasattr(ds, "main_data_key"):
        main_data_key = ds.main_data_key
    else:
        data_example = ds[0]
        if isinstance(data_example, dict):
            if "dynamics" in data_example:
                main_data_key = "dynamics"
            elif "data" in data_example:
                main_data_key = "data"
            else:
                raise ValueError(f"Could not determine main_data_key from data_example: {data_example.keys()}")
    return main_data_key


def rename_state_dict_keys(state_dict: Dict[str, torch.Tensor]) -> (Dict[str, torch.Tensor], bool):
    #  Missing key(s) in state_dict: "model.downs.0.2.fn.fn.to_qkv.1.weight", "model.downs.1.2.fn.fn.to_qkv.1.weight",
    #  Unexpected key(s) in state_dict: "model.downs.0.2.fn.fn.to_qkv.weight", "model.downs.1.2.fn.fn.to_qkv.weight",
    # rename weights
    renamed = False
    for k in list(state_dict.keys()):
        if "fn.to_qkv.weight" in k and "mid_attn" not in k:
            state_dict[k.replace("fn.to_qkv.weight", "fn.to_qkv.1.weight")] = state_dict.pop(k)
            renamed = True

    return state_dict, renamed


def rename_state_dict_keys_and_save(torch_model_state, ckpt_path: str) -> Dict[str, torch.Tensor]:
    """Renames the state dict keys and saves the renamed state dict back to the checkpoint."""
    state_dict, has_been_renamed = rename_state_dict_keys(torch_model_state["state_dict"])
    if has_been_renamed:
        # Save the renamed model state
        torch_model_state["state_dict"] = state_dict
        torch.save(torch_model_state, ckpt_path)
    return state_dict


def freeze_model(model: nn.Module):
    for param in model.parameters():
        param.requires_grad = False
    model.eval()  # set to eval mode
    return model


all_dropout_layers = [nn.Dropout, nn.Dropout2d, nn.Dropout3d, nn.AlphaDropout, nn.FeatureAlphaDropout, DropPath]


def enable_inference_dropout(model: nn.Module):
    """Set all dropout layers to training mode"""
    # find all dropout layers
    dropout_layers = [m for m in model.modules() if any([isinstance(m, layer) for layer in all_dropout_layers])]
    for layer in dropout_layers:
        layer.train()
    # assert all([layer.training for layer in [m for m in model.modules() if isinstance(m, nn.Dropout)]])


def disable_inference_dropout(model: nn.Module):
    """Set all dropout layers to eval mode"""
    # find all dropout layers
    dropout_layers = [m for m in model.modules() if any([isinstance(m, layer) for layer in all_dropout_layers])]
    for layer in dropout_layers:
        layer.eval()


def find_differences_between_dicts(d1: Dict[str, Any], d2: Dict[str, Any]) -> List[str]:
    """Finds any (nested) differences between the two dictionaries."""
    diff = []
    for k, v in d1.items():
        d2_v = d2.get(k)
        if not isinstance(v, dict) and d2_v != v:
            diff.append(f"{k}: {v} != {d2_v}")
        elif isinstance(v, dict):
            diff += find_differences_between_dicts(v, d2_v)
    return diff


def update_dict_with_other(d1: Dict[str, Any], other: Dict[str, Any]):  # _and_return_difference
    """Updates d1 with other, other can be a dict of dicts with partial updates.

    Returns:
        d1: the updated dict
        diff: the difference between the original d1 and the updated d1 as a string

    Example:
        d1 = {'a': {'b': 1, 'c': 2}, 'x': 99}
        other = {'a': {'b': 3}, 'y': 100}
        d1, diff = update_dict_with_other(d1, other)
        log.info(d1)
        # {'a': {'b': 3, 'c': 2}, 'x': 99, 'y': 100}
        log.info(diff)
        # ['a.b: 1 -> 3', 'y: None -> 100']
    """
    diff = []
    for k, v in other.items():
        if isinstance(v, dict) and d1.get(k) is not None:
            d1[k], diff_sub = update_dict_with_other(d1.get(k, {}), v)
            diff += [f"{k}.{x}" for x in diff_sub]
        else:
            if d1.get(k) != v:
                diff.append(f"{k}: {d1.get(k, None)} -> {v}")
            d1[k] = v
    return d1, diff


def flatten_dict(dictionary: Dict[Any, Any], save: bool = True) -> Dict[Any, Any]:
    """Flattens a nested dict."""
    # The dictionary may consist of dicts or normal values
    # If it's a dict, recursively flatten it
    # If it's a normal value, return it
    flattened = {}
    for k, v in dictionary.items():
        if isinstance(v, (dict, TensorDictBase)):
            # check that no duplicate keys exist
            flattened_v = flatten_dict(v)
            if save and len(set(flattened_v.keys()).intersection(set(flattened.keys()))) > 0:
                raise ValueError(f"Duplicate keys in flattened dict: {set(flattened_v.keys())}")
            flattened.update(flattened_v)
        else:
            if save and k in flattened:
                raise ValueError(f"Duplicate keys in flattened dict: {k}")
            flattened[k] = v
    return flattened


def find_config_differences(
    configs: List[Dict[str, Any]],
    keys_to_tolerated_percent_diff: Dict[str, float] = None,
    sort_by_name: bool = True,
) -> List[List[str]]:
    """
    Find and return the differences between multiple nested configurations.

    This function compares each configuration with all others and identifies
    keys that have different values across configurations. It returns a list
    of differences for each input configuration.

    Args:
        configs (List[Dict[str, Any]]): A list of nested configuration dictionaries.
        keys_to_tolerated_percent_diff (Dict[str, float], optional): A dictionary mapping keys to maximum tolerated differences. Any keys not included in this dictionary will be compared for exact equality. Defaults to None.
        sort_by_name (bool, optional): Whether to sort the output by key names.
                                       Defaults to True.

    Returns:
        List[List[str]]: A list containing lists of strings, where each inner list
                         represents the differences for one configuration in the
                         format ["key=value", ...].

    Example:
        configs = [
            {"a": 1, "b": {"c": 2, "d": 3}},
            {"a": 1, "b": {"c": 2, "d": 4}},
            {"a": 2, "b": {"c": 2, "d": 3}, "e": 5}
        ]
        result = find_config_differences(configs)
        # Result will be:
        # [['a=1', 'b.d=3'], ['b.d=4'], ['a=2', 'e=5']]
    """

    def flatten_dict(d: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
        """
        Recursively flatten a nested dictionary, using dot notation for nested keys.

        Args:
            d (Dict[str, Any]): The dictionary to flatten.
            prefix (str, optional): The prefix to use for the current level of nesting.

        Returns:
            Dict[str, Any]: A flattened version of the input dictionary.
        """
        items = []
        for k, v in d.items():
            new_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                # If the value is a dictionary, recurse with the new key as prefix
                items.extend(flatten_dict(v, new_key).items())
            else:
                # If the value is not a dictionary, add it to the items list
                items.append((new_key, v))
        return dict(items)

    # Flatten all input configurations
    flat_configs = [flatten_dict(config) for config in configs]

    # Get all unique keys from all configurations
    all_keys = set().union(*flat_configs)

    # Sort keys if sort_by_name is True
    if sort_by_name:
        all_keys = sorted(all_keys)

    keys_to_tolerated_percent_diff = keys_to_tolerated_percent_diff or dict()
    assert all(
        [0 <= value <= 1 for value in keys_to_tolerated_percent_diff.values()]
    ), "Values in keys_to_tolerated_percent_diff must be between 0 and 1"
    differences = []
    for i, config in enumerate(flat_configs):
        diff = []
        for key in all_keys:
            # Check if the key exists in the current config and has a different value in any other config
            value = config.get(key)
            if value is not None:
                # Check if the key is in the keys_to_percent_diff dictionary
                if key in keys_to_tolerated_percent_diff.keys():
                    if any(
                        abs(value - other.get(key, value)) > keys_to_tolerated_percent_diff[key] * abs(value)
                        for other in flat_configs
                    ):
                        diff.append(f"{key}={config[key]}")
                        # print(f"key={key}, value={value}. diff={[abs(value - other.get(key, value)) for other in flat_configs]}. tol={keys_to_tolerated_percent_diff[key] * abs(value)}")
                elif any(config[key] != other.get(key) for other in flat_configs):
                    diff.append(f"{key}={config[key]}")
        differences.append(diff)

    return differences


def find_config_differences_return_as_joined_str(
    configs: List[Dict[str, Any]], join_with: str = " ", **kwargs
) -> List[str]:
    differences_list = find_config_differences(configs, **kwargs)
    return [join_with.join(differences) for differences in differences_list]


def concatenate_array_dicts(
    arrays_dicts: List[Dict[str, np.ndarray]],
    axis: int = 0,
    keys: List[str] = None,
) -> Dict[str, np.ndarray]:
    """Concatenates the given dicts of arrays along the given axis. The dict may be nested.
    Args:
        arrays_dicts: A list of a
    """
    if len(arrays_dicts) == 0:
        return dict()

    if keys is None:
        # Check that all dicts have the same keys
        keys = arrays_dicts[0].keys()
        for d in arrays_dicts:
            if d.keys() != keys:
                raise ValueError(f"Keys of dicts do not match: {d.keys()} != {keys}")
    else:
        keys = [keys] if isinstance(keys, str) else keys

    # Concatenate the arrays
    concatenated = {}
    for k in keys:
        if isinstance(arrays_dicts[0][k], dict):
            concatenated[k] = concatenate_array_dicts([d[k] for d in arrays_dicts], axis=axis)
        else:
            concatenated[k] = np.concatenate([d[k] for d in arrays_dicts], axis=axis)
    return concatenated


def get_first_array_in_nested_dict(nested_dict: Dict[str, Any]):
    """Returns the first array that is found when descending the hierarchy in the given nested dict."""
    for v in nested_dict.values():
        if isinstance(v, dict):
            return get_first_array_in_nested_dict(v)
        elif isinstance(v, (np.ndarray, Tensor)):
            return v
    raise ValueError(f"Could not find any array in the given nested dict: {nested_dict}")


def split3d_and_merge_variables(results_dict, level_names) -> Dict[str, Any]:
    """"""
    if level_names is None:
        return results_dict
    keys3d = [k for k in results_dict.keys() if "3d" in k]
    # results_dicts = dict()
    assert len(keys3d) == 1, f"Expected only one 3d key, but got {keys3d}"
    for k in keys3d:
        data3d = results_dict.pop(k)
        results_dict[k] = dict()
        for variable in list(data3d.keys()):
            var_data = data3d.pop(variable)
            n_levels = var_data.shape[-3]
            for i in range(n_levels):
                new_k = f"{variable}_{level_names[i]}"
                results_dict[k][new_k] = torch_select(var_data, dim=-3, index=i)
        results_dict = flatten_dict(results_dict)
        # results_dicts[k_base] = flatten_dict(results_dict)\
    return results_dict


def split3d_and_merge_variables_maybe(
    results_dict, result_key: str, multiple_result_keys: List[str], level_names: List[str]
) -> Dict[str, Any]:
    if result_key in results_dict.keys():
        return results_dict[result_key]
    elif all([k in results_dict.keys() for k in multiple_result_keys]):
        return split3d_and_merge_variables({k: results_dict[k] for k in multiple_result_keys}, level_names)
    else:
        raise ValueError(
            f"Could not find any of the given keys in the results_dict: {result_key}, {multiple_result_keys}"
        )


def get_common_substrings(strings, min_length):
    common_substrings = set()

    # Compare each pair of strings to find common substrings
    for i in range(len(strings)):
        for j in range(i + 1, len(strings)):
            s1, s2 = strings[i], strings[j]
            seq_matcher = SequenceMatcher(None, s1, s2)

            for match in seq_matcher.get_matching_blocks():
                if match.size >= min_length:
                    common_substrings.add(s1[match.a : match.a + match.size])

    return common_substrings


def remove_substrings(strings, substrings):
    result = []
    strings_list = [strings] if isinstance(strings, str) else strings
    for string in strings_list:
        for substring in substrings:
            string = string.replace(substring, "")
        result.append(string)
    if isinstance(strings, str):
        return result[0]
    return result


def remove_common_substrings(strings, min_length: int = 5):
    common_substrings = get_common_substrings(strings, min_length)
    return remove_substrings(strings, common_substrings)
