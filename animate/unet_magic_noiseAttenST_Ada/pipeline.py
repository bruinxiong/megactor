# Copyright 2024 Megvii inc.
#
# Copyright (2024) MegActor Authors.
#
# Megvii Inc. retain all intellectual property and proprietary rights in 
# and to this material, related documentation and any modifications thereto. 
# Any use, reproduction, disclosure or distribution of this material and related 
# documentation without an express license agreement from Megvii Inc. is strictly prohibited.

# Adapted from https://github.com/showlab/Tune-A-Video/blob/main/tuneavideo/pipelines/pipeline_tuneavideo.py

# Copyright 2023 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
TODO:
1. support multi-controlnet
2. [DONE] support DDIM inversion
3. support Prompt-to-prompt
"""

import inspect
import math
from typing import Callable, List, Optional, Union
from dataclasses import dataclass
from PIL import Image
import numpy as np
import torch
import torch.distributed as dist
from tqdm import tqdm
from diffusers.utils import is_accelerate_available
from packaging import version
from transformers import CLIPTextModel, CLIPTokenizer

from diffusers.configuration_utils import FrozenDict
from diffusers.models import AutoencoderKL
from diffusers import DiffusionPipeline
from diffusers.schedulers import (
    DDIMScheduler,
    DPMSolverMultistepScheduler,
    EulerAncestralDiscreteScheduler,
    EulerDiscreteScheduler,
    LMSDiscreteScheduler,
    PNDMScheduler,
)
from diffusers.utils import deprecate, logging, BaseOutput

from einops import rearrange

from .unet_controlnet import UNet3DConditionModel
from .controlnet import ControlNetModel
from .mutual_self_attention import ReferenceAttentionControl
from animate.context import (
    get_context_scheduler,
    get_total_steps
)
from animate.utils.util import get_tensor_interpolation_method

logger = logging.get_logger(__name__)  # pylint: disable=invalid-name


@dataclass
class AnimationPipelineOutput(BaseOutput):
    videos: Union[torch.Tensor, np.ndarray]

# two input source_image is (-1, 1)
# control is (0, 1)

class AnimationPipeline(DiffusionPipeline):
    _optional_components = []

    def __init__(
            self,
            vae: AutoencoderKL,
            text_encoder: CLIPTextModel,
            tokenizer: CLIPTokenizer,
            unet: UNet3DConditionModel,
            controlnet: ControlNetModel,
            scheduler: Union[
                DDIMScheduler,
                PNDMScheduler,
                LMSDiscreteScheduler,
                EulerDiscreteScheduler,
                EulerAncestralDiscreteScheduler,
                DPMSolverMultistepScheduler,
            ],
    ):
        super().__init__()

        if hasattr(scheduler.config, "steps_offset") and scheduler.config.steps_offset != 1:
            deprecation_message = (
                f"The configuration file of this scheduler: {scheduler} is outdated. `steps_offset`"
                f" should be set to 1 instead of {scheduler.config.steps_offset}. Please make sure "
                "to update the config accordingly as leaving `steps_offset` might led to incorrect results"
                " in future versions. If you have downloaded this checkpoint from the Hugging Face Hub,"
                " it would be very nice if you could open a Pull request for the `scheduler/scheduler_config.json`"
                " file"
            )
            deprecate("steps_offset!=1", "1.0.0",
                      deprecation_message, standard_warn=False)
            new_config = dict(scheduler.config)
            new_config["steps_offset"] = 1
            scheduler._internal_dict = FrozenDict(new_config)

        if hasattr(scheduler.config, "clip_sample") and scheduler.config.clip_sample is True:
            deprecation_message = (
                f"The configuration file of this scheduler: {scheduler} has not set the configuration `clip_sample`."
                " `clip_sample` should be set to False in the configuration file. Please make sure to update the"
                " config accordingly as not setting `clip_sample` in the config might lead to incorrect results in"
                " future versions. If you have downloaded this checkpoint from the Hugging Face Hub, it would be very"
                " nice if you could open a Pull request for the `scheduler/scheduler_config.json` file"
            )
            deprecate("clip_sample not set", "1.0.0",
                      deprecation_message, standard_warn=False)
            new_config = dict(scheduler.config)
            new_config["clip_sample"] = False
            scheduler._internal_dict = FrozenDict(new_config)

        is_unet_version_less_0_9_0 = hasattr(unet.config, "_diffusers_version") and version.parse(
            version.parse(unet.config._diffusers_version).base_version
        ) < version.parse("0.9.0.dev0")
        is_unet_sample_size_less_64 = hasattr(
            unet.config, "sample_size") and unet.config.sample_size < 64
        if is_unet_version_less_0_9_0 and is_unet_sample_size_less_64:
            deprecation_message = (
                "The configuration file of the unet has set the default `sample_size` to smaller than"
                " 64 which seems highly unlikely. If your checkpoint is a fine-tuned version of any of the"
                " following: \n- CompVis/stable-diffusion-v1-4 \n- CompVis/stable-diffusion-v1-3 \n-"
                " CompVis/stable-diffusion-v1-2 \n- CompVis/stable-diffusion-v1-1 \n- runwayml/stable-diffusion-v1-5"
                " \n- runwayml/stable-diffusion-inpainting \n you should change 'sample_size' to 64 in the"
                " configuration file. Please make sure to update the config accordingly as leaving `sample_size=32`"
                " in the config might lead to incorrect results in future versions. If you have downloaded this"
                " checkpoint from the Hugging Face Hub, it would be very nice if you could open a Pull request for"
                " the `unet/config.json` file"
            )
            deprecate("sample_size<64", "1.0.0",
                      deprecation_message, standard_warn=False)
            new_config = dict(unet.config)
            new_config["sample_size"] = 64
            unet._internal_dict = FrozenDict(new_config)

        self.register_modules(
            vae=vae,
            text_encoder=text_encoder,
            tokenizer=tokenizer,
            unet=unet,
            controlnet=controlnet,
            scheduler=scheduler,
        )
        self.vae_scale_factor = 2 ** (
            len(self.vae.config.block_out_channels) - 1)

    def enable_vae_slicing(self):
        self.vae.enable_slicing()

    def disable_vae_slicing(self):
        self.vae.disable_slicing()

    def enable_sequential_cpu_offload(self, gpu_id=0):
        if is_accelerate_available():
            from accelerate import cpu_offload
        else:
            raise ImportError(
                "Please install accelerate via `pip install accelerate`")

        device = torch.device(f"cuda:{gpu_id}")

        for cpu_offloaded_model in [self.unet, self.text_encoder, self.vae]:
            if cpu_offloaded_model is not None:
                cpu_offload(cpu_offloaded_model, device)

    @property
    def _execution_device(self):
        if self.device != torch.device("meta") or not hasattr(self.unet, "_hf_hook"):
            return self.device
        for module in self.unet.modules():
            if (
                    hasattr(module, "_hf_hook")
                    and hasattr(module._hf_hook, "execution_device")
                    and module._hf_hook.execution_device is not None
            ):
                return torch.device(module._hf_hook.execution_device)
        return self.device

    def _encode_prompt(self, prompt, device, num_videos_per_prompt, do_classifier_free_guidance, negative_prompt):
        batch_size = len(prompt) if isinstance(prompt, list) else 1

        text_inputs = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=self.tokenizer.model_max_length,
            truncation=True,
            return_tensors="pt",
        )
        text_input_ids = text_inputs.input_ids
        untruncated_ids = self.tokenizer(
            prompt, padding="longest", return_tensors="pt").input_ids

        if untruncated_ids.shape[-1] >= text_input_ids.shape[-1] and not torch.equal(text_input_ids, untruncated_ids):
            removed_text = self.tokenizer.batch_decode(
                untruncated_ids[:, self.tokenizer.model_max_length - 1: -1])
            logger.warning(
                "The following part of your input was truncated because CLIP can only handle sequences up to"
                f" {self.tokenizer.model_max_length} tokens: {removed_text}"
            )

        if hasattr(self.text_encoder.config, "use_attention_mask") and self.text_encoder.config.use_attention_mask:
            attention_mask = text_inputs.attention_mask.to(device)
        else:
            attention_mask = None

        text_embeddings = self.text_encoder(
            text_input_ids.to(device),
            attention_mask=attention_mask,
        )
        text_embeddings = text_embeddings[0]

        # duplicate text embeddings for each generation per prompt, using mps friendly method
        bs_embed, seq_len, _ = text_embeddings.shape
        text_embeddings = text_embeddings.repeat(1, num_videos_per_prompt, 1)
        text_embeddings = text_embeddings.view(
            bs_embed * num_videos_per_prompt, seq_len, -1)

        # get unconditional embeddings for classifier free guidance
        if do_classifier_free_guidance:
            uncond_tokens: List[str]
            if negative_prompt is None:
                uncond_tokens = [""] * batch_size
            elif type(prompt) is not type(negative_prompt):
                raise TypeError(
                    f"`negative_prompt` should be the same type to `prompt`, but got {type(negative_prompt)} !="
                    f" {type(prompt)}."
                )
            elif isinstance(negative_prompt, str):
                uncond_tokens = [negative_prompt]
            elif batch_size != len(negative_prompt):
                raise ValueError(
                    f"`negative_prompt`: {negative_prompt} has batch size {len(negative_prompt)}, but `prompt`:"
                    f" {prompt} has batch size {batch_size}. Please make sure that passed `negative_prompt` matches"
                    " the batch size of `prompt`."
                )
            else:
                uncond_tokens = negative_prompt

            max_length = text_input_ids.shape[-1]
            uncond_input = self.tokenizer(
                uncond_tokens,
                padding="max_length",
                max_length=max_length,
                truncation=True,
                return_tensors="pt",
            )

            if hasattr(self.text_encoder.config, "use_attention_mask") and self.text_encoder.config.use_attention_mask:
                attention_mask = uncond_input.attention_mask.to(device)
            else:
                attention_mask = None

            uncond_embeddings = self.text_encoder(
                uncond_input.input_ids.to(device),
                attention_mask=attention_mask,
            )
            uncond_embeddings = uncond_embeddings[0]

            # duplicate unconditional embeddings for each generation per prompt, using mps friendly method
            seq_len = uncond_embeddings.shape[1]
            uncond_embeddings = uncond_embeddings.repeat(
                1, num_videos_per_prompt, 1)
            uncond_embeddings = uncond_embeddings.view(
                batch_size * num_videos_per_prompt, seq_len, -1)

            # For classifier free guidance, we need to do two forward passes.
            # Here we concatenate the unconditional and text embeddings into a single batch
            # to avoid doing two forward passes
            text_embeddings = torch.cat([uncond_embeddings, text_embeddings])
        return text_embeddings

    def decode_latents(self, latents, rank, decoder_consistency=None):
        video_length = latents.shape[2]
        latents = 1 / 0.18215 * latents
        latents = rearrange(latents, "b c f h w -> (b f) c h w")
        # video = self.vae.decode(latents).sample
        video = []
        for frame_idx in tqdm(range(latents.shape[0]), disable=(rank != 0)):
            if decoder_consistency is not None:
                video.append(decoder_consistency(
                    latents[frame_idx:frame_idx + 1]))
            else:
                video.append(self.vae.decode(
                    latents[frame_idx:frame_idx + 1]).sample)
        video = torch.cat(video)
        video = rearrange(video, "(b f) c h w -> b c f h w", f=video_length)
        video = (video / 2 + 0.5).clamp(0, 1)
        # we always cast to float32 as this does not cause significant overhead and is compatible with bfloa16
        video = video.cpu().float().numpy()
        return video

    def prepare_extra_step_kwargs(self, generator, eta):
        # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
        # eta (η) is only used with the DDIMScheduler, it will be ignored for other schedulers.
        # eta corresponds to η in DDIM paper: https://arxiv.org/abs/2010.02502
        # and should be between [0, 1]

        accepts_eta = "eta" in set(inspect.signature(
            self.scheduler.step).parameters.keys())
        extra_step_kwargs = {}
        if accepts_eta:
            extra_step_kwargs["eta"] = eta

        # check if the scheduler accepts generator
        accepts_generator = "generator" in set(
            inspect.signature(self.scheduler.step).parameters.keys())
        if accepts_generator:
            extra_step_kwargs["generator"] = generator
        return extra_step_kwargs

    def check_inputs(self, prompt, height, width, callback_steps):
        if not isinstance(prompt, str) and not isinstance(prompt, list):
            raise ValueError(
                f"`prompt` has to be of type `str` or `list` but is {type(prompt)}")

        if height % 8 != 0 or width % 8 != 0:
            raise ValueError(
                f"`height` and `width` have to be divisible by 8 but are {height} and {width}.")

        if (callback_steps is None) or (
                callback_steps is not None and (not isinstance(
                    callback_steps, int) or callback_steps <= 0)
        ):
            raise ValueError(
                f"`callback_steps` has to be a positive integer but is {callback_steps} of type"
                f" {type(callback_steps)}."
            )

    def prepare_latents(self, batch_size, num_channels_latents, video_length, height, width, dtype, device, generator,
                        latents=None, clip_length=16):
        shape = (
            batch_size, num_channels_latents, clip_length, height // self.vae_scale_factor,
            width // self.vae_scale_factor)
        if isinstance(generator, list) and len(generator) != batch_size:
            raise ValueError(
                f"You have passed a list of generators of length {len(generator)}, but requested an effective batch"
                f" size of {batch_size}. Make sure the batch size matches the length of the generators."
            )
        if latents is None:
            rand_device = "cpu" if device.type == "mps" else device

            if isinstance(generator, list):
                latents = [
                    torch.randn(
                        shape, generator=generator[i], device=rand_device, dtype=dtype)
                    for i in range(batch_size)
                ]
                latents = torch.cat(latents, dim=0).to(device)
            else:
                # print("arrive init noise latent !!!!!!!!!!!!")
                latents = torch.randn(
                    shape, generator=generator, device=rand_device, dtype=dtype).to(device)
                # latents = latents + 0.1 * torch.randn(latents.shape[0], latents.shape[1], 1, 1, 1, generator=generator, device=rand_device, dtype=dtype).to(device)

            latents = latents.repeat(1, 1, video_length // clip_length, 1, 1)
        else:
            if latents.shape != shape:
                raise ValueError(
                    f"Unexpected latents shape, got {latents.shape}, expected {shape}")
            latents = latents.to(device)

        # scale the initial noise by the standard deviation required by the scheduler
        latents = latents * self.scheduler.init_noise_sigma
        return latents

    def prepare_condition(self, condition, num_videos_per_prompt, device, dtype, do_classifier_free_guidance):
        # prepare conditions for controlnet
        # condition = torch.from_numpy(condition.copy()).to(
        #     device=device, dtype=dtype) / 255.0
        # condition = torch.stack(
        #     [condition for _ in range(num_videos_per_prompt)], dim=0)
        # condition = rearrange(condition, 'b f h w c -> (b f) c h w').clone()
        # if do_classifier_free_guidance:
        #     condition = torch.cat([condition] * 2)

        
        condition = torch.concat(
            [condition for _ in range(num_videos_per_prompt)], dim=0)
        condition = rearrange(condition, 'b f h w c -> (b f) c h w')
        if do_classifier_free_guidance:
            condition = torch.cat([condition] * 2)
        return condition

    def next_step(
            self,
            model_output: torch.FloatTensor,
            timestep: int,
            x: torch.FloatTensor,
            eta=0.,
            verbose=False
    ):
        """
        Inverse sampling for DDIM Inversion
        """
        if verbose:
            print("timestep: ", timestep)
        next_step = timestep
        timestep = min(timestep - self.scheduler.config.num_train_timesteps //
                       self.scheduler.num_inference_steps, 999)
        alpha_prod_t = self.scheduler.alphas_cumprod[
            timestep] if timestep >= 0 else self.scheduler.final_alpha_cumprod
        alpha_prod_t_next = self.scheduler.alphas_cumprod[next_step]
        beta_prod_t = 1 - alpha_prod_t
        pred_x0 = (x - beta_prod_t ** 0.5 * model_output) / alpha_prod_t ** 0.5
        pred_dir = (1 - alpha_prod_t_next) ** 0.5 * model_output
        x_next = alpha_prod_t_next ** 0.5 * pred_x0 + pred_dir
        return x_next, pred_x0

    @torch.no_grad()
    def images2latents(self, images, dtype):
        """
        Convert RGB image to VAE latents
        """
        device = self._execution_device
        images = torch.from_numpy(images).float().to(dtype) / 127.5 - 1
        images = rearrange(images, "f h w c -> f c h w").to(device)
        latents = []
        for frame_idx in range(images.shape[0]):
            latents.append(self.vae.encode(
                images[frame_idx:frame_idx + 1])['latent_dist'].mean * 0.18215)
        latents = torch.cat(latents)
        return latents

    @torch.no_grad()
    def invert(
            self,
            image: torch.Tensor,
            prompt,
            num_inference_steps=20,
            num_actual_inference_steps=10,
            eta=0.0,
            return_intermediates=False,
            **kwargs):
        """
        Adapted from: https://github.com/Yujun-Shi/DragDiffusion/blob/main/drag_pipeline.py#L440
        invert a real image into noise map with determinisc DDIM inversion
        """
        device = self._execution_device
        batch_size = image.shape[0]
        if isinstance(prompt, list):
            if batch_size == 1:
                image = image.expand(len(prompt), -1, -1, -1)
        elif isinstance(prompt, str):
            if batch_size > 1:
                prompt = [prompt] * batch_size

        # text embeddings
        text_input = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=77,
            return_tensors="pt"
        )
        text_embeddings = self.text_encoder(text_input.input_ids.to(device))[0]
        print("input text embeddings :", text_embeddings.shape)
        # define initial latents
        latents = self.images2latents(image)

        print("latents shape: ", latents.shape)
        # interative sampling
        self.scheduler.set_timesteps(num_inference_steps)
        print("Valid timesteps: ", reversed(self.scheduler.timesteps))
        latents_list = [latents]
        pred_x0_list = [latents]
        for i, t in enumerate(tqdm(reversed(self.scheduler.timesteps), desc="DDIM Inversion")):

            if num_actual_inference_steps is not None and i >= num_actual_inference_steps:
                continue
            model_inputs = latents

            # predict the noise
            # NOTE: the u-net here is UNet3D, therefore the model_inputs need to be of shape (b c f h w)
            model_inputs = rearrange(model_inputs, "f c h w -> 1 c f h w")
            noise_pred = self.unet(
                model_inputs, t, encoder_hidden_states=text_embeddings).sample
            noise_pred = rearrange(noise_pred, "b c f h w -> (b f) c h w")

            # compute the previous noise sample x_t-1 -> x_t
            latents, pred_x0 = self.next_step(noise_pred, t, latents)
            latents_list.append(latents)
            pred_x0_list.append(pred_x0)

        if return_intermediates:
            # return the intermediate laters during inversion
            return latents, latents_list
        return latents

    def interpolate_latents(self, latents: torch.Tensor, interpolation_factor: int, device):
        if interpolation_factor < 2:
            return latents

        new_latents = torch.zeros(
            (latents.shape[0], latents.shape[1], ((latents.shape[2] - 1) * interpolation_factor) + 1, latents.shape[3],
             latents.shape[4]),
            device=latents.device,
            dtype=latents.dtype,
        )

        org_video_length = latents.shape[2]
        rate = [
            i / interpolation_factor for i in range(interpolation_factor)][1:]

        new_index = 0

        v0 = None
        v1 = None

        for i0, i1 in zip(range(org_video_length), range(org_video_length)[1:]):
            v0 = latents[:, :, i0, :, :]
            v1 = latents[:, :, i1, :, :]

            new_latents[:, :, new_index, :, :] = v0
            new_index += 1

            for f in rate:
                v = get_tensor_interpolation_method()(
                    v0.to(device=device), v1.to(device=device), f)
                new_latents[:, :, new_index, :, :] = v.to(latents.device)
                new_index += 1

        new_latents[:, :, new_index, :, :] = v1
        new_index += 1

        return new_latents

    def select_controlnet_res_samples(self, controlnet_res_samples_cache_dict, context, do_classifier_free_guidance, b,
                                      f):
        _down_block_res_samples = []
        _mid_block_res_sample = []
        for i in np.concatenate(np.array(context)):
            _down_block_res_samples.append(
                controlnet_res_samples_cache_dict[i][0])
            _mid_block_res_sample.append(
                controlnet_res_samples_cache_dict[i][1])
        down_block_res_samples = [[] for _ in range(
            len(controlnet_res_samples_cache_dict[i][0]))]
        for res_t in _down_block_res_samples:
            for i, res in enumerate(res_t):
                down_block_res_samples[i].append(res)
        down_block_res_samples = [torch.cat(res)
                                  for res in down_block_res_samples]
        mid_block_res_sample = torch.cat(_mid_block_res_sample)

        # reshape controlnet output to match the unet3d inputs
        b = b // 2 if do_classifier_free_guidance else b
        _down_block_res_samples = []
        for sample in down_block_res_samples:
            sample = rearrange(sample, '(b f) c h w -> b c f h w', b=b, f=f)
            if do_classifier_free_guidance:
                sample = sample.repeat(2, 1, 1, 1, 1)
            _down_block_res_samples.append(sample)
        down_block_res_samples = _down_block_res_samples
        mid_block_res_sample = rearrange(
            mid_block_res_sample, '(b f) c h w -> b c f h w', b=b, f=f)
        if do_classifier_free_guidance:
            mid_block_res_sample = mid_block_res_sample.repeat(2, 1, 1, 1, 1)

        return down_block_res_samples, mid_block_res_sample

    @torch.no_grad()
    def __call__(
            self,
            prompt: Union[str, List[str]],
            video_length: Optional[int],
            prompt_embeddings: Optional[torch.FloatTensor] = None,
            height: Optional[int] = None,
            width: Optional[int] = None,
            num_inference_steps: int = 50,
            guidance_scale: float = 7.5,
            negative_prompt: Optional[Union[str, List[str]]] = None,
            num_videos_per_prompt: Optional[int] = 1,
            eta: float = 0.0,
            generator: Optional[Union[torch.Generator,
                                      List[torch.Generator]]] = None,
            latents: Optional[torch.FloatTensor] = None,
            output_type: Optional[str] = "tensor",
            return_dict: bool = True,
            callback: Optional[Callable[[
                int, int, torch.FloatTensor], None]] = None,
            callback_steps: Optional[int] = 1,
            controlnet_condition: list = None,
            controlnet_conditioning_scale: float = 1.0,
            context_frames: int = 16,
            context_stride: int = 1,
            context_overlap: int = 4,
            context_batch_size: int = 1,
            context_schedule: str = "uniform",
            init_latents: Optional[torch.FloatTensor] = None,
            num_actual_inference_steps: Optional[int] = None,
            appearance_encoder=None,
            reference_control_writer=None,
            reference_control_reader=None,
            source_image: str = None,
            decoder_consistency=None,
            froce_text_embedding_zero=False,
            ref_concat_image_noises_latents=None,
            do_classifier_free_guidance=True,
            add_noise_image_type="",
            ref_img_condition=None,
            show_progressbar=False,
            **kwargs,
    ):
        """
        New args:
        - controlnet_condition          : condition map (e.g., depth, canny, keypoints) for controlnet
        - controlnet_conditioning_scale : conditioning scale for controlnet
        - init_latents                  : initial latents to begin with (used along with invert())
        - num_actual_inference_steps    : number of actual inference steps (while total steps is num_inference_steps)
        """

        # Default height and width to unet
        height = height or self.unet.config.sample_size * self.vae_scale_factor
        width = width or self.unet.config.sample_size * self.vae_scale_factor

        # Check inputs. Raise error if not correct
        # self.check_inputs(prompt, height, width, callback_steps)

        # Define call parameters
        # batch_size = 1 if isinstance(prompt, str) else len(prompt)
        batch_size = 1
        if latents is not None:
            batch_size = latents.shape[0]
        if isinstance(prompt, list):
            batch_size = len(prompt)

        device = self._execution_device
        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        if guidance_scale > 1.0 and do_classifier_free_guidance:
            do_classifier_free_guidance = True
            print('this inference use classifier_free_guidance')
        else:
            do_classifier_free_guidance = False
            print('this inference not use classifier_free_guidance')

        # Encode input prompt
        '''
        source_image0 torch.Size([1, 512, 512, 3])
        control0 torch.Size([1, 40, 512, 512, 3])
        image_prompts torch.Size([2, 257, 1280])
        prompt_embeddings torch.Size([2, 257, 1280])
        text_embeddings torch.Size([2, 64, 768])
        controlnet_text_embeddings torch.Size([80, 64, 768])
        '''
        # print('prompt_embeddings', prompt_embeddings)
        if prompt_embeddings is None:
            prompt = prompt if isinstance(prompt, list) else [
                prompt] * batch_size
            if negative_prompt is not None:
                negative_prompt = negative_prompt if isinstance(negative_prompt, list) else [
                    negative_prompt] * batch_size
            text_embeddings = self._encode_prompt(
                prompt, device, num_videos_per_prompt, do_classifier_free_guidance, negative_prompt
            )
            text_embeddings = torch.cat([text_embeddings] * context_batch_size)
        else:
            # print('prompt_embeddings shape is', prompt_embeddings.shape)
            # print('num_videos_per_prompt is', num_videos_per_prompt)
            # project from (batch_size, 257, 1280) to (batch_size, 16, 768)
            # with torch.inference_mode():
            #     prompt_embeddings = self.image_proj_model(
            #         prompt_embeddings)
            text_embeddings = torch.cat(
                [prompt_embeddings] * context_batch_size)
        
        # print('text_embeddings shape is', text_embeddings.shape)

        if froce_text_embedding_zero:
            text_embeddings = torch.zeros_like(text_embeddings)
        
        if appearance_encoder is not None:
            reference_control_writer = ReferenceAttentionControl(appearance_encoder,
                                                                do_classifier_free_guidance=do_classifier_free_guidance,
                                                                mode='write',
                                                                batch_size=context_batch_size,
                                                                clip_length=16,
                                                                )
            reference_control_reader = ReferenceAttentionControl(self.unet,
                                                                do_classifier_free_guidance=do_classifier_free_guidance,
                                                                mode='read',
                                                                batch_size=context_batch_size,
                                                                clip_length=16,
                                                                )

        is_dist_initialized = kwargs.get("dist", False)
        rank = kwargs.get("rank", 0)
        world_size = kwargs.get("world_size", 1)

        # Prepare video
        # FIXME: verify if num_videos_per_prompt > 1 works
        assert num_videos_per_prompt == 1
        assert batch_size == 1  # FIXME: verify if batch_size > 1 works
        # print('infer controlnet_condition target unique is (0, 1), real is', controlnet_condition.unique())
        # print('infer source_image target unique is (-1, 1), real is', source_image.unique())
        control = self.prepare_condition(
            condition=controlnet_condition,
            device=device,
            dtype=appearance_encoder.dtype,
            num_videos_per_prompt=num_videos_per_prompt,
            do_classifier_free_guidance=do_classifier_free_guidance,
        )
        if do_classifier_free_guidance:
            controlnet_uncond_images, controlnet_cond_images = control.chunk(2)
        else:
            controlnet_cond_images = control

        # print('infer controlnet_cond_images unique is (0, 1) real is', controlnet_cond_images.unique())
        # print('infer source_image unique is (-1, 1) real is', source_image.unique())

        # Prepare timesteps
        self.scheduler.set_timesteps(num_inference_steps, device=device)
        timesteps = self.scheduler.timesteps

        # Prepare latent variables
        if init_latents is not None:
            latents = rearrange(
                init_latents, "(b f) c h w -> b c f h w", f=video_length)
        else:
            num_channels_latents = self.unet.in_channels
            latents = self.prepare_latents(
                batch_size * num_videos_per_prompt,
                num_channels_latents,
                video_length,
                height,
                width,
                text_embeddings.dtype,
                device,
                generator,
                latents,
                clip_length=video_length
            )
        latents_dtype = latents.dtype


        # Prepare extra step kwargs.
        extra_step_kwargs = self.prepare_extra_step_kwargs(generator, eta)

        # For img2img setting
        if num_actual_inference_steps is None:
            num_actual_inference_steps = num_inference_steps

        source_image = rearrange(
                source_image, "f h w c -> f c h w").to(device)
        ref_image_latents = self.vae.encode(
            source_image)['latent_dist'].mean * 0.18215
        
        if add_noise_image_type != "":
            latents = latents * 0.9 + ref_image_latents.unsqueeze(2).repeat(1, 1, video_length, 1, 1) * 0.1 * self.scheduler.init_noise_sigma

        # if batch_size == 1:
        #     ref_image_latents = ref_image_latents[:1]
        context_scheduler = get_context_scheduler(context_schedule)
        if do_classifier_free_guidance and ref_img_condition is not None:
            ref_img_condition = torch.cat([ref_img_condition, ref_img_condition])
        
        # Denoising loop
        for i, t in tqdm(enumerate(timesteps), total=len(timesteps), disable=(rank != 0 or not show_progressbar)):
            if num_actual_inference_steps is not None and i < num_inference_steps - num_actual_inference_steps:
                continue

            noise_pred = torch.zeros(
                (latents.shape[0] * (2 if do_classifier_free_guidance else 1),
                 *latents.shape[1:]),
                device=latents.device,
                dtype=latents.dtype,
            )
            counter = torch.zeros(
                (1, 1, latents.shape[2], 1, 1), device=latents.device, dtype=latents.dtype
            )

            if appearance_encoder is not None:
                appearance_encoder(
                    ref_image_latents.repeat(
                        context_batch_size * (2 if do_classifier_free_guidance else 1), 1, 1, 1),
                    t,
                    encoder_hidden_states=text_embeddings,
                    return_dict=False,
                )
           
            context_queue = list(context_scheduler(
                0, num_inference_steps, latents.shape[2], context_frames, context_stride, context_overlap
            ))

            num_context_batches = math.ceil(
                len(context_queue) / context_batch_size)
            global_context = []
            for i in range(num_context_batches):
                global_context.append(
                    context_queue[i * context_batch_size: (i + 1) * context_batch_size])

            for context in global_context[rank::world_size]:
                latent_model_input = (
                    torch.cat([latents[:, :, c] for c in context])
                    .to(device)
                    .repeat(2 if do_classifier_free_guidance else 1, 1, 1, 1, 1)
                )

                latent_model_input = self.scheduler.scale_model_input(
                    latent_model_input, t)

                controlnet_cond=torch.cat(
                        [controlnet_cond_images[c] for c in context])

                if appearance_encoder is not None:
                    reference_control_reader.update(reference_control_writer)
        
                if ref_concat_image_noises_latents is not None:
                    ref_back_latent_input = (
                        torch.cat([ref_concat_image_noises_latents[:, :, c] for c in context])
                        .to(device)
                    )
                    latent_model_input = torch.cat([latent_model_input, ref_back_latent_input], dim=1)
                
                pose_guide_conditions = rearrange(controlnet_cond, "(b f) c h w -> b c f h w", b=context_batch_size)
                if do_classifier_free_guidance:
                    pose_guide_conditions = torch.cat([pose_guide_conditions, pose_guide_conditions])

                # DEBUG SHOW PART OF IMAGE
                # from animatediff.utils.util import save_videos_grid
                # import os
                # if not os.path.exists("./check_conditions_eval.gif"):
                #     save_videos_grid(pose_guide_conditions[:1].cpu(), "./check_conditions_eval.gif")
                #     ref_image = 1 / 0.18215 * ref_image_latents
                #     ref_image = self.vae.decode(ref_image).sample
                #     ref_image = (ref_image / 2 + 0.5).clamp(0, 1)
                #     save_videos_grid(ref_image.unsqueeze(2).cpu(), "./check_ref_image_eval.gif")
                pred = self.unet(
                    latent_model_input,
                    t,
                    encoder_hidden_states=text_embeddings,
                    down_block_additional_residuals=None,
                    mid_block_additional_residual=None,
                    return_dict=False,
                    pose_guide_conditions=pose_guide_conditions,
                    ref_img_conditions=ref_img_condition,
                )[0]

                if appearance_encoder is not None:
                    reference_control_reader.clear()

                if do_classifier_free_guidance:
                    pred_uc, pred_c = pred.chunk(2)
                    pred = torch.cat([pred_uc.unsqueeze(0), pred_c.unsqueeze(0)])
                else:
                    pred = pred.unsqueeze(1)
                    
                for j, c in enumerate(context):
                    noise_pred[:, :, c] = noise_pred[:, :, c] + pred[:, j]
                    counter[:, :, c] = counter[:, :, c] + 1

            if is_dist_initialized:
                noise_pred_gathered = [torch.zeros_like(
                    noise_pred) for _ in range(world_size)]
                if rank == 0:
                    dist.gather(tensor=noise_pred,
                                gather_list=noise_pred_gathered, dst=0)
                else:
                    dist.gather(tensor=noise_pred, gather_list=[], dst=0)
                dist.barrier()

                if rank == 0:
                    for k in range(1, world_size):
                        for context in global_context[k::world_size]:
                            for j, c in enumerate(context):
                                noise_pred[:, :, c] = noise_pred[:, :,
                                                                 c] + noise_pred_gathered[k][:, :, c]
                                counter[:, :, c] = counter[:, :, c] + 1

            # perform guidance
            if do_classifier_free_guidance:
                noise_pred_uncond, noise_pred_text = (
                    noise_pred / counter).chunk(2)
                noise_pred = noise_pred_uncond + guidance_scale * \
                    (noise_pred_text - noise_pred_uncond)
                # noise_pred = noise_pred_text
                
            latents = self.scheduler.step(
                noise_pred, t, latents, **extra_step_kwargs).prev_sample
            

            if is_dist_initialized:
                dist.broadcast(latents, 0)
                dist.barrier()

            if appearance_encoder is not None:
                reference_control_writer.clear()

        interpolation_factor = 1
        latents = self.interpolate_latents(
            latents, interpolation_factor, device)
        # Post-processing
        video = self.decode_latents(
            latents, rank, decoder_consistency=decoder_consistency)

        if is_dist_initialized:
            dist.barrier()

        # Convert to tensor
        if output_type == "tensor":
            video = torch.from_numpy(video)

        if not return_dict:
            return video

        return AnimationPipelineOutput(videos=video)

    def train(
            self,
            prompt: Union[str, List[str]],
            prompt_embeddings: Optional[torch.FloatTensor] = None,
            video_length: Optional[int] = 8,
            height: Optional[int] = 512,
            width: Optional[int] = 512,
            timestep: Union[torch.Tensor, float, int] = None,
            negative_prompt: Optional[Union[str, List[str]]] = None,
            num_videos_per_prompt: Optional[int] = 1,
            generator: Optional[Union[torch.Generator,
                                      List[torch.Generator]]] = None,
            latents: Optional[torch.FloatTensor] = None,
            callback_steps: Optional[int] = 1,
            controlnet_condition: list = None,
            controlnet_conditioning_scale: float = 1.0,
            init_latents: Optional[torch.FloatTensor] = None,
            appearance_encoder=None,
            source_image: str = None,
            decoder_consistency=None,
            context_frames: int = 16,
            context_batch_size: int = 1,
            froce_text_embedding_zero = False,
            add_noise_image_type = "",
            ref_img_conditions = None,
            **kwargs,
    ):
        """
        New args:
        - controlnet_condition          : condition map (e.g., depth, canny, keypoints) for controlnet
        - controlnet_conditioning_scale : conditioning scale for controlnet
        - init_latents                  : initial latents to begin with (used along with invert())
        - num_actual_inference_steps    : number of actual inference steps (while total steps is num_inference_steps)
        """
        controlnet = self.controlnet

        # Default height and width to unet
        height = height or self.unet.config.sample_size * self.vae_scale_factor
        width = width or self.unet.config.sample_size * self.vae_scale_factor

        # Check inputs. Raise error if not correct
        # self.check_inputs(prompt, height, width, callback_steps)

        # Define call parameters
        # batch_size = 1 if isinstance(prompt, str) else len(prompt)
        batch_size = 1
        if latents is not None:
            batch_size = latents.shape[0]
        if isinstance(prompt, list):
            batch_size = len(prompt)
        if init_latents is not None:
            batch_size = init_latents.shape[0]

        device = self._execution_device
        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        do_classifier_free_guidance = False

        # Encode input prompt
        text_embeddings = torch.cat(
            [prompt_embeddings] * 1)
        
        # text_embeddings = text_embeddings.repeat(1, num_videos_per_prompt, 1)
        # text_embeddings = text_embeddings.view(
        #     bs_embed * num_videos_per_prompt, seq_len, -1)
        # text_embeddings = prompt_embeddings
        # text_embeddings = torch.cat([prompt_embeddings] * context_batch_size)
    

        if froce_text_embedding_zero:
            text_embeddings = torch.zeros_like(text_embeddings)
        """
        LLZ TODO
        context_frames: int = 16,
        context_batch_size: int = 1,
        """
        # LLZ TODO:这里是batchsize吗 context batchsize的概念是？
        if appearance_encoder is not None:
            self.reference_control_writer = ReferenceAttentionControl(appearance_encoder, do_classifier_free_guidance=False,
                                                                mode='write', batch_size=context_batch_size, clip_length=context_frames)
            self.reference_control_reader = ReferenceAttentionControl(self.unet, do_classifier_free_guidance=False, mode='read',
                                                                batch_size=context_batch_size, clip_length=context_frames)

        # Prepare video
        # FIXME: verify if num_videos_per_prompt > 1 works
        assert num_videos_per_prompt == 1
        # assert batch_size == 1  # FIXME: verify if batch_size > 1 works

        # print('train controlnet_condition target unique is (0, 1), real is', controlnet_condition.unique())
        # print('train source_image target unique is (-1, 1), real is', source_image.unique())
        control = self.prepare_condition(
            condition=controlnet_condition,
            device=device,
            dtype=text_embeddings.dtype,
            num_videos_per_prompt=num_videos_per_prompt,
            do_classifier_free_guidance=do_classifier_free_guidance,
        )

        # Prepare latent variables
        if init_latents is None:
            # latents = rearrange(init_latents, "(b f) c h w -> b c f h w", f=video_length)
            num_channels_latents = self.unet.in_channels


            latents = self.prepare_latents(
                batch_size * num_videos_per_prompt,
                num_channels_latents,
                video_length,
                height,
                width,
                text_embeddings.dtype,
                device,
                generator,
                latents,
                clip_length=video_length
            )
        else:
            latents = init_latents
        del init_latents
        latents_dtype = latents.dtype

        pose_guide_conditions = rearrange(control, "(b f) c h w -> b c f h w", b=context_batch_size)

        # Prepare text embeddings for controlnet
        # controlnet_text_embeddings_c = text_embeddings.repeat_interleave(
        #     video_length, 0)

        # print('train controlnet_cond_images unique is', controlnet_cond_images.unique())
        # print('train source_image unique is', source_image.unique())
        
        # prepare controlnet condition input
        ref_image_latents = self.vae.encode(
            source_image)['latent_dist'].mean * 0.18215

        t = timestep

        # print('text_embeddings', text_embeddings.shape)
        
        """
        ref_image_latents torch.Size([2, 4, 64, 64])                                                                                                                                                  │····················
        text_embeddings torch.Size([1, 77, 768]) 
        """

        if appearance_encoder is not None:
            appearance_encoder(
                ref_image_latents,
                t,
                encoder_hidden_states=text_embeddings,
                return_dict=False,
            )


        if appearance_encoder is not None:
            self.reference_control_reader.update(self.reference_control_writer)

        # DEBUG SHOW PART OF IMAGE
        # from animatediff.utils.util import save_videos_grid
        # import os
        # if not os.path.exists("./check_conditions.gif"):
        #     save_videos_grid(pose_guide_conditions.cpu(), "./check_conditions.gif")
        #     video = self.decode_latents(
        #         latents, device, decoder_consistency=decoder_consistency)
        #     save_videos_grid(torch.tensor(video), "./check_video.gif")
        #     ref_image_latents = 1 / 0.18215 * ref_image_latents
        #     ref_image = self.vae.decode(ref_image_latents).sample
        #     ref_image = (ref_image / 2 + 0.5).clamp(0, 1)
        #     save_videos_grid(ref_image.unsqueeze(2).cpu(), "./check_ref_image.gif")

        # predict the noise residual
        noise_pred = self.unet(
            latents,
            t,
            encoder_hidden_states=text_embeddings,
            down_block_additional_residuals=None,
            mid_block_additional_residual=None,
            return_dict=False,
            pose_guide_conditions=pose_guide_conditions,
            ref_img_conditions=ref_img_conditions,
        )[0]

        return noise_pred
    
    def clear_reference_control(self):
        if hasattr(self, "reference_control_reader"):
            self.reference_control_reader.clear()
            self.reference_control_writer.clear()
            self.reference_control_reader = None
            self.reference_control_writer = None
