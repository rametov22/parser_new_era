import colorsys
import hashlib
import math
from io import BytesIO

from django.utils import timezone
from PIL import Image

SAMPLE = 48
HUE_BINS = 30
MIN_SAT = 0.15
MIN_LIGHT = 0.06
MAX_LIGHT = 0.95
MIN_COLORED_FRAC = 0.02

OUT_SAT_MIN = 0.5
OUT_SAT_MAX = 0.9
OUT_LIGHT_MIN = 0.42
OUT_LIGHT_MAX = 0.62

FALLBACK_DOMINANT = "#1A1A1A"
FALLBACK_SECONDARY = ["#0E0E0E", "#2A2A2A"]


def _to_hex(value: float) -> str:
    clamped = max(0, min(255, round(value)))
    return f"{clamped:02x}"


def _fallback_palette():
    return {
        "dominant": FALLBACK_DOMINANT,
        "secondary": list(FALLBACK_SECONDARY),
    }


def _resolve_bin_color(bin_idx, weight, sin_sum, cos_sum, sat_sum, light_sum):
    hue = (math.atan2(sin_sum[bin_idx], cos_sum[bin_idx]) / (2 * math.pi) + 1) % 1
    sat = sat_sum[bin_idx] / weight[bin_idx]
    light = light_sum[bin_idx] / weight[bin_idx]

    sat_clamped = max(OUT_SAT_MIN, min(OUT_SAT_MAX, sat))
    light_clamped = max(OUT_LIGHT_MIN, min(OUT_LIGHT_MAX, light))

    r, g, b = colorsys.hls_to_rgb(hue, light_clamped, sat_clamped)
    return f"#{_to_hex(r * 255)}{_to_hex(g * 255)}{_to_hex(b * 255)}"


def _hue_distance(h1, h2):
    diff = abs(h1 - h2) % 1.0
    return min(diff, 1.0 - diff) * 360.0


def extract_colors(image_bytes: bytes):
    try:
        img = Image.open(BytesIO(image_bytes))
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        pixels = list(img.resize((SAMPLE, SAMPLE), Image.Resampling.BILINEAR).getdata())
    except Exception:
        return _fallback_palette()

    weight = [0.0] * HUE_BINS
    sin_sum = [0.0] * HUE_BINS
    cos_sum = [0.0] * HUE_BINS
    sat_sum = [0.0] * HUE_BINS
    light_sum = [0.0] * HUE_BINS
    total_pixels = 0
    colored_pixels = 0

    for r, g, b, a in pixels:
        if a < 128:
            continue
        total_pixels += 1
        h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        if s < MIN_SAT or l < MIN_LIGHT or l > MAX_LIGHT:
            continue
        colored_pixels += 1

        bin_idx = min(HUE_BINS - 1, math.floor(h * HUE_BINS))
        angle = h * 2 * math.pi
        weight[bin_idx] += s
        sin_sum[bin_idx] += math.sin(angle) * s
        cos_sum[bin_idx] += math.cos(angle) * s
        sat_sum[bin_idx] += s * s
        light_sum[bin_idx] += l * s

    if total_pixels == 0 or colored_pixels < (total_pixels * MIN_COLORED_FRAC):
        return _fallback_palette()

    dominant_bin = max(range(HUE_BINS), key=lambda b: weight[b])
    if weight[dominant_bin] <= 0:
        return _fallback_palette()

    dominant_color = _resolve_bin_color(
        dominant_bin, weight, sin_sum, cos_sum, sat_sum, light_sum
    )

    bin_hues = {
        b: (math.atan2(sin_sum[b], cos_sum[b]) / (2 * math.pi) + 1) % 1
        for b in range(HUE_BINS)
        if weight[b] > 0
    }
    candidates = [
        (weight[b], b)
        for b in range(HUE_BINS)
        if b != dominant_bin and weight[b] > 0
    ]
    candidates.sort(reverse=True, key=lambda item: item[0])

    selected_bins = [dominant_bin]
    secondary_colors = []
    for _candidate_weight, bin_idx in candidates:
        if len(secondary_colors) >= 3:
            break
        candidate_hue = bin_hues[bin_idx]
        if all(
            _hue_distance(candidate_hue, bin_hues[selected]) >= 20.0
            for selected in selected_bins
        ):
            selected_bins.append(bin_idx)
            secondary_colors.append(
                _resolve_bin_color(
                    bin_idx, weight, sin_sum, cos_sum, sat_sum, light_sum
                )
            )

    if len(secondary_colors) < 2:
        for _candidate_weight, bin_idx in candidates:
            if len(secondary_colors) >= 2:
                break
            if bin_idx not in selected_bins:
                selected_bins.append(bin_idx)
                secondary_colors.append(
                    _resolve_bin_color(
                        bin_idx, weight, sin_sum, cos_sum, sat_sum, light_sum
                    )
                )

    return {
        "dominant": dominant_color,
        "secondary": secondary_colors or list(FALLBACK_SECONDARY),
    }


def apply_poster_colors(content_obj, image_bytes: bytes) -> bool:
    if getattr(content_obj, "color_locked", False):
        return False

    new_hash = hashlib.md5(image_bytes).hexdigest()
    if (
        getattr(content_obj, "poster_color_hash", None) == new_hash
        and getattr(content_obj, "poster_processed_at", None) is not None
    ):
        return False

    result = extract_colors(image_bytes)
    fields = {
        "dominant_color": result["dominant"],
        "secondary_colors": result["secondary"],
        "poster_color_hash": new_hash,
        "poster_processed_at": timezone.now(),
        "poster_color_attempts": 0,
    }
    type(content_obj).objects.filter(pk=content_obj.pk, color_locked=False).update(
        **fields
    )
    for key, value in fields.items():
        setattr(content_obj, key, value)
    return True
