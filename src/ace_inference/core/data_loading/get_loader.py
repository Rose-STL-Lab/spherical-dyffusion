from pathlib import Path
from typing import Optional

import numpy as np
import torch.utils.data
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler

from src.ace_inference.core.device import using_gpu
from src.ace_inference.core.distributed import Distributed

from ._xarray import XarrayDataset
from .data_typing import Dataset, GriddedData
from .params import DataLoaderParams
from .requirements import DataRequirements


def _all_same(iterable, cmp=lambda x, y: x == y):
    it = iter(iterable)
    try:
        first = next(it)
    except StopIteration:
        return True
    return all(cmp(first, rest) for rest in it)


def _get_ensemble_dataset(
    params: DataLoaderParams,
    requirements: DataRequirements,
    window_time_slice: Optional[slice] = None,
    sub_paths: Optional[list] = None,
    dataset_class: Optional[Dataset] = None,
    **kwargs,
) -> Dataset:
    """Returns a dataset that is a concatenation of the datasets for each
    ensemble member.
    """
    dataset_class = dataset_class or XarrayDataset
    if sub_paths is not None:
        sub_paths = [sub_paths] if isinstance(sub_paths, str) else sub_paths
        paths = [str(Path(params.data_path) / sub_path) for sub_path in sub_paths]
    else:
        # Get all subdirectories of the data path
        paths = sorted([str(d) for d in Path(params.data_path).iterdir() if d.is_dir()])

    datasets, metadatas, sigma_coords = [], [], []
    samples_per_member = params.n_samples // len(paths) if params.n_samples else None
    for path in paths:
        params_curr_member = DataLoaderParams(
            path, params.data_type, params.batch_size, params.num_data_workers, samples_per_member
        )
        dataset = dataset_class(params_curr_member, requirements, window_time_slice=window_time_slice, **kwargs)

        datasets.append(dataset)
        metadatas.append(dataset.metadata)
        sigma_coords.append(dataset.sigma_coordinates)

    if not _all_same(metadatas):
        raise ValueError("Metadata for each ensemble member should be the same.")

    ak, bk = list(zip(*[(s.ak.cpu().numpy(), s.bk.cpu().numpy()) for s in sigma_coords]))
    if not (_all_same(ak, cmp=np.allclose) and _all_same(bk, cmp=np.allclose)):
        raise ValueError("Sigma coordinates for each ensemble member should be the same.")

    ensemble = torch.utils.data.ConcatDataset(datasets)
    ensemble.metadata = metadatas[0]  # type: ignore
    ensemble.area_weights = datasets[0].area_weights  # type: ignore
    ensemble.sigma_coordinates = datasets[0].sigma_coordinates  # type: ignore
    ensemble.horizontal_coordinates = datasets[0].horizontal_coordinates  # type: ignore
    return ensemble


def get_data_loader(
    params: DataLoaderParams,
    train: bool,
    requirements: DataRequirements,
    window_time_slice: Optional[slice] = None,
    dist: Optional[Distributed] = None,
) -> GriddedData:
    """
    Args:
        params: Parameters for the data loader.
        train: Whether to use the training or validation data.
        requirements: Data requirements for the model.
        window_time_slice: Time slice within each window to use for the data loader,
            if given the loader will only return data from this time slice.
            By default it will return the full windows.
        dist: Distributed object to use for distributed training.
    """
    if dist is None:
        dist = Distributed.get_instance()
    if params.data_type == "xarray":
        dataset = XarrayDataset(params, requirements=requirements, window_time_slice=window_time_slice)
    elif params.data_type == "ensemble_xarray":
        dataset = _get_ensemble_dataset(params, requirements, window_time_slice=window_time_slice)
    else:
        raise NotImplementedError(f"{params.data_type} does not have an implemented data loader")

    sampler = DistributedSampler(dataset, shuffle=train) if dist.is_distributed() else None

    dataloader = DataLoader(
        dataset,
        batch_size=dist.local_batch_size(int(params.batch_size)),
        num_workers=params.num_data_workers,
        shuffle=(sampler is None) and train,
        sampler=sampler if train else None,
        drop_last=True,
        pin_memory=using_gpu(),
        persistent_workers=params.num_data_workers > 0,
    )

    return GriddedData(
        loader=dataloader,
        metadata=dataset.metadata,
        area_weights=dataset.area_weights,
        sampler=sampler,
        sigma_coordinates=dataset.sigma_coordinates,
        horizontal_coordinates=dataset.horizontal_coordinates,
    )
