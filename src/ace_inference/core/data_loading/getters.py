import dataclasses
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
from .inference import InferenceDataLoaderParams, InferenceDataset
from .params import DataLoaderParams, XarrayDataParams
from .requirements import DataRequirements
from .utils import BatchData


def _all_same(iterable, cmp=lambda x, y: x == y):
    it = iter(iterable)
    try:
        first = next(it)
    except StopIteration:
        return True
    return all(cmp(first, rest) for rest in it)


def _subset_dataset(dataset: Dataset, subset: slice) -> Dataset:
    """Returns a subset of the dataset and propagates other properties."""
    indices = range(len(dataset))[subset]
    subsetted_dataset = torch.utils.data.Subset(dataset, indices)
    subsetted_dataset.metadata = dataset.metadata
    subsetted_dataset.area_weights = dataset.area_weights
    subsetted_dataset.sigma_coordinates = dataset.sigma_coordinates
    subsetted_dataset.horizontal_coordinates = dataset.horizontal_coordinates
    return subsetted_dataset


def _get_ensemble_dataset(
    params: XarrayDataParams,
    requirements: DataRequirements,
    subset: slice,
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
    if len(paths) == 0:
        raise ValueError(
            f"No directories found in {params.data_path}. "
            "Check path and whether you meant to use 'ensemble_xarray' data_type."
        )
    datasets, metadatas, sigma_coords = [], [], []
    for path in paths:
        params_curr_member = dataclasses.replace(params, data_path=path)
        dataset = dataset_class(params_curr_member, requirements, **kwargs)
        dataset = _subset_dataset(dataset, subset)

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


def get_dataset(
    params: DataLoaderParams, requirements: DataRequirements, dataset_class: Optional[Dataset] = None, **kwargs
) -> Dataset:
    dataset_class = dataset_class or XarrayDataset
    if params.data_type == "xarray":
        dataset = dataset_class(params.dataset, requirements, **kwargs)
        dataset = _subset_dataset(dataset, params.subset.slice)
    elif params.data_type == "ensemble_xarray":
        dataset = _get_ensemble_dataset(
            params.dataset, requirements, params.subset.slice, dataset_class=dataset_class, **kwargs
        )
    else:
        raise NotImplementedError(f"{params.data_type} does not have an implemented data loader")
    return dataset


def get_data_loader(
    params: DataLoaderParams,
    train: bool,
    requirements: DataRequirements,
) -> GriddedData:
    """
    Args:
        params: Parameters for the data loader.
        train: Whether to use the training or validation data.
        requirements: Data requirements for the model.
        window_time_slice: Time slice within each window to use for the data loader,
            if given the loader will only return data from this time slice.
            By default it will return the full windows.
    """
    dataset = get_dataset(params, requirements)
    dist = Distributed.get_instance()
    sampler = DistributedSampler(dataset, shuffle=train) if dist.is_distributed() else None

    dataloader = DataLoader(
        dataset,
        batch_size=dist.local_batch_size(int(params.batch_size)),
        num_workers=params.num_data_workers,
        shuffle=(sampler is None) and train,
        sampler=sampler if train else None,
        drop_last=True,
        pin_memory=using_gpu(),
        collate_fn=BatchData.from_sample_tuples,
    )

    return GriddedData(
        loader=dataloader,
        metadata=dataset.metadata,
        area_weights=dataset.area_weights,
        sampler=sampler,
        sigma_coordinates=dataset.sigma_coordinates,
        horizontal_coordinates=dataset.horizontal_coordinates,
    )


def get_inference_data(
    config: InferenceDataLoaderParams,
    forward_steps_in_memory: int,
    requirements: DataRequirements,
) -> GriddedData:
    """
    Args:
        config: Parameters for the data loader.
        forward_steps_in_memory: Number of forward steps to keep in memory at once.
        requirements: Data requirements for the model.

    Returns:
        A data loader for inference with coordinates and metadata.
    """
    dataset = InferenceDataset(config, forward_steps_in_memory, requirements)
    # we roll our own batching in InferenceDataset, which is why batch_size=None below
    loader = DataLoader(
        dataset,
        batch_size=None,
        num_workers=config.num_data_workers,
        shuffle=False,
        pin_memory=using_gpu(),
    )
    return GriddedData(
        loader=loader,
        metadata=dataset.metadata,
        area_weights=dataset.area_weights,
        sigma_coordinates=dataset.sigma_coordinates,
        horizontal_coordinates=dataset.horizontal_coordinates,
    )
