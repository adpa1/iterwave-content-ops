"""图片素材质量初筛（确定性图像指标，不冒充语义理解或 AI 审美判断）。"""
from __future__ import annotations

import io
import math

import numpy as np
from PIL import Image, ImageStat


def score_image(raw: bytes, cfg=None) -> dict:
    """返回 0–100 的启发式质量分与可解释指标。评分用于初筛，不代替人工审核。"""
    if cfg is None:
        from src.core.config import get_config
        cfg = get_config()
    policy = cfg.media_quality
    with Image.open(io.BytesIO(raw)) as source:
        image = source.convert("RGB")
        width, height = image.size
        sample = image.copy()
        sample.thumbnail((512, 512))
    pixels = width * height
    resolution_score = min(policy.resolution_weight, pixels / policy.resolution_reference_pixels * policy.resolution_weight)

    ratio = width / max(1, height)
    if policy.portrait_ratio_min <= ratio <= policy.portrait_ratio_max:
        composition_score = policy.composition_weight
        ratio_note = "纵向构图适合图文卡片"
    elif policy.portrait_ratio_max < ratio <= policy.square_ratio_max:
        composition_score = policy.composition_weight * 0.85
        ratio_note = "接近方形构图，可用于多数版式"
    elif policy.square_ratio_max < ratio <= policy.landscape_ratio_max:
        composition_score = policy.composition_weight * 0.6
        ratio_note = "横向构图，竖版平台可能需要裁切"
    else:
        composition_score = policy.composition_weight * 0.25
        ratio_note = "长宽比极端，建议人工检查裁切"

    gray = sample.convert("L")
    stat = ImageStat.Stat(gray)
    brightness = float(stat.mean[0])
    contrast = float(stat.stddev[0])
    contrast_score = min(policy.contrast_weight, contrast / policy.contrast_reference * policy.contrast_weight)
    if policy.brightness_min <= brightness <= policy.brightness_max:
        brightness_score = policy.brightness_weight
        brightness_note = "整体亮度在常用范围"
    else:
        brightness_score = max(0.0, policy.brightness_weight - min(abs(brightness - (policy.brightness_min + policy.brightness_max) / 2) / policy.brightness_penalty_scale, policy.brightness_weight))
        brightness_note = "整体可能过暗或过亮"

    sharpness = None
    try:
        import cv2
        gray_array = np.asarray(gray)
        sharpness = float(cv2.Laplacian(gray_array, cv2.CV_64F).var())
        sharpness_score = min(policy.sharpness_weight, math.log1p(max(0.0, sharpness)) / math.log1p(policy.sharpness_reference) * policy.sharpness_weight)
    except Exception:  # noqa: BLE001 — OpenCV 不可用时用灰度对比度作保守近似
        sharpness_score = min(policy.sharpness_weight, contrast / 60.0 * policy.sharpness_weight)

    quantized = sample.quantize(colors=16, method=Image.Quantize.MEDIANCUT)
    color_count = len(quantized.getcolors(maxcolors=65536) or [])
    diversity_score = min(policy.color_diversity_weight, color_count / policy.color_count_reference * policy.color_diversity_weight)
    score = round(max(0.0, min(
        100.0,
        resolution_score + composition_score + contrast_score
        + brightness_score + sharpness_score + diversity_score,
    )), 1)
    reasons = [ratio_note, brightness_note]
    if pixels < policy.low_resolution_pixels:
        reasons.append("分辨率偏低，放大后可能模糊")
    if sharpness is not None and sharpness < policy.sharpness_warning_threshold:
        reasons.append("边缘细节较少，可能存在模糊或画面过于平滑")
    if contrast < policy.low_contrast_threshold:
        reasons.append("画面对比度较低")
    if color_count <= policy.low_color_count:
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
