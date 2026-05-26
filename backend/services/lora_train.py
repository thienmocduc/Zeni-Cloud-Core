#!/usr/bin/env python3
"""
Zeni Cloud Core — LoRA Training Entrypoint (Vertex AI Custom Training).

Reads WebDataset shards from GCS, trains an SDXL or FLUX.1-dev LoRA adapter,
and uploads weights back to GCS. Designed to run in a Vertex AI Custom Job
with one A100 40GB (a2-highgpu-1g).

Usage:
    python lora_train.py \
        --dataset_gcs_uri gs://wellnexus-data-warehouse/v1/raw/ \
        --output_gcs_uri gs://witsagi-llm-lora/job-abc123/ \
        --base_model sdxl \
        --lora_rank 16 \
        --steps 4000 \
        --learning_rate 1e-4 \
        --resolution 1024 \
        --batch_size 1 \
        --gradient_accumulation_steps 4

References:
  - kohya-ss/sd-scripts: train_network.py
  - HuggingFace diffusers: examples/dreambooth/train_dreambooth_lora_sdxl.py
  - FLUX.1-dev LoRA: ostris/ai-toolkit

NOTE: This file is the CODE STRUCTURE. It will run end-to-end given the
correct deps (see ../services/training_dockerfile/Dockerfile), but actual
execution requires GPU access + chairman budget approval.
"""
from __future__ import annotations

import argparse
import io
import logging
import math
import os
import sys
import time
from contextlib import nullcontext
from pathlib import Path

# Deferred imports — only loaded if running on a GPU node with the right deps.
# Allows static analysis / linting without diffusers installed locally.
try:
    import torch
    from torch.utils.data import DataLoader
    import webdataset as wds
    from accelerate import Accelerator
    from accelerate.utils import set_seed
    from diffusers import (
        AutoencoderKL,
        DDPMScheduler,
        StableDiffusionXLPipeline,
        UNet2DConditionModel,
    )
    from diffusers.loaders import LoraLoaderMixin
    from diffusers.optimization import get_scheduler
    from peft import LoraConfig, get_peft_model
    from transformers import CLIPTextModel, CLIPTextModelWithProjection, CLIPTokenizer
    _DEPS_OK = True
except ImportError as e:
    _DEPS_OK = False
    _DEPS_ERR = str(e)

log = logging.getLogger("zeni.lora_train")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)


# ─── Argument parsing ──────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Zeni LoRA Trainer (SDXL / FLUX)")
    p.add_argument("--dataset_gcs_uri", type=str, required=True,
                   help="gs://bucket/prefix/ — WebDataset .tar shards")
    p.add_argument("--output_gcs_uri", type=str, required=True,
                   help="gs://bucket/prefix/ — output weights destination")
    p.add_argument("--base_model", type=str, default="sdxl",
                   choices=["sdxl", "flux"],
                   help="Base diffusion model family")
    p.add_argument("--base_model_path", type=str, default=None,
                   help="HF id or local path. Defaults: SDXL=stabilityai/stable-diffusion-xl-base-1.0, FLUX=black-forest-labs/FLUX.1-dev")
    p.add_argument("--lora_rank", type=int, default=16,
                   help="LoRA rank (8/16/32/64)")
    p.add_argument("--lora_alpha", type=int, default=None,
                   help="LoRA alpha (defaults to rank * 1.0)")
    p.add_argument("--steps", type=int, default=4000,
                   help="Total training steps")
    p.add_argument("--learning_rate", type=float, default=1e-4)
    p.add_argument("--resolution", type=int, default=1024)
    p.add_argument("--batch_size", type=int, default=1)
    p.add_argument("--gradient_accumulation_steps", type=int, default=4)
    p.add_argument("--mixed_precision", type=str, default="bf16",
                   choices=["no", "fp16", "bf16"])
    p.add_argument("--warmup_steps", type=int, default=200)
    p.add_argument("--save_every", type=int, default=500,
                   help="Save checkpoint every N steps")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--local_workdir", type=str, default="/tmp/lora_train")
    return p.parse_args()


# ─── GCS helpers ───────────────────────────────────────────────────────────
def list_webdataset_shards(gcs_uri: str) -> list[str]:
    """List .tar shards in a GCS prefix. Returns full gs:// URLs."""
    import gcsfs
    fs = gcsfs.GCSFileSystem()
    prefix = gcs_uri.replace("gs://", "").rstrip("/")
    paths = fs.glob(f"{prefix}/**/*.tar")
    return [f"gs://{p}" for p in paths]


def upload_to_gcs(local_path: Path, gcs_uri: str) -> None:
    """Upload a local file to gs://."""
    import gcsfs
    fs = gcsfs.GCSFileSystem()
    target = gcs_uri.rstrip("/") + "/" + local_path.name
    log.info("Uploading %s → %s", local_path, target)
    with open(local_path, "rb") as fsrc:
        with fs.open(target, "wb") as fdst:
            fdst.write(fsrc.read())


# ─── Dataset pipeline (WebDataset) ─────────────────────────────────────────
def build_dataset(shards: list[str], resolution: int, batch_size: int,
                  tokenizer_one, tokenizer_two):
    """Construct a WebDataset DataLoader for SDXL training.

    Expected shard schema (per sample):
        __key__:  unique id
        jpg:      image bytes
        txt:      caption (utf-8 text)
    """
    import torchvision.transforms as T

    image_tfm = T.Compose([
        T.Resize(resolution, interpolation=T.InterpolationMode.BILINEAR),
        T.CenterCrop(resolution),
        T.ToTensor(),
        T.Normalize([0.5], [0.5]),
    ])

    def _decode(sample: dict) -> dict:
        from PIL import Image
        img = Image.open(io.BytesIO(sample["jpg"])).convert("RGB")
        caption = sample["txt"].decode("utf-8") if isinstance(sample["txt"], bytes) else sample["txt"]
        # SDXL uses 2 tokenizers
        ids_one = tokenizer_one(caption, padding="max_length", max_length=77,
                                truncation=True, return_tensors="pt").input_ids[0]
        ids_two = tokenizer_two(caption, padding="max_length", max_length=77,
                                truncation=True, return_tensors="pt").input_ids[0]
        return {"pixel_values": image_tfm(img),
                "input_ids_one": ids_one,
                "input_ids_two": ids_two}

    ds = (
        wds.WebDataset(shards, shardshuffle=True, resampled=True)
           .shuffle(1000)
           .decode()
           .map(_decode)
           .batched(batch_size, partial=False)
    )
    return DataLoader(ds, batch_size=None, num_workers=4, pin_memory=True)


# ─── SDXL LoRA training loop ──────────────────────────────────────────────
def train_sdxl(args: argparse.Namespace) -> Path:
    base = args.base_model_path or "stabilityai/stable-diffusion-xl-base-1.0"
    log.info("[sdxl] loading base model: %s", base)

    accelerator = Accelerator(
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        mixed_precision=args.mixed_precision,
    )
    set_seed(args.seed)
    weight_dtype = {"fp16": torch.float16, "bf16": torch.bfloat16, "no": torch.float32}[args.mixed_precision]

    tokenizer_one = CLIPTokenizer.from_pretrained(base, subfolder="tokenizer")
    tokenizer_two = CLIPTokenizer.from_pretrained(base, subfolder="tokenizer_2")
    text_encoder_one = CLIPTextModel.from_pretrained(base, subfolder="text_encoder").to(accelerator.device, dtype=weight_dtype)
    text_encoder_two = CLIPTextModelWithProjection.from_pretrained(base, subfolder="text_encoder_2").to(accelerator.device, dtype=weight_dtype)
    vae = AutoencoderKL.from_pretrained(base, subfolder="vae").to(accelerator.device, dtype=weight_dtype)
    unet = UNet2DConditionModel.from_pretrained(base, subfolder="unet").to(accelerator.device, dtype=weight_dtype)
    noise_scheduler = DDPMScheduler.from_pretrained(base, subfolder="scheduler")

    # Freeze everything except LoRA
    for m in (text_encoder_one, text_encoder_two, vae, unet):
        m.requires_grad_(False)

    # ─── Wrap UNet with LoRA via peft ────────────────────
    lora_alpha = args.lora_alpha or args.lora_rank
    lora_config = LoraConfig(
        r=args.lora_rank,
        lora_alpha=lora_alpha,
        target_modules=["to_q", "to_k", "to_v", "to_out.0"],
        init_lora_weights="gaussian",
    )
    unet = get_peft_model(unet, lora_config)
    unet.print_trainable_parameters()

    optimizer = torch.optim.AdamW(
        [p for p in unet.parameters() if p.requires_grad],
        lr=args.learning_rate, betas=(0.9, 0.999), weight_decay=1e-2, eps=1e-8,
    )

    # ─── Dataset ────────────────────────────────────────
    shards = list_webdataset_shards(args.dataset_gcs_uri)
    log.info("[sdxl] dataset shards: %d", len(shards))
    if not shards:
        raise RuntimeError(f"No .tar shards under {args.dataset_gcs_uri}")
    train_loader = build_dataset(shards, args.resolution, args.batch_size,
                                  tokenizer_one, tokenizer_two)

    lr_scheduler = get_scheduler(
        "cosine",
        optimizer=optimizer,
        num_warmup_steps=args.warmup_steps * args.gradient_accumulation_steps,
        num_training_steps=args.steps * args.gradient_accumulation_steps,
    )

    unet, optimizer, train_loader, lr_scheduler = accelerator.prepare(
        unet, optimizer, train_loader, lr_scheduler
    )

    # ─── Training loop ──────────────────────────────────
    log.info("[sdxl] starting %d steps · lr=%g · rank=%d", args.steps, args.learning_rate, args.lora_rank)
    global_step = 0
    start_time = time.time()

    while global_step < args.steps:
        for batch in train_loader:
            with accelerator.accumulate(unet):
                pixel_values = batch["pixel_values"].to(accelerator.device, dtype=weight_dtype)

                # Encode images to latents
                with torch.no_grad():
                    latents = vae.encode(pixel_values).latent_dist.sample() * vae.config.scaling_factor

                # Sample noise + timesteps
                noise = torch.randn_like(latents)
                bsz = latents.shape[0]
                timesteps = torch.randint(0, noise_scheduler.config.num_train_timesteps, (bsz,),
                                          device=accelerator.device).long()
                noisy = noise_scheduler.add_noise(latents, noise, timesteps)

                # Text embeddings (dual-encoder SDXL)
                with torch.no_grad():
                    out1 = text_encoder_one(batch["input_ids_one"].to(accelerator.device), output_hidden_states=True)
                    out2 = text_encoder_two(batch["input_ids_two"].to(accelerator.device), output_hidden_states=True)
                    prompt_embeds = torch.cat([out1.hidden_states[-2], out2.hidden_states[-2]], dim=-1)
                    pooled = out2.text_embeds

                # SDXL micro-conditioning
                add_time_ids = torch.tensor(
                    [[args.resolution, args.resolution, 0, 0, args.resolution, args.resolution]],
                    device=accelerator.device, dtype=weight_dtype,
                ).repeat(bsz, 1)
                added_cond = {"text_embeds": pooled, "time_ids": add_time_ids}

                # Predict noise
                pred = unet(noisy, timesteps, encoder_hidden_states=prompt_embeds,
                            added_cond_kwargs=added_cond).sample

                target = noise
                loss = torch.nn.functional.mse_loss(pred.float(), target.float(), reduction="mean")

                accelerator.backward(loss)
                if accelerator.sync_gradients:
                    accelerator.clip_grad_norm_(unet.parameters(), 1.0)
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()

            if accelerator.sync_gradients:
                global_step += 1
                if global_step % 20 == 0:
                    elapsed = time.time() - start_time
                    sps = global_step / max(elapsed, 1)
                    eta_min = (args.steps - global_step) / max(sps, 0.001) / 60
                    log.info("[sdxl] step=%d/%d loss=%.4f sps=%.2f eta=%.1fmin",
                             global_step, args.steps, loss.item(), sps, eta_min)
                if global_step % args.save_every == 0 or global_step >= args.steps:
                    _save_lora_checkpoint(args, accelerator, unet, global_step)
                if global_step >= args.steps:
                    break

    # Final save
    out_dir = _save_lora_checkpoint(args, accelerator, unet, args.steps, final=True)
    log.info("[sdxl] DONE — total %.1fs", time.time() - start_time)
    return out_dir


# ─── FLUX LoRA training stub ──────────────────────────────────────────────
def train_flux(args: argparse.Namespace) -> Path:
    """
    FLUX.1-dev LoRA training entrypoint.

    Currently a stub — chairman TODO: implement using ostris/ai-toolkit pattern
    or HuggingFace diffusers FluxPipeline once it lands stable LoRA training
    support (diffusers ≥ 0.31).
    """
    log.warning("[flux] training NOT YET IMPLEMENTED — pattern hooks ready")
    log.info("[flux] would train with rank=%d steps=%d lr=%g",
             args.lora_rank, args.steps, args.learning_rate)
    raise NotImplementedError("FLUX.1-dev LoRA training requires diffusers >= 0.31 + chairman GPU budget approval")


# ─── Checkpoint save + upload ─────────────────────────────────────────────
def _save_lora_checkpoint(args, accelerator, unet, step: int, final: bool = False) -> Path:
    workdir = Path(args.local_workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    accelerator.wait_for_everyone()
    if not accelerator.is_main_process:
        return workdir

    name = "pytorch_lora_weights.safetensors" if final else f"step-{step:06d}.safetensors"
    target = workdir / name

    # Unwrap + save LoRA weights only
    unwrapped = accelerator.unwrap_model(unet)
    try:
        # peft path: save_pretrained writes adapter files; we want the consolidated safetensors
        lora_state = {k: v for k, v in unwrapped.state_dict().items() if "lora_" in k}
        from safetensors.torch import save_file
        save_file(lora_state, str(target))
    except Exception as e:
        log.exception("[save] safetensors save failed (%s) — falling back to torch.save", e)
        torch.save(unwrapped.state_dict(), str(target.with_suffix(".pt")))
        target = target.with_suffix(".pt")

    log.info("[save] checkpoint %s (%.1f MB)", target, target.stat().st_size / 1e6)
    if accelerator.is_main_process:
        try:
            upload_to_gcs(target, args.output_gcs_uri)
        except Exception as e:
            log.exception("[save] GCS upload failed (will retry on final): %s", e)
    return target


# ─── Main entry ────────────────────────────────────────────────────────────
def main() -> int:
    args = parse_args()
    log.info("=" * 72)
    log.info("Zeni Cloud · LoRA Training")
    log.info("base_model       = %s", args.base_model)
    log.info("dataset_gcs_uri  = %s", args.dataset_gcs_uri)
    log.info("output_gcs_uri   = %s", args.output_gcs_uri)
    log.info("lora_rank        = %d", args.lora_rank)
    log.info("steps            = %d", args.steps)
    log.info("learning_rate    = %g", args.learning_rate)
    log.info("resolution       = %d", args.resolution)
    log.info("batch_size       = %d (x%d grad accum = effective %d)",
             args.batch_size, args.gradient_accumulation_steps,
             args.batch_size * args.gradient_accumulation_steps)
    log.info("mixed_precision  = %s", args.mixed_precision)
    log.info("=" * 72)

    if not _DEPS_OK:
        log.error("Required deps not installed: %s", _DEPS_ERR)
        log.error("This script is meant to run in the Vertex AI training container.")
        log.error("See backend/services/training_dockerfile/Dockerfile.")
        return 2

    if args.base_model == "sdxl":
        train_sdxl(args)
    elif args.base_model == "flux":
        train_flux(args)
    else:
        raise ValueError(f"Unknown base_model: {args.base_model}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
