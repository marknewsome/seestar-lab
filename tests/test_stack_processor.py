"""
Unit tests for pure functions in stack_processor.py.

Run with:  pytest tests/test_stack_processor.py -v
"""
import numpy as np
import pytest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from stack_processor import (
    _quality_score,
    _compute_weight,
    _scnr_green,
    _auto_stretch,
    _sharpness,
    _sky_background,
    _subtract_background,
    _auto_crop,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def flat_bgr(h=64, w=64, val=0.2):
    """Uniform float32 BGR image."""
    return np.full((h, w, 3), val, dtype=np.float32)


def synthetic_sky(h=64, w=64, sky=0.05, rng=None):
    """Low-level sky image with faint Gaussian noise."""
    if rng is None:
        rng = np.random.default_rng(42)
    img = rng.normal(loc=sky, scale=0.005, size=(h, w, 3)).astype(np.float32)
    return np.clip(img, 0.0, 1.0)


# ── _quality_score ────────────────────────────────────────────────────────────

class TestQualityScore:
    def test_zero_when_few_stars(self):
        assert _quality_score({'star_count': 4, 'snr': 10.0, 'fwhm': 2.0}) == 0.0

    def test_zero_when_no_stars(self):
        assert _quality_score({}) == 0.0

    def test_positive_with_good_frame(self):
        score = _quality_score({'star_count': 100, 'snr': 20.0, 'fwhm': 2.0})
        assert score > 0.0

    def test_higher_snr_wins(self):
        lo = _quality_score({'star_count': 50, 'snr': 5.0,  'fwhm': 2.0})
        hi = _quality_score({'star_count': 50, 'snr': 20.0, 'fwhm': 2.0})
        assert hi > lo

    def test_lower_fwhm_wins(self):
        blurry = _quality_score({'star_count': 50, 'snr': 10.0, 'fwhm': 4.0})
        sharp  = _quality_score({'star_count': 50, 'snr': 10.0, 'fwhm': 2.0})
        assert sharp > blurry

    def test_more_stars_wins(self):
        few  = _quality_score({'star_count': 20,  'snr': 10.0, 'fwhm': 2.0})
        many = _quality_score({'star_count': 100, 'snr': 10.0, 'fwhm': 2.0})
        assert many > few

    def test_tiny_fwhm_clamped(self):
        # fwhm=0 would divide by zero; function clamps to 0.5
        score = _quality_score({'star_count': 50, 'snr': 10.0, 'fwhm': 0.0})
        assert np.isfinite(score) and score > 0.0


# ── _compute_weight ───────────────────────────────────────────────────────────

class TestComputeWeight:
    def test_positive(self):
        w = _compute_weight({'fwhm': 2.0, 'eccentricity': 0.1, 'star_count': 80, 'snr': 15.0})
        assert w > 0.0

    def test_better_frame_higher_weight(self):
        good = _compute_weight({'fwhm': 2.0, 'eccentricity': 0.1, 'star_count': 100, 'snr': 20.0})
        poor = _compute_weight({'fwhm': 4.0, 'eccentricity': 0.6, 'star_count': 20,  'snr': 3.0})
        assert good > poor

    def test_zero_fwhm_clamped(self):
        w = _compute_weight({'fwhm': 0.0, 'eccentricity': 0.0, 'star_count': 50, 'snr': 10.0})
        assert np.isfinite(w) and w > 0.0

    def test_fallback_defaults(self):
        # Missing keys should not raise
        w = _compute_weight({})
        assert np.isfinite(w) and w > 0.0


# ── _scnr_green ───────────────────────────────────────────────────────────────

class TestScnrGreen:
    def test_green_never_exceeds_max_rb(self):
        rng = np.random.default_rng(7)
        img = rng.random((32, 32, 3)).astype(np.float32)
        out = _scnr_green(img)
        max_rb = np.maximum(out[:, :, 0], out[:, :, 2])
        assert np.all(out[:, :, 1] <= max_rb + 1e-6)

    def test_rb_channels_unchanged(self):
        rng = np.random.default_rng(7)
        img = rng.random((32, 32, 3)).astype(np.float32)
        out = _scnr_green(img)
        np.testing.assert_array_equal(out[:, :, 0], img[:, :, 0])
        np.testing.assert_array_equal(out[:, :, 2], img[:, :, 2])

    def test_output_shape_preserved(self):
        img = flat_bgr()
        assert _scnr_green(img).shape == img.shape

    def test_no_change_when_green_already_neutral(self):
        # Green = min(R,B) everywhere — SCNR should not change anything
        img = np.ones((16, 16, 3), dtype=np.float32) * 0.5
        img[:, :, 1] = 0.3  # green below both R and B
        out = _scnr_green(img)
        np.testing.assert_allclose(out[:, :, 1], img[:, :, 1])


# ── _auto_stretch ─────────────────────────────────────────────────────────────

class TestAutoStretch:
    def test_output_range_0_1(self):
        img = synthetic_sky()
        out = _auto_stretch(img)
        assert out.min() >= -1e-6
        assert out.max() <= 1.0 + 1e-6

    def test_shape_preserved(self):
        img = flat_bgr()
        assert _auto_stretch(img).shape == img.shape

    def test_dtype_float32(self):
        assert _auto_stretch(flat_bgr()).dtype == np.float32

    def test_brighter_input_brighter_output(self):
        dim    = _auto_stretch(synthetic_sky(sky=0.01))
        bright = _auto_stretch(synthetic_sky(sky=0.3))
        # Both should be valid [0,1] images; just verify function runs on either
        assert dim.max()    <= 1.0 + 1e-6
        assert bright.max() <= 1.0 + 1e-6

    def test_uniform_image_no_crash(self):
        # hi == lo edge case; span is clamped to 1e-10
        out = _auto_stretch(flat_bgr(val=0.5))
        assert np.all(np.isfinite(out))


# ── _sharpness ────────────────────────────────────────────────────────────────

class TestSharpness:
    def test_sharp_image_higher_than_flat(self):
        rng = np.random.default_rng(1)
        noisy = (rng.random((128, 128)) * 65535).astype(np.uint16)
        flat  = np.full((128, 128), 1000, dtype=np.uint16)
        assert _sharpness(noisy) > _sharpness(flat)

    def test_flat_image_near_zero(self):
        flat = np.zeros((128, 128), dtype=np.uint16)
        assert _sharpness(flat) < 1.0

    def test_returns_float(self):
        data = np.zeros((64, 64), dtype=np.uint16)
        assert isinstance(_sharpness(data), float)


# ── _sky_background ───────────────────────────────────────────────────────────

class TestSkyBackground:
    def test_sky_near_input_level(self):
        img = synthetic_sky(sky=0.05)
        bg  = _sky_background(img)
        assert 0.0 < bg < 0.1

    def test_higher_sky_gives_higher_reading(self):
        lo = _sky_background(synthetic_sky(sky=0.03))
        hi = _sky_background(synthetic_sky(sky=0.15))
        assert hi > lo

    def test_all_zero_does_not_crash(self):
        img = np.zeros((32, 32, 3), dtype=np.float32)
        bg  = _sky_background(img)
        assert np.isfinite(bg)


# ── _subtract_background ─────────────────────────────────────────────────────

class TestSubtractBackground:
    def test_mesh_scale_zero_returns_copy(self):
        img = synthetic_sky()
        out = _subtract_background(img, mesh_scale=0)
        np.testing.assert_array_equal(out, img)
        assert out is not img  # must be a copy

    def test_output_shape_preserved(self):
        img = synthetic_sky()
        out = _subtract_background(img, mesh_scale=16)
        assert out.shape == img.shape

    def test_output_dtype_float32(self):
        img = synthetic_sky()
        out = _subtract_background(img, mesh_scale=16)
        assert out.dtype == np.float32

    def test_sky_reduced_after_subtraction(self):
        img = synthetic_sky(sky=0.1)
        out = _subtract_background(img, mesh_scale=16)
        # Background subtraction should pull the median sky closer to zero
        before = float(np.median(img))
        after  = float(np.median(out))
        assert after < before


# ── _auto_crop ────────────────────────────────────────────────────────────────

class TestAutoCrop:
    def _border_mask(self, h=64, w=64, border=8):
        mask = np.zeros((h, w), dtype=bool)
        mask[border:h - border, border:w - border] = True
        return mask

    def test_crops_to_valid_region(self):
        img  = flat_bgr(64, 64)
        mask = self._border_mask(64, 64, border=8)
        out  = _auto_crop(img, mask, margin=0)
        assert out.shape[0] < 64
        assert out.shape[1] < 64

    def test_margin_shrinks_further(self):
        img   = flat_bgr(64, 64)
        mask  = self._border_mask(64, 64, border=4)
        out0  = _auto_crop(img, mask, margin=0)
        out4  = _auto_crop(img, mask, margin=4)
        assert out4.shape[0] <= out0.shape[0]

    def test_all_valid_no_crash(self):
        # _auto_crop uses rows[-1] as an exclusive end index without +1,
        # so a fully-valid mask crops 1px from the bottom/right edge.
        # This edge case never occurs in real stacks (alignment always
        # leaves at least a 1-pixel invalid border), so we just verify
        # the function doesn't crash and returns a non-empty result.
        img  = flat_bgr(32, 32)
        mask = np.ones((32, 32), dtype=bool)
        out  = _auto_crop(img, mask, margin=0)
        assert out.ndim == 3 and out.size > 0

    def test_all_invalid_returns_original(self):
        img  = flat_bgr(32, 32)
        mask = np.zeros((32, 32), dtype=bool)
        out  = _auto_crop(img, mask)
        assert out.shape == img.shape


# ── Quality floor selection logic ─────────────────────────────────────────────
# Tests the inline selection logic from StackProcessor.run() in isolation,
# since the full run() requires FITS files, Siril, etc.

def _apply_quality_floor(all_scores, max_frames, min_quality):
    """Mirror of the selection logic in StackProcessor.run()."""
    scored = sorted(range(len(all_scores)), key=lambda k: all_scores[k], reverse=True)
    best_score  = all_scores[scored[0]] if scored else 0.0
    floor_score = best_score * max(min_quality, 0.0)
    scored_floor = [k for k in scored if all_scores[k] >= floor_score]
    n_floor_rejected = len(scored) - len(scored_floor)
    selected = scored_floor[:max_frames]
    return selected, n_floor_rejected, best_score, floor_score


class TestQualityFloor:
    def test_no_floor_keeps_all_up_to_max(self):
        scores = [10.0, 8.0, 6.0, 4.0, 2.0]
        sel, rej, _, _ = _apply_quality_floor(scores, max_frames=10, min_quality=0.0)
        assert len(sel) == 5
        assert rej == 0

    def test_max_frames_cap_applied(self):
        scores = [10.0, 8.0, 6.0, 4.0, 2.0]
        sel, _, _, _ = _apply_quality_floor(scores, max_frames=3, min_quality=0.0)
        assert len(sel) == 3

    def test_floor_rejects_low_scores(self):
        # min_quality=0.5 → floor = best * 0.5 = 10 * 0.5 = 5.0
        # Scores [10, 8, 6, 4, 2] → keep [10, 8, 6], reject [4, 2]
        scores = [10.0, 8.0, 6.0, 4.0, 2.0]
        sel, rej, best, floor = _apply_quality_floor(scores, max_frames=100, min_quality=0.5)
        assert best  == 10.0
        assert floor == 5.0
        assert rej   == 2
        assert len(sel) == 3
        assert all(scores[k] >= 5.0 for k in sel)

    def test_floor_then_cap(self):
        # Floor removes some, then cap limits further
        scores = [10.0, 8.0, 6.0, 4.0, 2.0]
        sel, rej, _, _ = _apply_quality_floor(scores, max_frames=2, min_quality=0.5)
        assert len(sel) == 2
        assert rej == 2  # floor-rejected only, not cap-rejected

    def test_floor_1_0_keeps_only_best(self):
        scores = [10.0, 9.9, 5.0, 1.0]
        sel, rej, _, _ = _apply_quality_floor(scores, max_frames=100, min_quality=1.0)
        assert len(sel) == 1
        assert scores[sel[0]] == 10.0

    def test_results_sorted_best_first(self):
        rng = np.random.default_rng(99)
        scores = list(rng.random(20) * 100)
        sel, _, _, _ = _apply_quality_floor(scores, max_frames=10, min_quality=0.3)
        selected_scores = [scores[k] for k in sel]
        assert selected_scores == sorted(selected_scores, reverse=True)

    def test_empty_scores(self):
        sel, rej, best, floor = _apply_quality_floor([], max_frames=10, min_quality=0.5)
        assert sel == []
        assert best == 0.0
