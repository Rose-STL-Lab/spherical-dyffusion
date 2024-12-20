import logging
import os
from collections import namedtuple
from glob import glob
from typing import Callable, Dict, List, Mapping, Optional, Tuple

import numpy as np
import torch
import xarray as xr

from src.ace_inference.core import metrics
from src.ace_inference.core.data_loading.params import XarrayDataParams
from src.ace_inference.core.data_loading.requirements import DataRequirements
from src.ace_inference.core.data_loading.utils import get_lons_and_lats, get_times, load_series_data
from src.ace_inference.core.device import get_device
from src.ace_inference.core.winds import lon_lat_to_xyz

from .data_typing import (
    Dataset,
    HorizontalCoordinates,
    SigmaCoordinates,
    VariableMetadata,
)


VariableNames = namedtuple(
    "VariableNames",
    (
        "time_dependent_names",
        "time_invariant_names",
        "static_derived_names",
    ),
)


def get_sigma_coordinates(ds: xr.Dataset) -> SigmaCoordinates:
    """
    Get sigma coordinates from a dataset.

    Assumes that the dataset contains variables named `ak_N` and `bk_N` where
    `N` is the level number. The returned tensors are sorted by level number.

    Args:
        ds: Dataset to get sigma coordinates from.
    """
    ak_mapping = {int(v[3:]): torch.as_tensor(ds[v].values) for v in ds.variables if v.startswith("ak_")}
    bk_mapping = {int(v[3:]): torch.as_tensor(ds[v].values) for v in ds.variables if v.startswith("bk_")}
    ak_list = [ak_mapping[k] for k in sorted(ak_mapping.keys())]
    bk_list = [bk_mapping[k] for k in sorted(bk_mapping.keys())]

    if len(ak_list) == 0 or len(bk_list) == 0:
        raise ValueError("Dataset does not contain ak and bk sigma coordinates.")

    if len(ak_list) != len(bk_list):
        raise ValueError("Expected same number of ak and bk coordinates, " f"got {len(ak_list)} and {len(bk_list)}.")

    return SigmaCoordinates(
        ak=torch.as_tensor(ak_list, device=get_device()),
        bk=torch.as_tensor(bk_list, device=get_device()),
    )


def get_cumulative_timesteps(paths: List[str]) -> np.ndarray:
    """Returns a list of cumulative timesteps for each file in paths."""
    num_timesteps_per_file = [0]
    for path in paths:
        with xr.open_dataset(path, use_cftime=True) as ds:
            num_timesteps_per_file.append(len(ds.time))
    return np.array(num_timesteps_per_file).cumsum()


def get_file_local_index(index: int, start_indices: np.ndarray) -> Tuple[int, int]:
    """
    Return a tuple of the index of the file containing the time point at `index`
    and the index of the time point within that file.
    """
    file_index = np.searchsorted(start_indices, index, side="right") - 1
    time_index = index - start_indices[file_index]
    return int(file_index), time_index


class StaticDerivedData:
    names = ("x", "y", "z")
    metadata = {
        "x": VariableMetadata(units="", long_name="Euclidean x-coordinate"),
        "y": VariableMetadata(units="", long_name="Euclidean y-coordinate"),
        "z": VariableMetadata(units="", long_name="Euclidean z-coordinate"),
    }

    def __init__(self, lons, lats):
        """
        Args:
            lons: 1D array of longitudes.
            lats: 1D array of latitudes.
        """
        self._lats = lats
        self._lons = lons
        self._x: Optional[torch.Tensor] = None
        self._y: Optional[torch.Tensor] = None
        self._z: Optional[torch.Tensor] = None

    def _get_xyz(self) -> Dict[str, torch.Tensor]:
        if self._x is None or self._y is None or self._z is None:
            lats, lons = np.broadcast_arrays(self._lats[:, None], self._lons[None, :])
            x, y, z = lon_lat_to_xyz(lons, lats)
            self._x = torch.as_tensor(x)
            self._y = torch.as_tensor(y)
            self._z = torch.as_tensor(z)
        return {"x": self._x, "y": self._y, "z": self._z}

    def __getitem__(self, name: str) -> torch.Tensor:
        return self._get_xyz()[name]


class XarrayDataset(Dataset):
    """Handles dataloading over multiple netcdf files using the xarray library.
    Assumes that the netcdf filenames are time-ordered."""

    def __init__(
        self,
        params: XarrayDataParams,
        requirements: DataRequirements,
    ):
        self.params = params
        self.names = requirements.names
        self.path = params.data_path
        self.engine = "netcdf4" if params.engine is None else params.engine
        # assume that filenames include time ordering
        self.full_paths = sorted(glob(os.path.join(self.path, "*.nc")))

        if len(self.full_paths) == 0:
            raise ValueError(f"No netCDF files found in '{self.path}'.")
        self.full_paths *= params.n_repeats
        self.n_steps = requirements.n_timesteps  # one input, n_steps - 1 outputs
        self._get_files_stats()
        first_dataset = xr.open_dataset(
            self.full_paths[0],
            decode_times=False,
        )
        lons, lats = get_lons_and_lats(first_dataset)
        self._static_derived_data = StaticDerivedData(lons, lats)
        (
            self.time_dependent_names,
            self.time_invariant_names,
            self.static_derived_names,
        ) = self._group_variable_names_by_time_type()
        self._area_weights = metrics.spherical_area_weights(lats, len(lons))
        self._sigma_coordinates = get_sigma_coordinates(first_dataset)
        self._horizontal_coordinates = HorizontalCoordinates(
            lat=torch.as_tensor(lats, device=get_device()),
            lon=torch.as_tensor(lons, device=get_device()),
        )

    @property
    def horizontal_coordinates(self) -> HorizontalCoordinates:
        return self._horizontal_coordinates

    def _get_metadata(self, ds):
        result = {}
        for name in self.names:
            if name in StaticDerivedData.names:
                result[name] = StaticDerivedData.metadata[name]
            elif hasattr(ds[name], "units") and hasattr(ds[name], "long_name"):
                result[name] = VariableMetadata(
                    units=ds[name].units,
                    long_name=ds[name].long_name,
                )
        self._metadata = result

    def _get_files_stats(self):
        logging.info(f"Opening data at {os.path.join(self.path, '*.nc')}")
        cum_num_timesteps = get_cumulative_timesteps(self.full_paths)
        self.start_indices = cum_num_timesteps[:-1]
        self.total_timesteps = cum_num_timesteps[-1]
        self._n_initial_conditions = self.total_timesteps - self.n_steps + 1
        del cum_num_timesteps

        ds = self._open_file(0)
        self._get_metadata(ds)

        verbose = False
        if verbose:
            for i in range(len(self.names)):
                if self.names[i] in ds.variables:
                    img_shape = ds[self.names[i]].shape[-2:]
                    break
            else:
                raise ValueError(f"None of the requested variables {self.names} are present " f"in the dataset.")
            logging.info(f"Found {self._n_initial_conditions} samples.")
            logging.info(f"Image shape is {img_shape[0]} x {img_shape[1]}.")
            # logging.info(f"Following variables are available: {list(ds.variables)}.")

    def _group_variable_names_by_time_type(self) -> VariableNames:
        """Returns lists of time-dependent variable names, time-independent
        variable names, and variables which are only present as an initial
        condition."""
        time_dependent_names, time_invariant_names, static_derived_names = [], [], []
        # Don't use open_mfdataset here, because it will give time-invariant
        # fields a time dimension. We assume that all fields are present in the
        # netcdf file corresponding to the first chunk of time.
        with xr.open_dataset(self.full_paths[0]) as ds:
            for name in self.names:
                if name in StaticDerivedData.names:
                    static_derived_names.append(name)
                else:
                    dims = ds[name].dims
                    if "time" in dims:
                        time_dependent_names.append(name)
                    else:
                        time_invariant_names.append(name)
        return VariableNames(
            time_dependent_names,
            time_invariant_names,
            static_derived_names,
        )

    @property
    def area_weights(self) -> torch.Tensor:
        return self._area_weights

    @property
    def metadata(self) -> Mapping[str, VariableMetadata]:
        return self._metadata

    @property
    def sigma_coordinates(self) -> SigmaCoordinates:
        return self._sigma_coordinates

    def __len__(self):
        return self._n_initial_conditions

    def _open_file(self, idx):
        return xr.open_dataset(
            self.full_paths[idx],
            engine=self.engine,
            use_cftime=True,
            cache=False,
            mask_and_scale=False,
        )

    def __getitem__(self, idx: int) -> Tuple[Dict[str, torch.Tensor], xr.DataArray]:
        """Open a time-ordered subset of the files which contain the input with
        global index idx and its outputs. Get a starting index in the first file
        (input_local_idx) and a final index in the last file (output_local_idx),
        returning the time-ordered sequence of observations from input_local_idx
        to output_local_idx (inclusive).

        Args:
            idx: Index of the sample to retrieve.

        Returns:
            Tuple of a sample's data (a mapping from names to data, for use in
                training and inference) and its corresponding time coordinates.
        """
        time_slice = slice(idx, idx + self.n_steps)
        return self.get_sample_by_time_slice(time_slice)

    def get_sample_by_time_slice(self, time_slice: slice) -> Tuple[Dict[str, torch.Tensor], xr.DataArray]:
        input_file_idx, input_local_idx = get_file_local_index(time_slice.start, self.start_indices)
        output_file_idx, output_local_idx = get_file_local_index(time_slice.stop - 1, self.start_indices)

        # get the sequence of observations
        arrays: Dict[str, List[torch.Tensor]] = {}
        times_segments: List[xr.DataArray] = []
        idxs = range(input_file_idx, output_file_idx + 1)
        total_steps = 0
        for i, file_idx in enumerate(idxs):
            ds = self._open_file(file_idx)
            start = input_local_idx if i == 0 else 0
            stop = output_local_idx if i == len(idxs) - 1 else len(ds["time"]) - 1
            n_steps = stop - start + 1
            total_steps += n_steps
            tensor_dict = load_series_data(start, n_steps, ds, self.time_dependent_names)
            for n in self.time_dependent_names:
                arrays.setdefault(n, []).append(tensor_dict[n])
            times_segments.append(get_times(ds, start, n_steps))
            ds.close()
            del ds

        tensors: Dict[str, torch.Tensor] = {}
        for n, tensor_list in arrays.items():
            tensors[n] = torch.cat(tensor_list)
        del arrays
        times: xr.DataArray = xr.concat(times_segments, dim="time")

        # load time-invariant variables from first dataset
        ds = self._open_file(idxs[0])
        for name in self.time_invariant_names:
            tensor = torch.as_tensor(ds[name].values)
            tensors[name] = tensor.repeat((total_steps, 1, 1))

        # load static derived variables
        for name in self.static_derived_names:
            tensor = self._static_derived_data[name]
            tensors[name] = tensor.repeat((total_steps, 1, 1))

        return tensors, times


class XarrayDatasetSalva(XarrayDataset):
    def __init__(
        self,
        *args,
        forcing_names: List[str] = tuple(),
        forcing_packer: Optional[Callable] = None,
        forcing_normalizer: Optional[Callable] = None,
        min_idx_shift: int = 0,
        split_id: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.forcing_names = forcing_names if len(forcing_names) > 0 else None
        self.forcing_packer = forcing_packer
        self.forcing_normalizer = forcing_normalizer
        self.min_idx_shift = min_idx_shift
        self.split_id = split_id

    def __getitem__(self, idx: int) -> Tuple[Dict[str, torch.Tensor], xr.DataArray]:
        idx = idx + self.min_idx_shift
        tensors, times = super().__getitem__(idx)

        if self.forcing_names is not None:
            forcings = {k: tensors.pop(k) for k in list(tensors.keys()) if k in self.forcing_names}
            forcings = self.forcing_packer.pack(self.forcing_normalizer.normalize(forcings))
            data = {"dynamics": tensors, "dynamical_condition": forcings}
        else:
            data = {"dynamics": tensors}
        return data
