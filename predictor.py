"""
Plug your ML model in here.

`predict(image_path)` returns a list of suggestion dicts in this shape:

    {
        "label": "singleshoe",
        "bounding_box": {"top": ..., "left": ..., "height": ..., "width": ...}
    }

Coordinates MUST be in the EXIF-oriented (human-perspective) coordinate system —
the same space the annotator UI displays. If you load images yourself for
inference, call `PIL.ImageOps.exif_transpose()` first so predictions line up
with what the user sees.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import torch
from PIL import Image, ImageOps
from transformers import AutoImageProcessor, DFineForObjectDetection


@lru_cache(maxsize=1)
def _load_model():
    """Load the fine-tuned D-FINE checkpoint once and cache it."""
    # Checkpoint from dfine-finetune project
    ckpt_dir = Path(__file__).parent.parent / "dfine-finetune" / "checkpoints" / "dfine-singleshoe"

    if not ckpt_dir.exists():
        raise FileNotFoundError(
            f"D-FINE checkpoint not found at {ckpt_dir}.\n"
            "Run dfine-finetune/train.py first to create it."
        )

    # Use CPU for inference (MPS has issues with D-FINE's denoising queries)
    device = torch.device("cpu")

    processor = AutoImageProcessor.from_pretrained(ckpt_dir)
    model = DFineForObjectDetection.from_pretrained(ckpt_dir)
    model.to(device).eval()

    return processor, model, device


def predict(image_path: str, threshold: float = 0.4) -> list[dict]:
    """Run the fine-tuned D-FINE model on an image and return bounding box suggestions.

    Args:
        image_path: Path to the image file
        threshold: Confidence threshold (default 0.5)

    Returns:
        List of detection dicts with label and bounding_box in EXIF-transposed pixel space
    """
    try:
        processor, model, device = _load_model()
    except FileNotFoundError as e:
        print(f"Warning: {e}")
        return []

    # Load image and transpose to display orientation
    try:
        with Image.open(image_path) as pil:
            image = ImageOps.exif_transpose(pil).convert("RGB")
    except Exception as e:
        print(f"Error loading image {image_path}: {e}")
        return []

    img_h, img_w = image.height, image.width

    # Preprocess and move to device
    try:
        inputs = processor(images=image, return_tensors="pt").to(device)
    except Exception as e:
        print(f"Error preprocessing image: {e}")
        return []

    # Run inference without gradients
    try:
        with torch.no_grad():
            outputs = model(**inputs)
    except Exception as e:
        print(f"Error during model inference: {e}")
        return []

    # Post-process: scale boxes back to original image size
    target_sizes = torch.tensor([[img_h, img_w]], device=device)
    results = processor.post_process_object_detection(
        outputs,
        threshold=threshold,
        target_sizes=target_sizes,
    )[0]

    # Convert to annotater format
    detections = []
    for score, label_id, box in zip(results["scores"], results["labels"], results["boxes"]):
        x1, y1, x2, y2 = box.tolist()
        label = model.config.id2label[int(label_id)]

        detections.append({
            "label": label,
            "bounding_box": {
                "top": float(y1),
                "left": float(x1),
                "height": float(y2 - y1),
                "width": float(x2 - x1),
            },
        })

    return detections
