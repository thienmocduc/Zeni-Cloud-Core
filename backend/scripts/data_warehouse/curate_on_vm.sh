#!/bin/bash
# Wellnexus VM curation pipeline — run sau khi có 1M+ ảnh raw trên GCS.
#
# Workflow:
#   1. List shards trên gs://wellnexus-data-warehouse/v1/raw/
#   2. Stream từng shard → extract ảnh
#   3. CLIP scoring (positive: interior/architecture, negative: low quality)
#   4. pHash dedup
#   5. Resolution + size filter
#   6. Upload curated → gs://wellnexus-data-warehouse/v1/curated/
#
# Usage trên VM Wellnexus (SSH):
#   bash /tmp/curate_on_vm.sh
#
# Hardware: e2-standard-8 CPU OK cho 100K ảnh (~6h)
# GPU recommended: 1× L4 spot ($0.30/h, ~2h cho 1M ảnh)

set -uo pipefail
exec > >(tee -a /tmp/curate.log) 2>&1

BUCKET="${BUCKET:-wellnexus-data-warehouse}"
RAW_PREFIX="${RAW_PREFIX:-v1/raw}"
CURATED_PREFIX="${CURATED_PREFIX:-v1/curated}"
MIN_CLIP_SCORE="${MIN_CLIP_SCORE:-0.25}"
MIN_RESOLUTION="${MIN_RESOLUTION:-512}"
BATCH_SIZE="${BATCH_SIZE:-128}"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

log "=== Curation pipeline start ==="
log "Source:   gs://$BUCKET/$RAW_PREFIX/"
log "Target:   gs://$BUCKET/$CURATED_PREFIX/"
log "Min CLIP: $MIN_CLIP_SCORE"
log "Min res:  $MIN_RESOLUTION px"

# Step 0: List raw shards
log "Listing raw shards..."
RAW_SHARDS=$(gsutil ls gs://$BUCKET/$RAW_PREFIX/*.tar 2>/dev/null | wc -l)
log "Found $RAW_SHARDS .tar shards on GCS"

if [ "$RAW_SHARDS" -eq 0 ]; then
    log "ERROR: No shards found in gs://$BUCKET/$RAW_PREFIX/"
    exit 1
fi

# Step 1: Install CLIP deps if missing
log "Checking CLIP deps..."
export PATH=$HOME/.local/bin:$PATH

python3 -c "import open_clip" 2>/dev/null || {
    log "Installing open_clip + torch..."
    pip3 install --break-system-packages \
        open_clip_torch torch torchvision imagededup \
        2>&1 | tail -5
}

# Step 2: Write curation Python script
cat > /tmp/curate.py << 'PYEOF'
"""
Curate raw shards → high-quality interior/architecture dataset.
"""
import os, sys, json, hashlib, tarfile, io, argparse
from pathlib import Path

import torch
import open_clip
from PIL import Image
import gcsfs

# Setup
ap = argparse.ArgumentParser()
ap.add_argument("--bucket", default="wellnexus-data-warehouse")
ap.add_argument("--raw-prefix", default="v1/raw")
ap.add_argument("--curated-prefix", default="v1/curated")
ap.add_argument("--min-clip", type=float, default=0.25)
ap.add_argument("--min-resolution", type=int, default=512)
ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
ap.add_argument("--batch-size", type=int, default=128)
args = ap.parse_args()

print(f"Device: {args.device}, batch_size: {args.batch_size}")

# Load CLIP
print("Loading OpenCLIP ViT-L/14...")
model, _, preprocess = open_clip.create_model_and_transforms(
    "ViT-L-14", pretrained="openai"
)
tokenizer = open_clip.get_tokenizer("ViT-L-14")
model = model.to(args.device).eval()

# Concept queries
POSITIVE = [
    "a photo of a modern interior design",
    "a photo of architectural exterior",
    "a beautiful living room with furniture",
    "a kitchen interior with modern appliances",
    "a bedroom with elegant decor",
    "Vietnamese traditional indochine interior",
    "Japanese minimalist interior",
    "Scandinavian modern home interior",
    "tropical villa architecture",
    "luxury hotel lobby interior",
]
NEGATIVE = [
    "a screenshot of text or a meme",
    "a low quality blurry photo",
    "a watermarked stock photo",
    "a cartoon or illustration",
    "a person selfie",
    "explicit adult content",
]

# Encode text features
with torch.no_grad():
    pos_tok = tokenizer(POSITIVE).to(args.device)
    neg_tok = tokenizer(NEGATIVE).to(args.device)
    pos_feats = model.encode_text(pos_tok)
    neg_feats = model.encode_text(neg_tok)
    pos_feats = pos_feats / pos_feats.norm(dim=-1, keepdim=True)
    neg_feats = neg_feats / neg_feats.norm(dim=-1, keepdim=True)

# Connect GCS
fs = gcsfs.GCSFileSystem()

# List raw shards
raw_pattern = f"{args.bucket}/{args.raw_prefix}/"
shards = sorted([f for f in fs.ls(raw_pattern) if f.endswith(".tar")])
print(f"Found {len(shards)} shards")

# Process each shard
stats = {"total": 0, "kept": 0, "rejected_resolution": 0, "rejected_clip": 0, "dedup": 0}
seen_hashes = set()

for shard_idx, shard_path in enumerate(shards):
    print(f"\n[{shard_idx+1}/{len(shards)}] Processing {shard_path}...")

    # Download shard to /tmp
    local_path = f"/tmp/shard_{shard_idx:05d}.tar"
    fs.get(shard_path, local_path)

    # Open tar + extract images
    batch_images = []
    batch_keys = []

    with tarfile.open(local_path) as tar:
        for member in tar:
            if not member.isfile() or not member.name.endswith(('.jpg', '.png', '.jpeg')):
                continue
            stats["total"] += 1

            try:
                f = tar.extractfile(member)
                img_bytes = f.read()

                # Quick resolution check
                img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                if min(img.size) < args.min_resolution:
                    stats["rejected_resolution"] += 1
                    continue

                # Hash dedup
                h = hashlib.md5(img_bytes).hexdigest()
                if h in seen_hashes:
                    stats["dedup"] += 1
                    continue
                seen_hashes.add(h)

                # Queue for batch CLIP
                batch_images.append(preprocess(img))
                batch_keys.append((member.name, img_bytes, h))

                # Process batch
                if len(batch_images) >= args.batch_size:
                    process_batch(batch_images, batch_keys, model, pos_feats, neg_feats,
                                  args, fs, stats, shard_idx)
                    batch_images = []
                    batch_keys = []
            except Exception as e:
                continue

    # Process last batch
    if batch_images:
        process_batch(batch_images, batch_keys, model, pos_feats, neg_feats,
                      args, fs, stats, shard_idx)

    # Cleanup local shard
    os.remove(local_path)

    # Print stats every shard
    print(f"Stats: total={stats['total']} kept={stats['kept']} "
          f"rej_res={stats['rejected_resolution']} rej_clip={stats['rejected_clip']} "
          f"dedup={stats['dedup']}")


def process_batch(imgs, keys, model, pos_feats, neg_feats, args, fs, stats, shard_idx):
    """Score batch via CLIP + upload kept."""
    batch_tensor = torch.stack(imgs).to(args.device)

    with torch.no_grad():
        img_feats = model.encode_image(batch_tensor)
        img_feats = img_feats / img_feats.norm(dim=-1, keepdim=True)
        pos_sim = (img_feats @ pos_feats.T).max(dim=-1).values
        neg_sim = (img_feats @ neg_feats.T).max(dim=-1).values

    for i, (name, img_bytes, h) in enumerate(keys):
        pos_score = pos_sim[i].item()
        neg_score = neg_sim[i].item()

        if pos_score > neg_score + 0.05 and pos_score >= args.min_clip:
            # KEEP: upload to curated
            out_path = f"{args.bucket}/{args.curated_prefix}/{h[:2]}/{h}.jpg"
            with fs.open(out_path, "wb") as f:
                f.write(img_bytes)
            stats["kept"] += 1
        else:
            stats["rejected_clip"] += 1


# Write final stats
print("\n=== Curation complete ===")
print(json.dumps(stats, indent=2))
PYEOF

# Step 3: Run curation
log "Running curation script..."
python3 /tmp/curate.py \
    --bucket "$BUCKET" \
    --raw-prefix "$RAW_PREFIX" \
    --curated-prefix "$CURATED_PREFIX" \
    --min-clip "$MIN_CLIP_SCORE" \
    --min-resolution "$MIN_RESOLUTION" \
    --batch-size "$BATCH_SIZE"

log "=== Curation done ==="
log "Output: gs://$BUCKET/$CURATED_PREFIX/"
gsutil du -sh gs://$BUCKET/$CURATED_PREFIX/ 2>&1 | tail -3
gsutil ls gs://$BUCKET/$CURATED_PREFIX/ 2>&1 | wc -l
