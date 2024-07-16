import numpy as np
import torch
import torch.nn as nn
import matplotlib.pyplot as plt 
from scipy.interpolate import griddata
from diffusers.utils import BaseOutput
from diffusers.configuration_utils import ConfigMixin, register_to_config
from diffusers.models.embeddings import GaussianFourierProjection, Timesteps, TimestepEmbedding
from diffusers.models.modeling_utils import ModelMixin
from diffusers.models.unets.unet_2d_blocks import UNetMidBlock2D, get_down_block, get_up_block
from dataclasses import dataclass
from typing import Optional, Tuple, Union

def get_grid_points_from_mask(batch_idx, channel_idx, mask):
    '''
    mask: torch.Tensor (B, C, H, W)
    '''
    if len(mask.shape) == 3:
        # maks (C, H, W)
        mask = mask.unsqueeze(0)
    flatten_batch_idx, flatten_channel_idx, flatten_y_idx, flatten_x_idx = torch.nonzero(mask, as_tuple=True)
    target_idx = torch.logical_and(flatten_batch_idx == batch_idx, flatten_channel_idx == channel_idx)
    return torch.column_stack((flatten_x_idx[target_idx], flatten_y_idx[target_idx]))

class vt_obs(object):
    def __init__(self, x_dim, y_dim, x_spacing, y_spacing, known_channels=None, device='cpu'):
        self.x_dim = x_dim
        self.y_dim = y_dim
        self.x_spacing = x_spacing
        self.y_spacing = y_spacing
        self.x_list = torch.arange(0, x_dim-x_spacing+1, x_spacing, device=device)
        self.y_list = torch.arange(0, y_dim-y_spacing+1, y_spacing, device=device)
        self.x_start_grid, self.y_start_grid = torch.meshgrid(self.x_list, self.y_list, indexing='ij')
        self.grid_x, self.grid_y = torch.meshgrid(torch.linspace(0, x_dim-1, x_dim), 
                                                  torch.linspace(0, y_dim-1, y_dim),
                                                    indexing='xy')
        self.known_channels = known_channels
        self.device = device

        self.x_start_grid = self.x_start_grid.to(device)
        self.y_start_grid = self.y_start_grid.to(device)
        self.grid_x = self.grid_x.to(device)
        self.grid_y = self.grid_y.to(device)

    @torch.no_grad()
    def structure_obs(self, generator=None):
        x_offset = torch.randint(0, self.x_spacing, self.x_start_grid.shape,
                                 device=self.device, generator=generator)
        y_offset = torch.randint(0, self.y_spacing, self.y_start_grid.shape,
                                 device=self.device, generator=generator)
        x_coords = (self.x_start_grid + x_offset).flatten()
        y_coords = (self.y_start_grid + y_offset).flatten()
        return x_coords, y_coords

    @torch.no_grad()
    def _get_grid_points(self, x_coords=None, y_coords=None, generator=None):
        if x_coords is None and y_coords is None:
            x_coords, y_coords = self.structure_obs(generator=generator)
        return torch.column_stack((x_coords, y_coords)) 

    @torch.no_grad()
    def _torch_griddata_nearest(self, points, values, xi):
        distances = torch.cdist(xi, points)
        nearest_indices = torch.argmin(distances, dim=1)
        interpolated_values = values[nearest_indices]
        return interpolated_values.reshape(self.y_dim, self.x_dim)

    @torch.no_grad()
    def interpolate(self, grid_points, field):
        '''
        return griddata(
            grid_points,
            field,
            (self.grid_x, self.grid_y),
            method='nearest'
        )
        '''
        return self._torch_griddata_nearest(
            grid_points.float(), 
            field, 
            torch.stack((self.grid_x.flatten(), self.grid_y.flatten()), dim=1).float(),
        )
    
    def _plot_vt(self, known_fields, mask=None, x_coords=None, y_coords=None):
        '''
        known_fields: (C, H, W)
        mask: (C, H, W)
        mask_channel_idx: int, if using same mask, input the corresponding channel index
        '''
        C, H, W = known_fields.shape
        if mask is None:
            grid_points = self._get_grid_points(x_coords=x_coords, y_coords=y_coords).to(self.device)
        in_channels = C if self.known_channels is None else len(self.known_channels)
        interpolated_fields = torch.zeros(in_channels, self.y_dim, self.x_dim, dtype=known_fields.dtype)
        for idx, known_channel in enumerate(range(C) if self.known_channels is None else self.known_channels):
            if mask is not None:
                grid_points = get_grid_points_from_mask(0, known_channel, mask).to(self.device)
            field = known_fields[known_channel][grid_points[:,1], grid_points[:,0]].flatten()
            interpolated_values = self.interpolate(grid_points, field)
            interpolated_fields[idx] = torch.tensor(interpolated_values, 
                                                    dtype=known_fields.dtype, 
                                                    device=self.device)
        if x_coords is None and y_coords is None:
            x_coords, y_coords = grid_points[:,0], grid_points[:,1] 
        fig, axs = plt.subplots(in_channels, 1, figsize=(4, 4*in_channels))
        if in_channels == 1:
            axs = [axs]
        for c in range(in_channels):
            im = axs[c].imshow(interpolated_fields[c].cpu().numpy(), cmap='jet')
            axs[c].scatter(x_coords.cpu().numpy(), y_coords.cpu().numpy(), c='r', s=1)
            fig.colorbar(im, ax=axs[c])
        plt.tight_layout()
        plt.show()

    @torch.no_grad()
    def __call__(self, known_fields, mask=None, x_coords=None, y_coords=None, generator=None):
        # known_fields: (B, C, H, W)
        B, C, _, _ = known_fields.shape
        in_channels = C if self.known_channels is None else len(self.known_channels)
        interpolated_fields = torch.zeros(B, in_channels, self.y_dim, self.x_dim, device=known_fields.device, dtype=known_fields.dtype)

        for b in range(B):
            if mask is None:
                grid_points = self._get_grid_points(x_coords=x_coords, y_coords=y_coords, generator=generator).to(self.device)

            for idx, known_channel in enumerate(range(C) if self.known_channels is None else self.known_channels):
                if mask is not None:
                    grid_points = get_grid_points_from_mask(b, known_channel, mask).to(self.device)
                field = known_fields[b, known_channel][grid_points[:,1], grid_points[:,0]].flatten()
                interpolated_values = self.interpolate(grid_points, field)
                interpolated_fields[b, idx] = torch.tensor(interpolated_values, 
                                                        dtype=known_fields.dtype, 
                                                        device=self.device)

        return interpolated_fields
        

@dataclass
class UNet2DOutput(BaseOutput):
    """
    The output of [`UNet2DModel`].

    Args:
        sample (`torch.Tensor` of shape `(batch_size, num_channels, height, width)`):
            The hidden states output from the last layer of the model.
    """

    sample: torch.Tensor


class diffuserUNet2D(ModelMixin, ConfigMixin):
    r"""
    A 2D UNet model that takes a noisy sample and a timestep and returns a sample shaped output.

    This model inherits from [`ModelMixin`]. Check the superclass documentation for it's generic methods implemented
    for all models (such as downloading or saving).

    Parameters:
        sample_size (`int` or `Tuple[int, int]`, *optional*, defaults to `None`):
            Height and width of input/output sample. Dimensions must be a multiple of `2 ** (len(block_out_channels) -
            1)`.
        in_channels (`int`, *optional*, defaults to 3): Number of channels in the input sample.
        out_channels (`int`, *optional*, defaults to 3): Number of channels in the output.
        center_input_sample (`bool`, *optional*, defaults to `False`): Whether to center the input sample.
        freq_shift (`int`, *optional*, defaults to 0): Frequency shift for Fourier time embedding.
        flip_sin_to_cos (`bool`, *optional*, defaults to `True`):
            Whether to flip sin to cos for Fourier time embedding.
        down_block_types (`Tuple[str]`, *optional*, defaults to `("DownBlock2D", "AttnDownBlock2D", "AttnDownBlock2D", "AttnDownBlock2D")`):
            Tuple of downsample block types.
        mid_block_type (`str`, *optional*, defaults to `"UNetMidBlock2D"`):
            Block type for middle of UNet, it can be either `UNetMidBlock2D` or `UnCLIPUNetMidBlock2D`.
        up_block_types (`Tuple[str]`, *optional*, defaults to `("AttnUpBlock2D", "AttnUpBlock2D", "AttnUpBlock2D", "UpBlock2D")`):
            Tuple of upsample block types.
        block_out_channels (`Tuple[int]`, *optional*, defaults to `(224, 448, 672, 896)`):
            Tuple of block output channels.
        layers_per_block (`int`, *optional*, defaults to `2`): The number of layers per block.
        mid_block_scale_factor (`float`, *optional*, defaults to `1`): The scale factor for the mid block.
        downsample_padding (`int`, *optional*, defaults to `1`): The padding for the downsample convolution.
        downsample_type (`str`, *optional*, defaults to `conv`):
            The downsample type for downsampling layers. Choose between "conv" and "resnet"
        upsample_type (`str`, *optional*, defaults to `conv`):
            The upsample type for upsampling layers. Choose between "conv" and "resnet"
        dropout (`float`, *optional*, defaults to 0.0): The dropout probability to use.
        act_fn (`str`, *optional*, defaults to `"silu"`): The activation function to use.
        attention_head_dim (`int`, *optional*, defaults to `8`): The attention head dimension.
        norm_num_groups (`int`, *optional*, defaults to `32`): The number of groups for normalization.
        attn_norm_num_groups (`int`, *optional*, defaults to `None`):
            If set to an integer, a group norm layer will be created in the mid block's [`Attention`] layer with the
            given number of groups. If left as `None`, the group norm layer will only be created if
            `resnet_time_scale_shift` is set to `default`, and if created will have `norm_num_groups` groups.
        norm_eps (`float`, *optional*, defaults to `1e-5`): The epsilon for normalization.
        resnet_time_scale_shift (`str`, *optional*, defaults to `"default"`): Time scale shift config
            for ResNet blocks (see [`~models.resnet.ResnetBlock2D`]). Choose from `default` or `scale_shift`.
        class_embed_type (`str`, *optional*, defaults to `None`):
            The type of class embedding to use which is ultimately summed with the time embeddings. Choose from `None`,
            `"timestep"`, or `"identity"`.
        num_class_embeds (`int`, *optional*, defaults to `None`):
            Input dimension of the learnable embedding matrix to be projected to `time_embed_dim` when performing class
            conditioning with `class_embed_type` equal to `None`.
    """

    @register_to_config
    def __init__(
        self,
        sample_size: Optional[Union[int, Tuple[int, int]]] = None,
        in_channels: int = 3,
        out_channels: int = 3,
        center_input_sample: bool = False,
        time_embedding_type: str = "positional",
        freq_shift: int = 0,
        flip_sin_to_cos: bool = True,
        down_block_types: Tuple[str, ...] = ("DownBlock2D", "AttnDownBlock2D", "AttnDownBlock2D", "AttnDownBlock2D"),
        up_block_types: Tuple[str, ...] = ("AttnUpBlock2D", "AttnUpBlock2D", "AttnUpBlock2D", "UpBlock2D"),
        block_out_channels: Tuple[int, ...] = (224, 448, 672, 896),
        layers_per_block: int = 2,
        mid_block_scale_factor: float = 1,
        downsample_padding: int = 1,
        downsample_type: str = "conv",
        upsample_type: str = "conv",
        dropout: float = 0.0,
        act_fn: str = "silu",
        attention_head_dim: Optional[int] = 8,
        norm_num_groups: int = 32,
        attn_norm_num_groups: Optional[int] = None,
        norm_eps: float = 1e-5,
        resnet_time_scale_shift: str = "default",
        add_attention: bool = True,
        class_embed_type: Optional[str] = None,
        num_class_embeds: Optional[int] = None,
        num_train_timesteps: Optional[int] = None,
    ):
        super().__init__()

        self.sample_size = sample_size
        time_embed_dim = block_out_channels[0] * 4 if num_class_embeds is not None else None

        # Check inputs
        if len(down_block_types) != len(up_block_types):
            raise ValueError(
                f"Must provide the same number of `down_block_types` as `up_block_types`. `down_block_types`: {down_block_types}. `up_block_types`: {up_block_types}."
            )

        if len(block_out_channels) != len(down_block_types):
            raise ValueError(
                f"Must provide the same number of `block_out_channels` as `down_block_types`. `block_out_channels`: {block_out_channels}. `down_block_types`: {down_block_types}."
            )

        # input
        self.conv_in = nn.Conv2d(in_channels, block_out_channels[0], kernel_size=3, padding=(1, 1))

        # class embedding
        if class_embed_type is None and num_class_embeds is not None:
            self.class_embedding = nn.Embedding(num_class_embeds, time_embed_dim)
        elif class_embed_type == "identity":
            self.class_embedding = nn.Identity(time_embed_dim, time_embed_dim)
        else:
            self.class_embedding = None

        self.down_blocks = nn.ModuleList([])
        self.mid_block = None
        self.up_blocks = nn.ModuleList([])

        # down
        output_channel = block_out_channels[0]
        for i, down_block_type in enumerate(down_block_types):
            input_channel = output_channel
            output_channel = block_out_channels[i]
            is_final_block = i == len(block_out_channels) - 1

            down_block = get_down_block(
                down_block_type,
                num_layers=layers_per_block,
                in_channels=input_channel,
                out_channels=output_channel,
                temb_channels=time_embed_dim,
                add_downsample=not is_final_block,
                resnet_eps=norm_eps,
                resnet_act_fn=act_fn,
                resnet_groups=norm_num_groups,
                attention_head_dim=attention_head_dim if attention_head_dim is not None else output_channel,
                downsample_padding=downsample_padding,
                resnet_time_scale_shift=resnet_time_scale_shift,
                downsample_type=downsample_type,
                dropout=dropout,
            )
            self.down_blocks.append(down_block)

        # mid
        self.mid_block = UNetMidBlock2D(
            in_channels=block_out_channels[-1],
            temb_channels=time_embed_dim,
            dropout=dropout,
            resnet_eps=norm_eps,
            resnet_act_fn=act_fn,
            output_scale_factor=mid_block_scale_factor,
            resnet_time_scale_shift=resnet_time_scale_shift,
            attention_head_dim=attention_head_dim if attention_head_dim is not None else block_out_channels[-1],
            resnet_groups=norm_num_groups,
            attn_groups=attn_norm_num_groups,
            add_attention=add_attention,
        )

        # up
        reversed_block_out_channels = list(reversed(block_out_channels))
        output_channel = reversed_block_out_channels[0]
        for i, up_block_type in enumerate(up_block_types):
            prev_output_channel = output_channel
            output_channel = reversed_block_out_channels[i]
            input_channel = reversed_block_out_channels[min(i + 1, len(block_out_channels) - 1)]

            is_final_block = i == len(block_out_channels) - 1

            up_block = get_up_block(
                up_block_type,
                num_layers=layers_per_block + 1,
                in_channels=input_channel,
                out_channels=output_channel,
                prev_output_channel=prev_output_channel,
                temb_channels=time_embed_dim,
                add_upsample=not is_final_block,
                resnet_eps=norm_eps,
                resnet_act_fn=act_fn,
                resnet_groups=norm_num_groups,
                attention_head_dim=attention_head_dim if attention_head_dim is not None else output_channel,
                resnet_time_scale_shift=resnet_time_scale_shift,
                upsample_type=upsample_type,
                dropout=dropout,
            )
            self.up_blocks.append(up_block)
            prev_output_channel = output_channel

        # out
        num_groups_out = norm_num_groups if norm_num_groups is not None else min(block_out_channels[0] // 4, 32)
        self.conv_norm_out = nn.GroupNorm(num_channels=block_out_channels[0], num_groups=num_groups_out, eps=norm_eps)
        self.conv_act = nn.SiLU()
        self.conv_out = nn.Conv2d(block_out_channels[0], out_channels, kernel_size=3, padding=1)

    def forward(
        self,
        sample: torch.Tensor,
        class_labels: Optional[torch.Tensor] = None,
        return_dict: bool = True,
    ) -> Union[UNet2DOutput, Tuple]:
        r"""
        The [`UNet2DModel`] forward method.

        Args:
            sample (`torch.Tensor`):
                The noisy input tensor with the following shape `(batch, channel, height, width)`.
            timestep (`torch.Tensor` or `float` or `int`): The number of timesteps to denoise an input.
            class_labels (`torch.Tensor`, *optional*, defaults to `None`):
                Optional class labels for conditioning. Their embeddings will be summed with the timestep embeddings.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~models.unet_2d.UNet2DOutput`] instead of a plain tuple.

        Returns:
            [`~models.unet_2d.UNet2DOutput`] or `tuple`:
                If `return_dict` is True, an [`~models.unet_2d.UNet2DOutput`] is returned, otherwise a `tuple` is
                returned where the first element is the sample tensor.
        """
        # 0. center input if necessary
        if self.config.center_input_sample:
            sample = 2 * sample - 1.0

        emb = None

        if self.class_embedding is not None:
            if class_labels is None:
                raise ValueError("class_labels should be provided when doing class conditioning")

            class_emb = self.class_embedding(class_labels).to(dtype=self.dtype)
            emb = class_emb
        elif self.class_embedding is None and class_labels is not None:
            raise ValueError("class_embedding needs to be initialized in order to use class conditioning")

        # 2. pre-process
        skip_sample = sample
        sample = self.conv_in(sample)

        # 3. down
        down_block_res_samples = (sample,)
        for downsample_block in self.down_blocks:
            if hasattr(downsample_block, "skip_conv"):
                sample, res_samples, skip_sample = downsample_block(
                    hidden_states=sample, temb=emb, skip_sample=skip_sample
                )
            else:
                sample, res_samples = downsample_block(hidden_states=sample, temb=emb)

            down_block_res_samples += res_samples

        # 4. mid
        sample = self.mid_block(sample, emb)

        # 5. up
        skip_sample = None
        for upsample_block in self.up_blocks:
            res_samples = down_block_res_samples[-len(upsample_block.resnets) :]
            down_block_res_samples = down_block_res_samples[: -len(upsample_block.resnets)]

            if hasattr(upsample_block, "skip_conv"):
                sample, skip_sample = upsample_block(sample, res_samples, emb, skip_sample)
            else:
                sample = upsample_block(sample, res_samples, emb)

        # 6. post-process
        sample = self.conv_norm_out(sample)
        sample = self.conv_act(sample)
        sample = self.conv_out(sample)

        if skip_sample is not None:
            sample += skip_sample

        if not return_dict:
            return (sample,)

        return UNet2DOutput(sample=sample)