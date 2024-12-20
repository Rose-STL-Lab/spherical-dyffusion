import dataclasses
import warnings
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    Union,
)

import dacite
import torch
from torch import nn
from torch.nn.parallel import DistributedDataParallel
from tqdm import tqdm

from src.ace_inference.core.aggregator.null import NullAggregator
from src.ace_inference.core.corrector import Corrector, CorrectorConfig
from src.ace_inference.core.data_loading.data_typing import SigmaCoordinates
from src.ace_inference.core.data_loading.requirements import DataRequirements
from src.ace_inference.core.device import get_device, using_gpu
from src.ace_inference.core.distributed import Distributed
from src.ace_inference.core.loss import ConservationLoss, ConservationLossConfig, LossConfig
from src.ace_inference.core.normalizer import (
    FromStateNormalizer,
    NormalizationConfig,
    StandardNormalizer,
)
from src.ace_inference.core.ocean import Ocean, OceanConfig
from src.ace_inference.core.optimization import DisabledOptimizationConfig, NullOptimization, Optimization
from src.ace_inference.core.parameter_init import ParameterInitializationConfig
from src.ace_inference.core.prescriber import PrescriberConfig
from src.ace_inference.core.registry import ModuleSelector
from src.evaluation.aggregators.main import OneStepAggregator
from src.utilities.packer import Packer
from src.utilities.utils import enable_inference_dropout as enable_inference_dropout_func


@dataclasses.dataclass
class SingleModuleStepperConfig:
    builder: ModuleSelector
    in_names: List[str]
    out_names: List[str]
    normalization: Union[NormalizationConfig, FromStateNormalizer]
    parameter_init: ParameterInitializationConfig = dataclasses.field(
        default_factory=lambda: ParameterInitializationConfig()
    )
    optimization: Optional[DisabledOptimizationConfig] = None
    ocean: Optional[OceanConfig] = None
    loss: LossConfig = dataclasses.field(default_factory=lambda: LossConfig())
    conserve_dry_air: Optional[bool] = None
    corrector: CorrectorConfig = dataclasses.field(default_factory=lambda: CorrectorConfig())
    conservation_loss: ConservationLossConfig = dataclasses.field(default_factory=lambda: ConservationLossConfig())
    prescriber: Optional[PrescriberConfig] = None
    enable_inference_dropout: bool = False

    def __post_init__(self):
        if self.conserve_dry_air is not None:
            warnings.warn(
                "conserve_dry_air is deprecated, " "use corrector.conserve_dry_air instead",
                category=DeprecationWarning,
            )
            self.corrector.conserve_dry_air = self.conserve_dry_air
        if self.prescriber is not None:
            warnings.warn(
                "Directly configuring prescriber is deprecated, " "use 'ocean' option instead.",
                category=DeprecationWarning,
            )
            if self.ocean is not None:
                raise ValueError("Cannot specify both prescriber and ocean.")
            self.ocean = OceanConfig(
                surface_temperature_name=self.prescriber.prescribed_name,
                ocean_fraction_name=self.prescriber.mask_name,
                interpolate=self.prescriber.interpolate,
            )
            del self.prescriber

    def get_data_requirements(self, n_forward_steps: int) -> DataRequirements:
        return DataRequirements(
            names=self.all_names,
            n_timesteps=n_forward_steps + 1,
        )

    def get_state(self):
        return dataclasses.asdict(self)

    def get_base_weights(self) -> Optional[List[Mapping[str, Any]]]:
        """
        If the model is being initialized from another model's weights for fine-tuning,
        returns those weights. Otherwise, returns None.

        The list mirrors the order of `modules` in the `SingleModuleStepper` class.
        """
        base_weights = self.parameter_init.get_base_weights()
        if base_weights is not None:
            return [base_weights]
        else:
            return None

    def get_stepper(
        self,
        img_shape: Tuple[int, int],
        area: Optional[torch.Tensor],
        sigma_coordinates: SigmaCoordinates,
    ):
        return SingleModuleStepper(
            config=self,
            img_shape=img_shape,
            area=area,
            sigma_coordinates=sigma_coordinates,
        )

    @classmethod
    def from_state(cls, state) -> "SingleModuleStepperConfig":
        return dacite.from_dict(data_class=cls, data=state, config=dacite.Config(strict=True))

    @property
    def all_names(self):
        """Names of all variables required, including auxiliary ones."""
        extra_names = []
        if self.ocean is not None:
            extra_names.extend(self.ocean.names)
        all_names = list(set(self.in_names).union(self.out_names).union(extra_names))
        return all_names

    @property
    def normalize_names(self):
        """Names of variables which require normalization. I.e. inputs/outputs."""
        return list(set(self.in_names).union(self.out_names))


@dataclasses.dataclass
class ExistingStepperConfig:
    checkpoint_path: str

    def _load_checkpoint(self) -> Mapping[str, Any]:
        return torch.load(self.checkpoint_path, map_location=get_device())

    def get_data_requirements(self, n_forward_steps: int) -> DataRequirements:
        return SingleModuleStepperConfig.from_state(
            self._load_checkpoint()["stepper"]["config"]
        ).get_data_requirements(n_forward_steps)

    def get_base_weights(self) -> Optional[List[Mapping[str, Any]]]:
        return SingleModuleStepperConfig.from_state(self._load_checkpoint()["stepper"]["config"]).get_base_weights()

    def get_stepper(self, img_shape, area, sigma_coordinates):
        del img_shape  # unused
        return SingleModuleStepper.from_state(
            self._load_checkpoint()["stepper"],
            area=area,
            sigma_coordinates=sigma_coordinates,
        )


class DummyWrapper(nn.Module):
    """
    Wrapper class for a single pytorch module, which does nothing.

    Exists so we have an identical module structure to the case where we use
    a DistributedDataParallel wrapper.
    """

    def __init__(self, module):
        super().__init__()
        self.module = module

    def forward(self, *args, **kwargs):
        return self.module(*args, **kwargs)


@dataclasses.dataclass
class SteppedData:
    metrics: Dict[str, torch.Tensor]
    gen_data: Dict[str, torch.Tensor]
    target_data: Dict[str, torch.Tensor]
    gen_data_norm: Dict[str, torch.Tensor]
    target_data_norm: Dict[str, torch.Tensor]

    def remove_initial_condition(self) -> "SteppedData":
        any_key = next(iter(self.gen_data.keys()))
        is_ensemble = self.gen_data[any_key].shape != self.target_data[any_key].shape

        def remove_initial_condition_from_gen(tensor: torch.Tensor) -> torch.Tensor:
            return tensor[:, :, 1:] if is_ensemble else tensor[:, 1:]

        return SteppedData(
            metrics=self.metrics,
            gen_data={k: remove_initial_condition_from_gen(v) for k, v in self.gen_data.items()},
            target_data={k: v[:, 1:] for k, v in self.target_data.items()},
            gen_data_norm={k: remove_initial_condition_from_gen(v) for k, v in self.gen_data_norm.items()},
            target_data_norm={k: v[:, 1:] for k, v in self.target_data_norm.items()},
        )

    def copy(self) -> "SteppedData":
        """Creates new dictionaries for the data but with the same tensors."""
        return SteppedData(
            metrics=self.metrics,
            gen_data={k: v for k, v in self.gen_data.items()},
            target_data={k: v for k, v in self.target_data.items()},
            gen_data_norm={k: v for k, v in self.gen_data_norm.items()},
            target_data_norm={k: v for k, v in self.target_data_norm.items()},
        )

    # Method to stack a list of stepped data objects together
    @staticmethod
    def stack(stepped_data_list: List["SteppedData"], dim: int) -> "SteppedData":
        return SteppedData(
            metrics=None,
            gen_data={
                k: torch.stack([sd.gen_data[k] for sd in stepped_data_list], dim=dim)
                for k in stepped_data_list[0].gen_data.keys()
            },
            target_data={
                k: torch.stack([sd.target_data[k] for sd in stepped_data_list], dim=dim)
                for k in stepped_data_list[0].target_data.keys()
            },
            gen_data_norm={
                k: torch.stack([sd.gen_data_norm[k] for sd in stepped_data_list], dim=dim)
                for k in stepped_data_list[0].gen_data_norm.keys()
            },
            target_data_norm={
                k: torch.stack([sd.target_data_norm[k] for sd in stepped_data_list], dim=dim)
                for k in stepped_data_list[0].target_data_norm.keys()
            },
        )


class SingleModuleStepper:
    """
    Stepper class for a single pytorch module.
    """

    def __init__(
        self,
        config: SingleModuleStepperConfig,
        img_shape: Tuple[int, int],
        area: torch.Tensor,
        sigma_coordinates: SigmaCoordinates,
        init_weights: bool = True,
    ):
        """
        Args:
            config: The configuration.
            img_shape: Shape of domain as (n_lat, n_lon).
            area: (n_lat, n_lon) array containing relative gridcell area,
                in any units including unitless.
            sigma_coordinates: The sigma coordinates.
            init_weights: Whether to initialize the weights. Should pass False if
                the weights are about to be overwritten by a checkpoint.
        """
        dist = Distributed.get_instance()
        n_in_channels = len(config.in_names)
        n_out_channels = len(config.out_names)
        channel_axis = -3
        self.in_packer = Packer(config.in_names, axis=channel_axis)
        self.out_packer = Packer(config.out_names, axis=channel_axis)
        self.normalizer = config.normalization.build(config.normalize_names)
        if config.ocean is not None:
            self.ocean = config.ocean.build(config.in_names, config.out_names)
        else:
            self.ocean = None
        self.module = config.builder.build(
            n_in_channels=n_in_channels,
            n_out_channels=n_out_channels,
            img_shape=img_shape,
        )
        self.module = config.parameter_init.apply(self.module, init_weights=init_weights).to(get_device())

        self._img_shape = img_shape
        self._config = config
        self._no_optimization = NullOptimization()

        if dist.is_distributed():
            if using_gpu():
                device_ids = [dist.local_rank]
                output_device = [dist.local_rank]
            else:
                device_ids = None
                output_device = None
            self.module = DistributedDataParallel(
                self.module,
                device_ids=device_ids,
                output_device=output_device,
            )
        else:
            self.module = DummyWrapper(self.module)
        self._is_distributed = dist.is_distributed()

        self.area = area
        self.sigma_coordinates = sigma_coordinates.to(get_device())
        self.loss_obj = config.loss.build(self.area)
        self._conservation_loss = config.conservation_loss.build(
            area_weights=self.area,
            sigma_coordinates=self.sigma_coordinates,
        )
        self._corrector = config.corrector.build(area=area, sigma_coordinates=sigma_coordinates)

    def get_data_requirements(self, n_forward_steps: int) -> DataRequirements:
        return self._config.get_data_requirements(n_forward_steps)

    @property
    def modules(self) -> nn.ModuleList:
        """
        Returns:
            A list of modules being trained.
        """
        return nn.ModuleList([self.module])

    def run_on_batch(
        self,
        data: Dict[str, torch.Tensor],
        optimization: Union[Optimization, NullOptimization],
        n_forward_steps: int = 1,
        aggregator: Optional[OneStepAggregator] = None,
    ) -> SteppedData:
        """
        Step the model forward on a batch of data.

        Args:
            data: The batch data of shape [n_sample, n_timesteps, n_channels, n_x, n_y].
            optimization: The optimization class to use for updating the module.
                Use `NullOptimization` to disable training.
            n_forward_steps: The number of timesteps to run the model for.
            aggregator: The data aggregator.

        Returns:
            The loss, the generated data, the normalized generated data,
                and the normalized batch data.
        """
        if aggregator is None:
            non_none_aggregator: Union[OneStepAggregator, NullAggregator] = NullAggregator()
        else:
            non_none_aggregator = aggregator

        device = get_device()
        device_data = {name: value.to(device, dtype=torch.float) for name, value in data.items()}
        return run_on_batch(
            data=device_data,
            module=self.module,
            normalizer=self.normalizer,
            in_packer=self.in_packer,
            out_packer=self.out_packer,
            optimization=optimization,
            loss_obj=self.loss_obj,
            n_forward_steps=n_forward_steps,
            ocean=self.ocean,
            aggregator=non_none_aggregator,
            corrector=self._corrector,
            conservation_loss=self._conservation_loss,
            enable_inference_dropout=self.enable_inference_dropout,
        )

    def get_state(self):
        """
        Returns:
            The state of the stepper.
        """
        return {
            "module": self.module.state_dict(),
            "normalizer": self.normalizer.get_state(),
            "img_shape": self._img_shape,
            "config": self._config.get_state(),
            "area": self.area,
            "sigma_coordinates": self.sigma_coordinates.as_dict(),
        }

    def load_state(self, state):
        """
        Load the state of the stepper.

        Args:
            state: The state to load.
        """
        if "module" in state:
            self.module.load_state_dict(state["module"])

    @classmethod
    def from_state(cls, state, area: torch.Tensor, sigma_coordinates: SigmaCoordinates) -> "SingleModuleStepper":
        """
        Load the state of the stepper.

        Args:
            state: The state to load.
            area: (n_lat, n_lon) array containing relative gridcell area, in any
                units including unitless.
            sigma_coordinates: The sigma coordinates.

        Returns:
            The stepper.
        """
        config = {**state["config"]}  # make a copy to avoid mutating input
        config["normalization"] = FromStateNormalizer(state["normalizer"])
        area = state.get("area", area)
        if "sigma_coordinates" in state:
            sigma_coordinates = dacite.from_dict(
                data_class=SigmaCoordinates,
                data=state["sigma_coordinates"],
                config=dacite.Config(strict=True),
            )
        if "img_shape" in state:
            img_shape = state["img_shape"]
        else:
            # this is for backwards compatibility with old checkpoints
            for v in state["data_shapes"].values():
                img_shape = v[-2:]
                break
        stepper = cls(
            config=SingleModuleStepperConfig.from_state(config),
            img_shape=img_shape,
            area=area,
            sigma_coordinates=sigma_coordinates,
            # don't need to initialize weights, we're about to load_state
            init_weights=False,
        )
        stepper.load_state(state)
        return stepper


class NameAndTimeQueryFunction(Protocol):
    def __call__(
        self,
        names: Iterable[str],
        time_index: int,
        norm_mode: Literal["norm", "denorm"],
    ) -> Dict[str, torch.Tensor]: ...


def get_name_and_time_query_fn(
    data: Dict[str, torch.Tensor], data_norm: Dict[str, torch.Tensor], time_dim: int
) -> NameAndTimeQueryFunction:
    """Construct a function for querying `data` by name and time and whether it
    is normalized or not. (Note: that the `names` argument can contain None values
    to handle NullPrescriber)."""

    norm_mode_to_data = {"norm": data_norm, "denorm": data}

    def name_and_time_query_fn(names, time_index, norm_mode):
        _data = norm_mode_to_data[norm_mode]
        query_results = {}
        for name in names:
            try:
                query_results[name] = _data[name].select(dim=time_dim, index=time_index)
            except IndexError as err:
                raise ValueError(f'tensor "{name}" does not have values at t={time_index}') from err
        return query_results

    return name_and_time_query_fn


def _pack_data_if_available(
    packer: Packer,
    data: Dict[str, torch.Tensor],
    axis: int,
) -> Optional[torch.Tensor]:
    try:
        return packer.pack(data, axis=axis)
    except ValueError:
        return None


def run_on_batch(
    data: Dict[str, torch.Tensor],
    module: nn.Module,
    normalizer: StandardNormalizer,
    in_packer: Packer,
    out_packer: Packer,
    optimization: Union[Optimization, NullOptimization],
    loss_obj: nn.Module,
    ocean: Optional[Ocean],
    aggregator: Union[OneStepAggregator, NullAggregator],
    corrector: Optional[Corrector],  # Optional so we can skip code when unused
    conservation_loss: ConservationLoss,
    n_forward_steps: int = 1,
    enable_inference_dropout: bool = False,
) -> SteppedData:
    """
    Run the model on a batch of data.

    The module is assumed to require packed (concatenated into a tensor with
    a channel dimension) and normalized data, as provided by the given packer
    and normalizer.

    Args:
        data: The denormalized batch data. The second dimension of each tensor
            should be the time dimension.
        module: The module to run.
        normalizer: The normalizer.
        in_packer: The packer for the input data.
        out_packer: The packer for the output data.
        optimization: The optimization object. If it is NullOptimization,
            then the model is not trained.
        loss_obj: The loss object.
        ocean: Determines sea surface temperatures.
        aggregator: The data aggregator.
        corrector: The post-step corrector.
        conservation_loss: Computes conservation-related losses, if any.
        n_forward_steps: The number of timesteps to run the model for.

    Returns:
        The loss, the generated data, the normalized generated data,
            and the normalized batch data. The generated data contains
            the initial input data as its first timestep.
    """
    channel_dim = -3
    time_dim = 1
    full_data_norm = normalizer.normalize(data)
    get_input_data = get_name_and_time_query_fn(data, full_data_norm, time_dim)

    full_target_tensor_norm = _pack_data_if_available(
        out_packer,
        full_data_norm,
        channel_dim,
    )

    loss = torch.tensor(0.0, device=get_device())
    metrics = {}
    input_data_norm = get_input_data(in_packer.names, time_index=0, norm_mode="norm")
    gen_data_norm = []
    optimization.set_mode(module)
    if enable_inference_dropout:
        enable_inference_dropout_func(module)
    tqdm_bar = tqdm(range(n_forward_steps), desc="Horizon")
    for step in tqdm_bar:
        input_tensor_norm = in_packer.pack(input_data_norm, axis=channel_dim)

        if full_target_tensor_norm is None:
            target_tensor_norm: Optional[torch.Tensor] = None
        else:
            target_tensor_norm = full_target_tensor_norm.select(dim=time_dim, index=step + 1)

        with optimization.autocast():
            gen_tensor_norm = module(input_tensor_norm).to(get_device(), dtype=torch.float)
            gen_norm = out_packer.unpack(gen_tensor_norm, axis=channel_dim)
            gen_data = normalizer.denormalize(gen_norm)
            input_data = normalizer.denormalize(input_data_norm)
            if corrector is not None:
                gen_data = corrector(input_data, gen_data)
            if ocean is not None:
                target_data = get_input_data(ocean.target_names, step + 1, "denorm")
                gen_data = ocean(target_data, input_data, gen_data)
            gen_norm = normalizer.normalize(gen_data)
            gen_tensor_norm = out_packer.pack(gen_norm, axis=channel_dim).to(get_device(), dtype=torch.float)
            if target_tensor_norm is None:
                step_loss = torch.tensor(torch.nan)
            else:
                step_loss = loss_obj(gen_tensor_norm, target_tensor_norm)
            loss += step_loss
            metrics[f"loss_step_{step}"] = step_loss.detach()
        gen_norm = out_packer.unpack(gen_tensor_norm, axis=channel_dim)
        gen_data_norm.append(gen_norm)
        # update input data with generated outputs, and forcings for missing outputs
        forcing_names = list(set(in_packer.names).difference(gen_norm.keys()))
        forcing_data_norm = get_input_data(forcing_names, time_index=step + 1, norm_mode="norm")
        input_data_norm = {**forcing_data_norm, **gen_norm}

    # prepend the initial (pre-first-timestep) output data to the generated data
    initial = get_input_data(out_packer.names, time_index=0, norm_mode="norm")
    gen_data_norm = [initial] + gen_data_norm
    gen_data_norm_timeseries = {}
    for name in out_packer.names:
        gen_data_norm_timeseries[name] = torch.stack([x[name] for x in gen_data_norm], dim=time_dim)
    gen_data = normalizer.denormalize(gen_data_norm_timeseries)

    conservation_metrics, conservation_loss = conservation_loss(gen_data)
    metrics.update(conservation_metrics)
    loss += conservation_loss

    metrics["loss"] = loss.detach()
    optimization.step_weights(loss)

    aggregator.record_batch(
        float(loss),
        target_data=data,
        gen_data=gen_data,
        target_data_norm=full_data_norm,
        gen_data_norm=gen_data_norm_timeseries,
    )

    return SteppedData(
        metrics=metrics,
        gen_data=gen_data,
        target_data=data,
        gen_data_norm=gen_data_norm_timeseries,
        target_data_norm=full_data_norm,
    )
