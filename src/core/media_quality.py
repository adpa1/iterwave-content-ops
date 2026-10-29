"""图片素材质量初筛（确定性图像指标，不冒充语义理解或 AI 审美判断）。"""
from __future__ import annotations

import io
import math

import numpy as np
from PIL import Image, ImageStat


def score_image(raw: bytes) -> dict:
    """返回 0–100 的启发式质量分与可解释指标。评分用于初筛，不代替人工审核。"""
    with Image.open(io.BytesIO(raw)) as source:
        image = source.convert("RGB")
        width, height = image.size
        sample = image.copy()
        sample.thumbnail((512, 512))
    pixels = width * height
    resolution_score = min(25.0, pixels / 1_500_000 * 25.0)

    ratio = width / max(1, height)
    if 0.55 <= ratio <= 0.85:
        composition_score = 20.0
        ratio_note = "纵向构图适合图文卡片"
    elif 0.85 < ratio <= 1.1:
        composition_score = 17.0
        ratio_note = "接近方形构图，可用于多数版式"
    elif 1.1 < ratio <= 1.7:
        composition_score = 12.0
        ratio_note = "横向构图，竖版平台可能需要裁切"
    else:
        composition_score = 5.0
        ratio_note = "长宽比极端，建议人工检查裁切"

    gray = sample.convert("L")
    stat = ImageStat.Stat(gray)
    brightness = float(stat.mean[0])
    contrast = float(stat.stddev[0])
    contrast_score = min(20.0, contrast / 45.0 * 20.0)
    if 45 <= brightness <= 210:
        brightness_score = 15.0
        brightness_note = "整体亮度在常用范围"
    else:
        brightness_score = max(0.0, 15.0 - min(abs(brightness - 127.5) / 8.5, 15.0))
        brightness_note = "整体可能过暗或过亮"

    sharpness = None
    try:
        import cv2
        gray_array = np.asarray(gray)
        sharpness = float(cv2.Laplacian(gray_array, cv2.CV_64F).var())
        sharpness_score = min(20.0, math.log1p(max(0.0, sharpness)) / math.log1p(500.0) * 20.0)
    except Exception:  # noqa: BLE001 — OpenCV 不可用时用灰度对比度作保守近似
        sharpness_score = min(20.0, contrast / 60.0 * 20.0)

    quantized = sample.quantize(colors=16, method=Image.Quantize.MEDIANCUT)
    color_count = len(quantized.getcolors(maxcolors=65536) or [])
    diversity_score = min(20.0, color_count / 16.0 * 20.0)
    score = round(max(0.0, min(
        100.0,
        resolution_score + composition_score + contrast_score
        + brightness_score + sharpness_score + diversity_score,
    )), 1)
    reasons = [ratio_note, brightness_note]
    if pixels < 500_000:
        reasons.append("分辨率偏低，放大后可能模糊")
    if sharpness is not None and sharpness < 35:
        reasons.append("边缘细节较少，可能存在模糊或画面过于平滑")
    if contrast < 18:
        reasons.append("画面对比度较低")
    if color_count <= 3:
        reasons.append("颜色种类较少，可能是纯色或极简图")
    return {
        "score": score,
        "metrics": {
            "width": width,
            "height": height,
            "pixels": pixels,
            "aspect_ratio": round(ratio, 3),
            "brightness": round(brightness, 1),
            "contrast": round(contrast, 1),
            "sharpness": round(sharpness, 1) if sharpness is not None else None,
            "color_count": color_count,
        },
        "components": {
            "resolution": round(resolution_score, 1),
            "composition": round(composition_score, 1),
            "contrast": round(contrast_score, 1),
            "brightness": round(brightness_score, 1),
            "sharpness": round(sharpness_score, 1),
            "color_diversity": round(diversity_score, 1),
        },
        "reasons": reasons,
        "method": "deterministic_image_heuristics_v1",
        "disclaimer": "自动视觉初筛，不代表语义相关性或 AI 审美判断；仍需人工确认",
    }
