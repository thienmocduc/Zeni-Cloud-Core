#!/usr/bin/env python3
"""
Vertex AI Custom Training — LoRA fine-tune for design styles.

Train SDXL LoRA trên Vertex AI GPU (NVIDIA L4 hoặc A100 spot) cho 5 styles:
  1. Indochine VN     (200K ảnh kiến trúc/nội thất Việt Nam)
  2. Japandi          (150K ảnh Japanese/Scandinavian)
  3. Tropical Villa   (100K ảnh nhiệt đới)
  4. Luxury Indochine (80K ảnh cao cấp)
  5. Industrial Loft  (100K ảnh loft công nghiệp)

Cost ước tính (1 LoRA):
  - L4 spot:  $0.30/h × 12h = $3.6
  - A100 spot: $0.50/h × 6h = $3.0
  - V100 spot: $0.40/h × 8h = $3.2

Total 5 LoRA: ~$15-18 (trả bằng $300 GCP credits).

Usage:
  # Submit single LoRA job
  python vertex_train_lora.py \\
      --style indochine \\
      --dataset gs://zeni-data-warehouse/v1/curated/indochine \\
      --output gs://zeni-data-warehouse/v1/models/indochine-lora

  # Submit all 5 styles parallel
  python vertex_train_lora.py --all
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("zeni.vertex_train")


# Style configs
STYLE_CONFIGS = {
    "indochine": {
        "trigger_word": "zeni_indochine",
        "target_size": 200_000,
        "rank": 64,
        "epochs": 8,
        "lr": 1e-4,
        "expected_hours": 12,
    },
    "japandi": {
        "trigger_word": "zeni_japandi",
        "target_size": 150_000,
        "rank": 48,
        "epochs": 10,
        "lr": 1e-4,
        "expected_hours": 10,
    },
    "tropical": {
        "trigger_word": "zeni_tropical",
        "target_size": 100_000,
        "rank": 32,
        "epochs": 12,
        "lr": 1e-4,
        "expected_hours": 8,
    },
    "luxury": {
        "trigger_word": "zeni_luxury",
        "target_size": 80_000,
        "rank": 32,
        "epochs": 14,
        "lr": 8e-5,
        "expected_hours": 7,
    },
    "industrial": {
        "trigger_word": "zeni_industrial",
        "target_size": 100_000,
        "rank": 32,
        "epochs": 12,
        "lr": 1e-4,
        "expected_hours": 8,
    },
}


def submit_training_job(
    *,
    style: str,
    dataset_uri: str,
    output_uri: str,
    project: str = "zeni-cloud-core",
    region: str = "us-central1",
    machine_type: str = "g2-standard-8",
    accelerator: str = "NVIDIA_L4",
    accelerator_count: int = 1,
    image_uri: str | None = None,
) -> dict:
    """Submit Vertex AI Custom Training Job.

    Uses L4 spot by default (best price/perf, $0.30/h).
    """
    try:
        from google.cloud import aiplatform
    except ImportError:
        log.error("Install: pip install google-cloud-aiplatform")
        return {}

    aiplatform.init(project=project, location=region)

    cfg = STYLE_CONFIGS.get(style)
    if not cfg:
        raise ValueError(f"Unknown style: {style}. Allowed: {list(STYLE_CONFIGS)}")

    if image_uri is None:
        # Default training container (kohya_ss SDXL LoRA — community-standard)
        image_uri = f"us-central1-docker.pkg.dev/{project}/zeni/sdxl-lora-trainer:latest"

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    display_name = f"zeni-lora-{style}-{timestamp}"

    log.info("Submitting LoRA training job: %s", display_name)
    log.info("  Style:        %s", style)
    log.info("  Trigger:      %s", cfg["trigger_word"])
    log.info("  Rank:         %d", cfg["rank"])
    log.info("  Epochs:       %d", cfg["epochs"])
    log.info("  LR:           %g", cfg["lr"])
    log.info("  Expected hrs: %d", cfg["expected_hours"])
    log.info("  Machine:      %s + %dx %s", machine_type, accelerator_count, accelerator)
    log.info("  Dataset:      %s", dataset_uri)
    log.info("  Output:       %s", output_uri)

    job = aiplatform.CustomContainerTrainingJob(
        display_name=display_name,
        container_uri=image_uri,
        command=["python", "/workspace/train_lora.py"],
    )

    # Hyperparams injected as args
    args = [
        f"--style={style}",
        f"--trigger_word={cfg['trigger_word']}",
        f"--dataset_uri={dataset_uri}",
        f"--output_uri={output_uri}",
        f"--rank={cfg['rank']}",
        f"--epochs={cfg['epochs']}",
        f"--lr={cfg['lr']}",
        "--mixed_precision=fp16",
        "--gradient_checkpointing=true",
    ]

    # Run with spot pricing (60-91% discount vs on-demand)
    job.run(
        args=args,
        machine_type=machine_type,
        accelerator_type=accelerator,
        accelerator_count=accelerator_count,
        replica_count=1,
        boot_disk_size_gb=200,
        sync=False,  # don't block — submit & return job ID
        # Spot configuration (Vertex AI Spot VMs available since 2024)
        # scheduling=aiplatform.JobScheduling(
        #     restart_job_on_worker_restart=True,
        #     timeout=cfg["expected_hours"] * 3600 * 2,  # 2x safety
        # ),
    )

    log.info("Job submitted. State: %s", job.state)
    log.info("Console: https://console.cloud.google.com/vertex-ai/training/custom-jobs?project=%s", project)

    return {
        "job_name": display_name,
        "resource_name": job.resource_name,
        "state": str(job.state),
        "console_url": f"https://console.cloud.google.com/vertex-ai/training/custom-jobs?project={project}",
    }


def submit_all_styles(
    *,
    dataset_root: str = "gs://zeni-data-warehouse/v1/curated",
    output_root: str = "gs://zeni-data-warehouse/v1/models",
    **kwargs,
) -> list[dict]:
    """Fire 5 LoRA training jobs parallel."""
    results = []
    for style in STYLE_CONFIGS:
        dataset_uri = f"{dataset_root}/{style}"
        output_uri = f"{output_root}/{style}-lora"
        try:
            result = submit_training_job(
                style=style,
                dataset_uri=dataset_uri,
                output_uri=output_uri,
                **kwargs,
            )
            results.append(result)
        except Exception as e:
            log.error("Failed to submit %s: %s", style, e)
            results.append({"style": style, "error": str(e)})
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--style", choices=list(STYLE_CONFIGS) + ["all"],
                        default="indochine")
    parser.add_argument("--dataset", help="GCS URI to curated dataset")
    parser.add_argument("--output", help="GCS URI for output LoRA weights")
    parser.add_argument("--project", default="zeni-cloud-core")
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--machine-type", default="g2-standard-8",
                        help="g2-standard-8 (L4) or a2-highgpu-1g (A100)")
    parser.add_argument("--accelerator", default="NVIDIA_L4",
                        choices=["NVIDIA_L4", "NVIDIA_A100_80GB", "NVIDIA_TESLA_T4"])
    parser.add_argument("--accelerator-count", type=int, default=1)
    args = parser.parse_args()

    if args.style == "all":
        results = submit_all_styles(
            project=args.project,
            region=args.region,
            machine_type=args.machine_type,
            accelerator=args.accelerator,
            accelerator_count=args.accelerator_count,
        )
        log.info("All 5 jobs submitted: %s",
                 [r.get("job_name") or r.get("error") for r in results])
    else:
        if not args.dataset or not args.output:
            parser.error("--dataset and --output required when --style != 'all'")
        result = submit_training_job(
            style=args.style,
            dataset_uri=args.dataset,
            output_uri=args.output,
            project=args.project,
            region=args.region,
            machine_type=args.machine_type,
            accelerator=args.accelerator,
            accelerator_count=args.accelerator_count,
        )
        log.info("Job submitted: %s", result)


if __name__ == "__main__":
    main()
