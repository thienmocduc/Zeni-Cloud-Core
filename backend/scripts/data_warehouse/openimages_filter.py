#!/usr/bin/env python3
"""
Google Open Images V7 — interior/architecture filter.

Open Images V7: 9M images, ~600 label classes, CC-BY 2.0 license (FREE LEGAL).
Filter relevant labels: Furniture, Room, Building, Architecture, Window, Sofa, Bed, ...

Usage:
    # Step 1: download metadata CSV (small, ~500MB)
    wget https://storage.googleapis.com/openimages/v7/oidv7-class-descriptions.csv
    wget https://storage.googleapis.com/openimages/v7/oidv7-train-annotations-human-imagelabels.csv

    # Step 2: filter
    python openimages_filter.py \\
        --labels-csv oidv7-class-descriptions.csv \\
        --annotations-csv oidv7-train-annotations-human-imagelabels.csv \\
        --output-dir /mnt/zeni-data/openimages-interior

    # Step 3: img2dataset download
    img2dataset --url_list=/mnt/zeni-data/openimages-interior/urls.txt \\
                --output_folder=/mnt/zeni-data/openimages-interior/images \\
                --image_size=1024 --output_format=webdataset

Pipeline:
    1. Load class descriptions → keep interior/architecture LABEL IDs
    2. Stream annotations CSV → keep rows with matching LABEL IDs
    3. Generate image URL list (gs://open-images-dataset/train/{ImageID}.jpg)
    4. Write urls.txt for img2dataset batch download

Expected output: ~500K-1M images sau filter (vs 9M total).
License: CC-BY 2.0 — kèm attribution khi publish derivative.
"""
from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("zeni.openimages")


# ─── Interior/architecture label names (English, exact match Open Images) ───
INTERIOR_LABELS = {
    # Rooms
    "Room", "Living room", "Bedroom", "Bathroom", "Kitchen", "Dining room",
    "Office", "Lobby", "Library", "Wine cellar",
    # Furniture
    "Furniture", "Sofa", "Couch", "Bed", "Bed frame", "Chair", "Stool",
    "Table", "Coffee table", "Desk", "Dining table", "Wardrobe", "Cabinet",
    "Shelf", "Bookcase", "Drawer", "Cupboard", "Dresser",
    # Architecture
    "Building", "House", "Skyscraper", "Tower", "Castle", "Mansion",
    "Architecture", "Facade", "Window", "Door", "Stairs", "Pillar",
    "Arch", "Balcony", "Roof", "Ceiling", "Floor", "Wall",
    # Decor
    "Curtain", "Carpet", "Lamp", "Light fixture", "Chandelier",
    "Picture frame", "Painting", "Sculpture", "Vase", "Plant",
    # Kitchen detail
    "Kitchen appliance", "Refrigerator", "Oven", "Microwave", "Sink",
    "Stove", "Countertop", "Cabinetry",
    # Bathroom detail
    "Bathtub", "Shower", "Toilet", "Bidet", "Mirror",
}


def load_class_descriptions(csv_path: Path) -> dict[str, str]:
    """Read class descriptions CSV → map LabelID → LabelName."""
    label_map: dict[str, str] = {}
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 2:
                label_map[row[0]] = row[1]
    log.info("Loaded %d class descriptions", len(label_map))
    return label_map


def filter_interior_label_ids(label_map: dict[str, str]) -> set[str]:
    """Return LabelIDs whose name matches INTERIOR_LABELS (case-insensitive)."""
    target = {name.lower() for name in INTERIOR_LABELS}
    matched = {
        label_id
        for label_id, name in label_map.items()
        if name.lower() in target
    }
    log.info("Matched %d interior/architecture labels out of %d", len(matched), len(label_map))
    return matched


def filter_annotations(
    annotations_csv: Path,
    interior_label_ids: set[str],
    output_urls: Path,
    *,
    target: int = 1_000_000,
    confidence_min: float = 0.7,
) -> int:
    """Stream annotations CSV → emit ImageID URLs matching interior labels.

    Open Images annotations format:
        ImageID,Source,LabelName,Confidence
    """
    seen_images: set[str] = set()
    matched_count = 0

    with open(annotations_csv, "r", encoding="utf-8") as f_in, \
         open(output_urls, "w", encoding="utf-8") as f_out:
        reader = csv.reader(f_in)
        header = next(reader, None)
        log.info("Annotations header: %s", header)

        for row in reader:
            if len(row) < 4:
                continue
            image_id, _source, label_name, confidence_str = row[:4]

            if label_name not in interior_label_ids:
                continue

            try:
                confidence = float(confidence_str)
            except ValueError:
                continue

            if confidence < confidence_min:
                continue

            if image_id in seen_images:
                continue
            seen_images.add(image_id)

            # Open Images URL format
            url = f"https://storage.googleapis.com/openimages/v7/train/{image_id}.jpg"
            f_out.write(url + "\n")
            matched_count += 1

            if matched_count % 10_000 == 0:
                log.info("Matched %d images so far...", matched_count)

            if matched_count >= target:
                break

    log.info("Final matched: %d unique images written to %s", matched_count, output_urls)
    return matched_count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels-csv", required=True,
                        help="Path to oidv7-class-descriptions.csv")
    parser.add_argument("--annotations-csv", required=True,
                        help="Path to oidv7-train-annotations-human-imagelabels.csv")
    parser.add_argument("--output-dir", default="./openimages-interior",
                        help="Where to write urls.txt")
    parser.add_argument("--target", type=int, default=1_000_000,
                        help="Max images to extract (default 1M)")
    parser.add_argument("--confidence-min", type=float, default=0.7,
                        help="Minimum label confidence (default 0.7)")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    label_map = load_class_descriptions(Path(args.labels_csv))
    interior_label_ids = filter_interior_label_ids(label_map)

    if not interior_label_ids:
        log.error("No interior labels matched. Check --labels-csv format.")
        return

    output_urls = out_dir / "urls.txt"
    count = filter_annotations(
        Path(args.annotations_csv),
        interior_label_ids,
        output_urls,
        target=args.target,
        confidence_min=args.confidence_min,
    )

    log.info("Done. %d URLs in %s", count, output_urls)
    log.info("Next step: img2dataset --url_list=%s --output_folder=%s --image_size=1024",
             output_urls, out_dir / "images")


if __name__ == "__main__":
    main()
