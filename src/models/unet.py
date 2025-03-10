from functools import partial
from typing import Optional

import torch
import torch.nn.functional as F
from einops import rearrange
from torch import nn

from src.models._base_model import BaseModel
from src.models.modules.attention import Attention, LinearAttention
from src.models.modules.convs import WeightStandardizedConv2d
from src.models.modules.misc import Residual, get_time_embedder
from src.models.modules.net_norm import PreNorm
from src.utilities.utils import default, exists


def Upsample(dim, dim_out=None, scale_factor=2):
    return nn.Sequential(
        nn.Upsample(scale_factor=scale_factor, mode="nearest"), nn.Conv2d(dim, default(dim_out, dim), 3, padding=1)
    )


def Downsample(dim, dim_out=None):
    return nn.Conv2d(dim, default(dim_out, dim), 4, 2, 1)


class LayerNorm(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.g = nn.Parameter(torch.ones(1, dim, 1, 1))

    def forward(self, x):
        # x is of shape (batch, channels, height, width)
        # to use it with (batch, tokens, dim) we need to reshape it to (batch, dim, tokens)
        eps = 1e-5 if x.dtype == torch.float32 else 1e-3
        var = torch.var(x, dim=1, unbiased=False, keepdim=True)
        mean = torch.mean(x, dim=1, keepdim=True)
        return (x - mean) * (var + eps).rsqrt() * self.g


# building block modules


class Block(nn.Module):
    def __init__(self, dim, dim_out, groups=8, dropout: float = 0.0):
        super().__init__()
        self.proj = WeightStandardizedConv2d(dim, dim_out, 3, padding=1)
        try:
            self.norm = nn.GroupNorm(groups, dim_out)
        except ValueError as e:
            raise ValueError(f"You misspecified the parameter groups={groups} and dim_out={dim_out}") from e
        self.act = nn.SiLU()
        self.dropout = nn.Dropout(dropout)

    def forward(self, x, scale_shift=None):
        x = self.proj(x)
        x = self.norm(x)

        if exists(scale_shift):
            scale, shift = scale_shift
            x = x * (scale + 1) + shift

        x = self.act(x)
        x = self.dropout(x)
        return x


class ResnetBlock(nn.Module):
    def __init__(
        self,
        dim,
        dim_out,
        *,
        time_emb_dim=None,
        groups=8,
        double_conv_layer: bool = True,
        dropout1: float = 0.0,
        dropout2: float = 0.0,
    ):
        super().__init__()
        self.mlp = nn.Sequential(nn.SiLU(), nn.Linear(time_emb_dim, dim_out * 2)) if exists(time_emb_dim) else None

        self.block1 = Block(dim, dim_out, groups=groups, dropout=dropout1)
        self.block2 = Block(dim_out, dim_out, groups=groups, dropout=dropout2) if double_conv_layer else nn.Identity()
        self.residual_conv = nn.Conv2d(dim, dim_out, 1) if dim != dim_out else nn.Identity()

    def forward(self, x, time_emb=None):
        scale_shift = None
        if exists(self.mlp) and exists(time_emb):
            time_emb = self.mlp(time_emb)
            time_emb = rearrange(time_emb, "b c -> b c 1 1")
            scale_shift = time_emb.chunk(2, dim=1)

        h = self.block1(x, scale_shift=scale_shift)

        h = self.block2(h)

        return h + self.residual_conv(x)


# model
class Unet(BaseModel):
    def __init__(
        self,
        dim,
        init_dim=None,
        dim_mults=(1, 2, 4, 8),
        resnet_block_groups=8,
        with_time_emb: bool = False,
        time_dim_mult: int = 2,
        block_dropout: float = 0.0,  # for second block in resnet block
        block_dropout1: float = 0.0,  # for first block in resnet block
        attn_dropout: float = 0.0,
        input_dropout: float = 0.0,
        double_conv_layer: bool = True,
        learned_variance=False,
        learned_sinusoidal_cond=False,
        learned_sinusoidal_dim=16,
        outer_sample_mode: str = None,  # bilinear or nearest
        upsample_dims: tuple = None,  # (256, 256) or (128, 120) etc.
        keep_spatial_dims: bool = False,
        init_kernel_size: int = 7,
        init_padding: int = 3,
        init_stride: int = 1,
        num_conditions: int = 0,
        dim_head: int = 32,
        drop_path_rate=None,
        dropout_mlp=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        # determine dimensions
        assert self.num_input_channels is not None, "Please specify ``num_input_channels`` in the model config."
        assert self.num_output_channels is not None, "Please specify ``num_output_channels`` in the model config."
        assert (
            self.num_conditional_channels is not None
        ), "Please specify ``num_conditional_channels`` in the model config."
        # raise_error_if_invalid_value(conditioning_mode, ["concat", "cross_attn"], "conditioning_mode")
        if self.hparams.debug_mode:
            self.hparams.dim_mults = dim_mults = (1, 1, 1)
            self.hparams.dim = dim = 8
        input_channels = self.num_input_channels + self.num_conditional_channels
        output_channels = self.num_output_channels or input_channels
        self.save_hyperparameters()
        if dropout_mlp is not None or drop_path_rate is not None:
            self.log_text.warning(
                "You have specified ``dropout_mlp`` or ``drop_path_rate``. These are not used in the current model."
                "To affect the model's stochasticity, please use ``block_dropout`` or ``block_dropout1`` or ``attn_dropout``."
            )

        if num_conditions >= 1:
            assert (
                self.num_conditional_channels > 0
            ), f"num_conditions is {num_conditions} but num_conditional_channels is {self.num_conditional_channels}"

        init_dim = default(init_dim, dim)
        assert (upsample_dims is None and outer_sample_mode is None) or (
            upsample_dims is not None and outer_sample_mode is not None
        ), "upsample_dims and outer_sample_mode must be both None or both not None"
        # To keep spatial dimensions for uneven spatial sizes, we need to use nearest upsampling
        # and then crop the output to the desired size
        if outer_sample_mode is not None:
            # Upsample (45, 90) to be easier to divide by 2 multiple times
            # upsample_dims = (48, 96)
            self.upsampler = torch.nn.Upsample(size=tuple(upsample_dims), mode=outer_sample_mode)
        else:
            self.upsampler = None

        self.init_conv = nn.Conv2d(
            input_channels,
            init_dim,
            init_kernel_size,
            padding=init_padding,
            stride=init_stride,
        )
        self.dropout_input = nn.Dropout(input_dropout)
        self.dropout_input_for_residual = nn.Dropout(input_dropout)

        if with_time_emb:
            pos_emb_dim = dim
            sinusoidal_embedding = "learned" if learned_sinusoidal_cond else "true"
            self.time_dim = dim * time_dim_mult
            self.time_emb_mlp = get_time_embedder(
                self.time_dim, pos_emb_dim, sinusoidal_embedding, learned_sinusoidal_dim
            )
        else:
            self.time_dim = None
            self.time_emb_mlp = None

        dims = [init_dim, *map(lambda m: dim * m, dim_mults)]
        in_out = list(zip(dims[:-1], dims[1:]))

        block_klass = partial(
            ResnetBlock,
            groups=resnet_block_groups,
            dropout2=block_dropout,
            dropout1=block_dropout1,
            double_conv_layer=double_conv_layer,
            time_emb_dim=self.time_dim,
        )
        # layers
        self.downs = nn.ModuleList([])
        self.ups = nn.ModuleList([])
        num_resolutions = len(in_out)
        linear_attn_kwargs = dict(rescale="qkv", dropout=attn_dropout)
        for ind, (dim_in, dim_out) in enumerate(in_out):
            is_last = ind >= (num_resolutions - 1)
            do_downsample = not is_last and not keep_spatial_dims
            # num_heads = dim // dim_head
            num_heads, dim_head = 4, 32

            self.downs.append(
                nn.ModuleList(
                    [
                        block_klass(dim_in, dim_in),
                        block_klass(dim_in, dim_in),
                        (
                            Residual(
                                PreNorm(
                                    dim_in,
                                    fn=LinearAttention(
                                        dim_in, **linear_attn_kwargs, heads=num_heads, dim_head=dim_head
                                    ),
                                    norm=LayerNorm,
                                )
                            )
                        ),
                        Downsample(dim_in, dim_out) if do_downsample else nn.Conv2d(dim_in, dim_out, 3, padding=1),
                    ]
                )
            )

        mid_dim = dims[-1]
        # num_heads = mid_dim // dim_head
        num_heads, dim_head = 4, 32
        self.mid_block1 = block_klass(mid_dim, mid_dim)
        self.mid_attn = Residual(
            PreNorm(
                mid_dim,
                fn=Attention(mid_dim, dropout=attn_dropout, heads=num_heads, dim_head=dim_head),
                norm=LayerNorm,
            )
        )
        self.mid_block2 = block_klass(mid_dim, mid_dim)

        if hasattr(self, "spatial_shape_in") and self.spatial_shape_in is not None:
            b, s1, s2 = 1, *self.spatial_shape_in
            self.example_input_array = [
                torch.rand(b, self.num_input_channels, s1, s2),
                torch.rand(b) if with_time_emb else None,
                torch.rand(b, self.num_conditional_channels, s1, s2) if self.num_conditional_channels > 0 else None,
            ]

        for ind, (dim_in, dim_out) in enumerate(reversed(in_out)):
            is_last = ind == (len(in_out) - 1)
            do_upsample = not is_last and not keep_spatial_dims
            # num_heads = dim_out // dim_head
            num_heads, dim_head = 4, 32

            self.ups.append(
                nn.ModuleList(
                    [
                        block_klass(dim_out + dim_in, dim_out),
                        block_klass(dim_out + dim_in, dim_out),
                        (
                            Residual(
                                PreNorm(
                                    dim_out,
                                    fn=LinearAttention(
                                        dim_out, heads=num_heads, dim_head=dim_head, **linear_attn_kwargs
                                    ),
                                    norm=LayerNorm,
                                )
                            )
                        ),
                        Upsample(dim_out, dim_in) if do_upsample else nn.Conv2d(dim_out, dim_in, 3, padding=1),
                    ]
                )
            )

        default_out_dim = input_channels * (1 if not learned_variance else 2)
        self.out_dim = default(output_channels, default_out_dim)
        self.final_res_block = block_klass(dim * 2, dim)
        self.final_conv = self.get_head()

    def get_head(self):
        return nn.Conv2d(self.hparams.dim, self.out_dim, 1)

    def get_block(self, dim_in, dim_out, dropout: Optional[float] = None):
        return ResnetBlock(
            dim_in,
            dim_out,
            groups=self.hparams.resnet_block_groups,
            dropout1=dropout or self.hparams.block_dropout1,
            dropout2=dropout or self.hparams.block_dropout,
            time_emb_dim=self.time_dim,
        )

    def forward(
        self,
        inputs,
        time=None,
        condition=None,
        static_condition=None,
        return_time_emb: bool = False,
        get_intermediate_shapes: bool = False,
        **kwargs,
    ):
        x = self.concat_condition_if_needed(inputs, condition, static_condition)

        orig_x_shape = x.shape[-2:]
        x = self.upsampler(x) if exists(self.upsampler) else x
        try:
            x = self.init_conv(x)
        except RuntimeError as e:
            raise RuntimeError(
                f"x.shape: {x.shape}, x.dtype: {x.dtype}, init_conv.weight.shape/dtype: {self.init_conv.weight.shape}/{self.init_conv.weight.dtype}"
            ) from e
        r = self.dropout_input_for_residual(x) if self.hparams.input_dropout > 0 else x.clone()
        x = self.dropout_input(x)

        if exists(self.time_emb_mlp):
            try:
                t = self.time_emb_mlp(time)
            except RuntimeError as e:
                raise RuntimeError(
                    f"Error when embedding AdaLN input. {time.shape=}, {time.dtype=}, time_emb_mlp.weight.shape/dtype: {self.time_emb_mlp[1].weight.shape}/{self.time_emb_mlp[1].weight.dtype}"
                ) from e
        else:
            t = None

        h = []
        for i, (block1, block2, attn, downsample) in enumerate(self.downs):
            x = block1(x, t)
            h.append(x)

            x = block2(x, t)
            x = attn(x)
            h.append(x)

            x = downsample(x)
        x = self.mid_block1(x, t)
        x = self.mid_attn(x)
        # print(f'mid_attn: {x.shape}')  # e.g. [10, 256, 45, 90])
        x = self.mid_block2(x, t)
        if get_intermediate_shapes:
            return x

        for i, (block1, block2, attn, upsample) in enumerate(self.ups):
            x = torch.cat((x, h.pop()), dim=1)
            x = block1(x, t)

            x = torch.cat((x, h.pop()), dim=1)
            x = block2(x, t)
            x = attn(x)

            x = upsample(x)  # each i, except for last, halves channels, doubles spatial dims
            # print(f"Upsample {i} shape: {x.shape}.")

        x = torch.cat((x, r), dim=1)
        if exists(self.upsampler):
            # x = F.interpolate(x, orig_x_shape, mode='bilinear', align_corners=False)
            x = F.interpolate(x, size=orig_x_shape, mode=self.hparams.outer_sample_mode)

        x = self.final_res_block(x, t)
        x = self.final_conv(x)
        return_dict = x
        if return_time_emb:
            return return_dict, t
        return return_dict


if __name__ == "__main__":
    unet = Unet(
        dim=64,
        num_input_channels=3,
        num_output_channels=3,
        spatial_shape_in=(45, 90),
        upsample_dims=(48, 96),
        outer_sample_mode="bilinear",
    )
    x = torch.rand(10, 3, 45, 90)
    print(unet.print_intermediate_shapes(x))
