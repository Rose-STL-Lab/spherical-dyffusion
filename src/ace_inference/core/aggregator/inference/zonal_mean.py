from typing import Dict, Mapping, Optional

import torch

from src.ace_inference.core.data_loading.data_typing import VariableMetadata
from src.ace_inference.core.device import get_device
from src.ace_inference.core.distributed import Distributed
from src.ace_inference.core.wandb import WandB


wandb = WandB.get_instance()


class ZonalMeanAggregator:
    """Images of the zonal-mean state as a function of latitude and time.

    This aggregator keeps track of the generated and target zonal-mean state,
    then generates zonal-mean (Hovmoller) images when logs are retrieved.
    The zonal-mean images are averaged across the sample dimension.
    """

    _captions = {
        "error": (
            "{name} zonal-mean error (generated - target) [{units}], "
            "x-axis is time increasing to right, y-axis is latitude increasing upward"
        ),
        "gen": (
            "{name} zonal-mean generated [{units}], "
            "x-axis is time increasing to right, y-axis is latitude increasing upward"
        ),
    }

    def __init__(
        self,
        n_timesteps: int,
        dist: Optional[Distributed] = None,
        metadata: Optional[Mapping[str, VariableMetadata]] = None,
    ):
        """
        Args:
            n_timesteps: Number of timesteps of inference that will be run.
            dist: Distributed object to use for communication.
            metadata: Mapping of variable names their metadata that will
                used in generating logged image captions.
        """
        self._n_timesteps = n_timesteps
        if dist is None:
            self._dist = Distributed.get_instance()
        else:
            self._dist = dist
        if metadata is None:
            self._metadata: Mapping[str, VariableMetadata] = {}
        else:
            self._metadata = metadata

        self._target_data: Optional[Dict[str, torch.Tensor]] = None
        self._gen_data: Optional[Dict[str, torch.Tensor]] = None
        self._n_batches = torch.zeros(n_timesteps, dtype=torch.int32, device=get_device())[
            None, :, None
        ]  # sample, time, lat

    def record_batch(
        self,
        loss: float,
        target_data: Mapping[str, torch.Tensor],
        gen_data: Mapping[str, torch.Tensor],
        target_data_norm: Mapping[str, torch.Tensor],
        gen_data_norm: Mapping[str, torch.Tensor],
        i_time_start: int,
    ):
        lon_dim = 3
        if self._target_data is None:
            self._target_data = self._initialize_zeros_zonal_mean_from_batch(target_data, self._n_timesteps)
        if self._gen_data is None:
            self._gen_data = self._initialize_zeros_zonal_mean_from_batch(gen_data, self._n_timesteps)

        window_steps = next(iter(target_data.values())).shape[1]
        time_slice = slice(i_time_start, i_time_start + window_steps)
        # we can average along longitude without area weighting
        for name, tensor in target_data.items():
            self._target_data[name][:, time_slice, :] += tensor.mean(dim=lon_dim)
        for name, tensor in gen_data.items():
            self._gen_data[name][:, time_slice, :] += tensor.mean(dim=lon_dim)
        self._n_batches[:, time_slice, :] += 1

    def get_logs(self, label: str) -> Dict[str, torch.Tensor]:
        if self._gen_data is None or self._target_data is None:
            raise RuntimeError("No data recorded")
        sample_dim = 0
        logs = {}
        for name in self._gen_data.keys():
            zonal_means = {}
            gen = self._dist.reduce_mean(self._gen_data[name] / self._n_batches)
            zonal_means["gen"] = gen.mean(dim=sample_dim).cpu()
            error = self._dist.reduce_mean((self._gen_data[name] - self._target_data[name]) / self._n_batches)
            zonal_means["error"] = error.mean(dim=sample_dim).cpu()
            for key, data in zonal_means.items():
                caption = self._get_caption(key, name, data)
                # images are y, x from upper left corner
                # data is time, lat
                # we want lat on y-axis (increasing upward) and time on x-axis
                # so transpose and flip along lat axis
                data = data.t().flip(dims=[0])
                wandb_image = wandb.Image(data, caption=caption)
                logs[f"{label}/{key}/{name}"] = wandb_image
        return logs

    def _get_caption(self, caption_key: str, varname: str, data: torch.Tensor) -> str:
        if varname in self._metadata:
            caption_name = self._metadata[varname].long_name
            units = self._metadata[varname].units
        else:
            caption_name, units = varname, "unknown_units"
        caption = self._captions[caption_key].format(name=caption_name, units=units)
        caption += f" vmin={data.min():.4g}, vmax={data.max():.4g}."
        return caption

    @staticmethod
    def _initialize_zeros_zonal_mean_from_batch(
        data: Mapping[str, torch.Tensor], n_timesteps: int, lat_dim: int = 2
    ) -> Dict[str, torch.Tensor]:
        return {
            name: torch.zeros(
                (tensor.shape[0], n_timesteps, tensor.shape[lat_dim]),
                dtype=tensor.dtype,
                device=tensor.device,
            )
            for name, tensor in data.items()
        }
