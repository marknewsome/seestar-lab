"""
Seestar Lab — YOLO second-stage transit validation.

Runs YOLOv8n inference on a hero-frame JPEG to confirm that a visually
recognisable object is actually present in the frame.  Only the COCO classes
relevant to transit detection are reported.

Soft dependency: if `ultralytics` is not installed, is_available() returns
False and validate() always returns (None, None).  The rest of the pipeline
continues unchanged and the UI hides the YOLO filter toggle.

Model weights (yolov8n.pt, ~6 MB) are downloaded automatically by
ultralytics on first use and cached in ~/.cache/ultralytics/.
"""

from __future__ import annotations

import os
from typing import Optional

# COCO class IDs we care about
_COCO_LABELS: dict[int, str] = {
    4:  "airplane",
    14: "bird",
}

_model     = None
_available = False

try:
    from ultralytics import YOLO as _YOLO  # type: ignore
    _available = True
except ImportError:
    pass


def is_available() -> bool:
    """Return True if ultralytics is installed."""
    return _available


def _get_model():
    global _model
    if _model is None:
        _model = _YOLO("yolov8n.pt")
    return _model


def validate(
    thumb_path: str,
    conf_threshold: float = 0.25,
) -> tuple[Optional[str], Optional[float]]:
    """
    Run YOLO on the full hero-frame JPEG at *thumb_path*.

    Returns ``(label, confidence)`` for the highest-confidence detection
    among the relevant COCO classes, or ``(None, None)`` if nothing is
    found, the file does not exist, or ultralytics is not installed.
    """
    if not _available or not os.path.isfile(thumb_path):
        return None, None

    model   = _get_model()
    results = model(thumb_path, verbose=False)[0]

    best_label: Optional[str]   = None
    best_conf:  float           = 0.0

    for box in results.boxes:
        cls_id = int(box.cls[0])
        conf   = float(box.conf[0])
        if cls_id in _COCO_LABELS and conf >= conf_threshold and conf > best_conf:
            best_conf  = conf
            best_label = _COCO_LABELS[cls_id]

    if best_label is None:
        return None, None
    return best_label, round(best_conf, 3)
