import dataclasses
from typing import Dict, Literal, Mapping, Optional

import torch

from src.ace_inference.core import metrics
from src.ace_inference.core.aggregator.climate_data import ClimateData
from src.ace_inference.core.constants import TIMESTEP_SECONDS
from src.ace_inference.core.data_loading.data_typing import SigmaCoordinates
from src.ace_inference.core.device import get_device


@dataclasses.dataclass
class CorrectorConfig:
    """
    Configuration for the post-step state corrector.

    conserve_dry_air enforces the constraint that:

        global_dry_air = global_mean(
            ps - sum_k((ak_diff + bk_diff * ps) * wat_k)
        )

    in the generated data is equal to its value in the input data. This is done
    by adding a globally-constant correction to the surface pressure in each
    column. As per-mass values such as mixing ratios of water are unchanged,
    this can cause changes in total water or energy. Note all global means here
    are area-weighted.

    zero_global_mean_moisture_advection enforces the constraint that:

        global_mean(tendency_of_total_water_path_due_to_advection) = 0

    in the generated data. This is done by adding a globally-constant correction
    to the moisture advection tendency in each column.

    moisture_budget_correction enforces closure of the moisture budget equation:

        tendency_of_total_water_path = (
            evaporation_rate - precipitation_rate
            + tendency_of_total_water_path_due_to_advection
        )

    in the generated data, where tendency_of_total_water_path is the difference
    between the total water path at the current timestep and the previous
    timestep divided by the time difference. This is done by modifying the
    precipitation, evaporation, and/or moisture advection tendency fields as
    described in the moisture_budget_correction attribute. When
    advection tendency is modified, this budget equation is enforced in each
    column, while when only precipitation or evaporation are modified, only
    the global mean of the budget equation is enforced.

    When enforcing moisture budget closure, we assume the global mean moisture
    advection is zero. Therefore zero_global_mean_moisture_advection must be
    True if using a moisture_budget_correction option other tha None.

    Attributes:
        conserve_dry_air: If True, force the generated data to conserve dry air
            by subtracting a constant offset from the surface pressure of each
            column. This can cause changes in per-mass values such as total water
            or energy.
        zero_global_mean_moisture_advection: If True, force the generated data to
            have zero global mean moisture advection by subtracting a constant
            offset from the moisture advection tendency of each column.
        moisture_budget_correction: If not "none", force the generated data to
            conserve global or column-local moisture by modifying budget fields.
            One of:
                - "precipitation": multiply precipitation by a scale factor
                    to close the global moisture budget
                - "evaporation": multiply evaporation by a scale factor
                    to close the global moisture budget
                - "advection_and_precipitation": after applying the "precipitation"
                    global-mean correction above, we recompute the column-integrated
                    advective tendency as the budget residual,
                    ensuring column budget closure.
                - "advection_and_evaporation": after applying the "evaporation"
                    global-mean correction above, we recompute the column-integrated
                    advective tendency as the budget residual,
                    ensuring column budget closure.
    """

    conserve_dry_air: bool = False
    zero_global_mean_moisture_advection: bool = False
    moisture_budget_correction: Optional[
        Literal[
            "precipitation",
            "evaporation",
            "advection_and_precipitation",
            "advection_and_evaporation",
        ]
    ] = None

    def build(self, area: torch.Tensor, sigma_coordinates: SigmaCoordinates) -> Optional["Corrector"]:
        return Corrector(config=self, area=area, sigma_coordinates=sigma_coordinates)


class Corrector:
    def __init__(
        self,
        config: CorrectorConfig,
        area: torch.Tensor,
        sigma_coordinates: SigmaCoordinates,
    ):
        self._config = config
        self._area = area.to(get_device())
        self._sigma_coordinates = sigma_coordinates.to(get_device())

    def __call__(
        self,
        input_data: Mapping[str, torch.Tensor],
        gen_data: Mapping[str, torch.Tensor],
    ):
        if self._config.conserve_dry_air:
            gen_data = _force_conserve_dry_air(
                input_data=input_data,
                gen_data=gen_data,
                area=self._area,
                sigma_coordinates=self._sigma_coordinates,
            )
        if self._config.zero_global_mean_moisture_advection:
            gen_data = _force_zero_global_mean_moisture_advection(
                gen_data=gen_data,
                area=self._area,
            )
        if self._config.moisture_budget_correction is not None:
            gen_data = _force_conserve_moisture(
                input_data=input_data,
                gen_data=gen_data,
                area=self._area,
                sigma_coordinates=self._sigma_coordinates,
                terms_to_modify=self._config.moisture_budget_correction,
            )
        return gen_data


def _force_conserve_dry_air(
    input_data: Mapping[str, torch.Tensor],
    gen_data: Mapping[str, torch.Tensor],
    area: torch.Tensor,
    sigma_coordinates: SigmaCoordinates,
) -> Dict[str, torch.Tensor]:
    """
    Update the generated data to conserve dry air.

    This is done by adding a constant correction to the dry air pressure of
    each column, and may result in changes in per-mass values such as
    total water or energy.

    We first compute the target dry air pressure by computing the globally
    averaged difference in dry air pressure between the input_data and gen_data,
    and then add this offset to the fully-resolved gen_data dry air pressure.
    We can then solve for the surface pressure corresponding to this new dry air
    pressure.

    We start from the expression for dry air pressure:

        dry_air = ps - sum_k((ak_diff + bk_diff * ps) * wat_k)

    To update the dry air, we compute and update the surface pressure:

        ps = (
            dry_air + sum_k(ak_diff * wat_k)
        ) / (
            1 - sum_k(bk_diff * wat_k)
        )
    """
    input = ClimateData(input_data)
    if input.surface_pressure is None:
        raise ValueError("surface_pressure is required to force dry air conservation")
    gen = ClimateData(gen_data)
    gen_dry_air = gen.surface_pressure_due_to_dry_air(sigma_coordinates)
    global_gen_dry_air = metrics.weighted_mean(gen_dry_air, weights=area, dim=(-2, -1))
    global_target_gen_dry_air = metrics.weighted_mean(
        input.surface_pressure_due_to_dry_air(sigma_coordinates),
        weights=area,
        dim=(-2, -1),
    )
    error = global_gen_dry_air - global_target_gen_dry_air
    new_gen_dry_air = gen_dry_air - error[..., None, None]
    try:
        wat = gen.specific_total_water
    except KeyError:
        raise ValueError("specific_total_water is required for conservation")
    ak_diff = sigma_coordinates.ak.diff()
    bk_diff = sigma_coordinates.bk.diff()
    new_pressure = (new_gen_dry_air + (ak_diff * wat).sum(-1)) / (1 - (bk_diff * wat).sum(-1))
    gen.surface_pressure = new_pressure.to(dtype=input.surface_pressure.dtype)
    return gen.data


def _force_zero_global_mean_moisture_advection(
    gen_data: Mapping[str, torch.Tensor],
    area: torch.Tensor,
) -> Dict[str, torch.Tensor]:
    """
    Update the generated data so advection conserves moisture.

    Does so by adding a constant offset to the moisture advective tendency.

    Args:
        gen_data: The generated data.
        area: (n_lat, n_lon) array containing relative gridcell area, in any
            units including unitless.
    """
    gen = ClimateData(gen_data)

    mean_moisture_advection = metrics.weighted_mean(
        gen.tendency_of_total_water_path_due_to_advection,
        weights=area,
        dim=(-2, -1),
    )
    gen.tendency_of_total_water_path_due_to_advection = (
        gen.tendency_of_total_water_path_due_to_advection - mean_moisture_advection[..., None, None]
    )
    return gen.data


def _force_conserve_moisture(
    input_data: Mapping[str, torch.Tensor],
    gen_data: Mapping[str, torch.Tensor],
    area: torch.Tensor,
    sigma_coordinates: SigmaCoordinates,
    terms_to_modify: Literal[
        "precipitation",
        "evaporation",
        "advection_and_precipitation",
        "advection_and_evaporation",
    ],
) -> Dict[str, torch.Tensor]:
    """
    Update the generated data to conserve moisture.

    Does so while conserving total dry air in each column.

    Assumes the global mean advective tendency of moisture is zero. This assumption
    means any existing global mean advective tendency will be set to zero
    if the advective tendency is re-computed.

    Args:
        input_data: The input data.
        gen_data: The generated data one timestep after the input data.
        area: (n_lat, n_lon) array containing relative gridcell area, in any
            units including unitless.
        sigma_coordinates: The sigma coordinates.
        terms_to_modify: Which terms to modify, in addition to modifying surface
            pressure to conserve dry air mass. One of:
            - "precipitation": modify precipitation only
            - "evaporation": modify evaporation only
            - "advection_and_precipitation": modify advection and precipitation
            - "advection_and_evaporation": modify advection and evaporation
    """
    input = ClimateData(input_data)
    gen = ClimateData(gen_data)

    gen_total_water_path = gen.total_water_path(sigma_coordinates)
    twp_total_tendency = (gen_total_water_path - input.total_water_path(sigma_coordinates)) / TIMESTEP_SECONDS
    twp_tendency_global_mean = metrics.weighted_mean(twp_total_tendency, weights=area, dim=(-2, -1))
    evaporation_global_mean = metrics.weighted_mean(gen.evaporation_rate, weights=area, dim=(-2, -1))
    precipitation_global_mean = metrics.weighted_mean(gen.precipitation_rate, weights=area, dim=(-2, -1))
    if terms_to_modify.endswith("precipitation"):
        # We want to achieve
        #     global_mean(twp_total_tendency) = (
        #         global_mean(evaporation_rate)
        #         - global_mean(precipitation_rate)
        #     )
        # so we modify precipitation_rate to achieve this. Note we have
        # assumed the global mean advection tendency is zero.
        # First, we find the required global-mean precipitation rate
        #     new_global_precip_rate = (
        #         global_mean(evaporation_rate)
        #         - global_mean(twp_total_tendency)
        #     )
        new_precipitation_global_mean = evaporation_global_mean - twp_tendency_global_mean
        # Because scalar multiplication commutes with summation, we can
        # achieve this by multiplying each gridcell's precipitation rate
        # by the ratio of the new global mean to the current global mean.
        #    new_precip_rate = (
        #        new_global_precip_rate / current_global_precip_rate
        #    ) * current_precip_rate
        gen.precipitation_rate = (
            gen.precipitation_rate * (new_precipitation_global_mean / precipitation_global_mean)[..., None, None]
        )
    elif terms_to_modify.endswith("evaporation"):
        # Derived similarly as for "precipitation" case.
        new_evaporation_global_mean = twp_tendency_global_mean + precipitation_global_mean
        gen.evaporation_rate = (
            gen.evaporation_rate * (new_evaporation_global_mean / evaporation_global_mean)[..., None, None]
        )
    if terms_to_modify.startswith("advection"):
        # Having already corrected the global-mean budget, we recompute
        # advection based on assumption that the columnwise
        # moisture budget closes. Correcting the global mean budget first
        # is important to ensure the resulting advection has zero global mean.
        new_advection = twp_total_tendency - (gen.evaporation_rate - gen.precipitation_rate)
        gen.tendency_of_total_water_path_due_to_advection = new_advection
    return gen.data
