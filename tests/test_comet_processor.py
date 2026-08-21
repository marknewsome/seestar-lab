"""
Unit tests for pure functions in comet_processor.py.

Run with:  pytest tests/test_comet_processor.py -v
"""
import numpy as np
import pytest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import comet_processor as cp


# ── Helpers ───────────────────────────────────────────────────────────────────

def _diffuse_frame(h=400, w=400, cx=200, cy=200, amp=400.0, sigma=20.0,
                    bg_amp=50.0, rng=None):
    """Synthetic luminance frame: noisy background + one diffuse (comet-like) blob."""
    if rng is None:
        rng = np.random.default_rng(0)
    lum = (rng.random((h, w)) * bg_amp).astype(np.float32)
    yy, xx = np.mgrid[0:h, 0:w]
    blob = amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2))
    return lum + blob


def _point_source_frame(h=400, w=400, cx=200, cy=200, amp=300.0, sigma=2.0,
                         bg_amp=50.0, rng=None):
    """Synthetic luminance frame: noisy background + one sharp star-like point."""
    if rng is None:
        rng = np.random.default_rng(0)
    lum = (rng.random((h, w)) * bg_amp).astype(np.float32)
    yy, xx = np.mgrid[0:h, 0:w]
    star = amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2))
    return lum + star


# ── _find_nucleus_in_frame ──────────────────────────────────────────────────────

def test_find_nucleus_returns_position_and_confidence():
    lum = _diffuse_frame()
    result = cp._find_nucleus_in_frame(lum, search_r=200)
    assert result is not None
    assert len(result) == 3
    x, y, confidence = result
    # The diffuseness-score centroid has a real systematic bias of a few tens
    # of px on synthetic single-blob test frames (background-gradient removal
    # interacts with the injected blob's own profile) — this tolerance reflects
    # that, not a tight accuracy requirement. What matters for the gate tests
    # below is that detection is directionally consistent frame-to-frame.
    assert abs(x - 200) < 40
    assert abs(y - 200) < 40
    assert confidence > 0


def test_find_nucleus_prefers_diffuse_over_point_source():
    """A diffuse blob should score higher than an equally-bright point source —
    this is the core mechanism that lets the detector distinguish the coma
    from a nearby bright star."""
    rng = np.random.default_rng(1)
    h, w = 400, 400
    lum = (rng.random((h, w)) * 50).astype(np.float32)
    yy, xx = np.mgrid[0:h, 0:w]
    # diffuse comet-like blob at (150,150)
    lum += 400 * np.exp(-((xx - 150) ** 2 + (yy - 150) ** 2) / (2 * 20 ** 2))
    # bright point star at (300,300), higher peak brightness than the blob
    lum += 800 * np.exp(-((xx - 300) ** 2 + (yy - 300) ** 2) / (2 * 2 ** 2))

    x, y, _ = cp._find_nucleus_in_frame(lum, search_r=200)
    # detector should land on the diffuse blob, not the brighter point source
    # (i.e. much closer to (150,150) than to the star at (300,300))
    dist_to_blob = np.hypot(x - 150, y - 150)
    dist_to_star = np.hypot(x - 300, y - 300)
    assert dist_to_blob < dist_to_star


# ── Rolling-hint confidence gate ────────────────────────────────────────────────
# Regression test for a real failure mode: an unconstrained rolling hint can
# lock onto a star instead of the comet for one frame, and without a gate that
# bad detection becomes the search seed for every subsequent frame, causing
# the nucleus-fixed animation to visibly jiggle to a wrong position and back.

def _run_rolling_hint(frames, ratio):
    """Minimal reimplementation of _find_nucleus's gating loop, for testing
    the gate logic in isolation without needing full FITS files / transforms."""
    h, w = frames[0].shape
    last_pos = None
    confident_scores = []
    trusted_flags = []
    for lum in frames:
        hx, hy = last_pos if last_pos is not None else (w // 2, h // 2)
        sr = int(min(h, w) * 0.4)
        x, y, conf = cp._find_nucleus_in_frame(lum, hint_x=hx, hint_y=hy, search_r=sr)
        baseline = np.median(confident_scores) if confident_scores else None
        trusted = baseline is None or conf >= baseline * ratio
        trusted_flags.append(trusted)
        if trusted:
            last_pos = (x, y)
            confident_scores.append(conf)
    return trusted_flags


def test_confidence_gate_rejects_injected_glitch_frame():
    """A frame with no real comet signal (pure noise + an off-target point
    source) should fail the confidence gate at the module's default ratio,
    so it cannot become the next frame's search seed."""
    rng = np.random.default_rng(7)
    h, w = 400, 400
    frames = []
    for i in range(6):
        if i == 3:
            frames.append(_point_source_frame(h, w, cx=350, cy=50, amp=300,
                                                sigma=2, rng=rng))
        else:
            cx, cy = 200 + i * 10, 200 - i * 5
            frames.append(_diffuse_frame(h, w, cx=cx, cy=cy, amp=400, rng=rng))

    trusted = _run_rolling_hint(frames, cp.NUCLEUS_CONFIDENCE_MIN_RATIO)
    assert not trusted[3], "glitch frame should fail the confidence gate"
    # all genuine comet frames should be trusted
    for i in (0, 1, 2, 4, 5):
        assert trusted[i], f"frame {i} (real comet signal) was wrongly rejected"


def test_confidence_gate_accepts_genuine_fading_comet():
    """A real comet that dims over a session (fading coma, brightening sky,
    etc.) should not be mistaken for a glitch and rejected."""
    rng = np.random.default_rng(3)
    h, w = 400, 400
    frames = []
    for i in range(8):
        cx, cy = 200 + i * 10, 200 - i * 5
        amp = 400.0 if i < 5 else 180.0  # ~55% dimmer for the back half
        frames.append(_diffuse_frame(h, w, cx=cx, cy=cy, amp=amp, rng=rng))

    trusted = _run_rolling_hint(frames, cp.NUCLEUS_CONFIDENCE_MIN_RATIO)
    assert all(trusted), "genuine (if fainter) comet detections should not be rejected"


# ── _stretch — white balance ────────────────────────────────────────────────────

def test_stretch_removes_channel_gain_color_cast():
    """Sky-level SUBTRACTION alone corrects the black point per channel but not
    a gain-type colour cast (e.g. green airglow contributing proportionally
    more signal in one channel). Channel-mean equalisation after subtraction
    should leave the background reading close to neutral gray."""
    rng = np.random.default_rng(0)
    h, w = 200, 200
    base = (rng.random((h, w, 1)) * 0.015).astype(np.float32)
    rgb = np.repeat(base, 3, axis=2).copy()
    rgb[..., 1] *= 1.6  # green channel has 60% more gain than the true sky signal
    rgb[100, 100] = [0.8, 0.8, 0.8]  # bright neutral source so the high-percentile anchor works

    out = cp._stretch(rgb)
    bg_means = out[:50, :50].reshape(-1, 3).mean(axis=0)
    spread = bg_means.max() - bg_means.min()
    assert spread < 0.01, f"background should read near-neutral after WB, spread={spread}"


def test_stretch_output_in_valid_range():
    rng = np.random.default_rng(0)
    rgb = rng.random((100, 100, 3)).astype(np.float32) * 0.05
    out = cp._stretch(rgb)
    assert out.shape == rgb.shape
    assert out.min() >= 0.0
    assert out.max() <= 1.0 + 1e-6


# ── _apply_noise ────────────────────────────────────────────────────────────────

def test_apply_noise_zero_is_noop():
    rng = np.random.default_rng(0)
    img = (rng.random((100, 100, 3)) * 255).astype(np.uint8)
    out = cp._apply_noise(img, 0)
    assert np.array_equal(img, out)


def test_apply_noise_has_visible_effect_at_full_frame_size():
    """Regression test: the previous bilateral-filter implementation had a
    near-zero effect at realistic full animation-frame resolution even though
    it looked effective on a small cropped test region with the same
    parameters. This verifies the replacement (non-local-means) produces a
    real, measurable change on a full-size (not cropped) frame."""
    rng = np.random.default_rng(1)
    img = (rng.random((1260, 620, 3)) * 40 + 30).astype(np.uint8)
    out = cp._apply_noise(img.copy(), 3)
    diff = np.abs(img.astype(np.int16) - out.astype(np.int16)).mean()
    assert diff > 2.0, f"noise level 3 should visibly change a full-size frame, diff={diff}"


# ── _detect_trail_mask ───────────────────────────────────────────────────────

def test_detect_trail_mask_does_not_flag_sparse_starfield():
    """Regression test: on a sparse starfield where >50% of pixels are exact
    background, the MAD-based sigma estimate collapses to 0 (median residual
    is 0, and MAD = median(|residual - 0|) is then also 0), making the
    "anomalously bright" threshold effectively 0 and flagging most of the
    frame's real stars as trail contamination. This corrupted every
    multi-frame comet_nucleus_stack.jpg composite that combined 2+ frames
    (single-frame outputs were unaffected since trail-masking only matters
    when accumulating a stack). A realistic starfield with many small point
    sources and no actual trails should flag well under 5% of pixels."""
    rng = np.random.default_rng(5)
    h, w = 600, 600
    lum = (rng.random((h, w)) * 5).astype(np.float32)  # faint noise background
    yy, xx = np.mgrid[0:h, 0:w]
    # scatter ~150 point-source stars of varying brightness
    star_rng = np.random.default_rng(6)
    for _ in range(150):
        cx = star_rng.integers(20, w - 20)
        cy = star_rng.integers(20, h - 20)
        amp = star_rng.uniform(200, 2000)
        lum += amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 2.0 ** 2))
    rgb = np.stack([lum, lum, lum], axis=-1)

    trail = cp._detect_trail_mask(rgb)
    flagged_frac = trail.mean()
    assert flagged_frac < 0.05, (
        f"sparse starfield should not be mostly flagged as trail contamination, "
        f"got {flagged_frac:.1%}"
    )


def test_detect_trail_mask_still_catches_a_real_trail():
    """A genuine long thin streak (satellite/aircraft) should still be
    detected after the sigma-collapse fix — this guards against overcorrecting
    the fix into never flagging anything."""
    rng = np.random.default_rng(5)
    h, w = 600, 600
    lum = (rng.random((h, w)) * 5).astype(np.float32)
    yy, xx = np.mgrid[0:h, 0:w]
    for _ in range(30):
        cx = rng.integers(20, w - 20)
        cy = rng.integers(20, h - 20)
        amp = rng.uniform(200, 1500)
        lum += amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 2.0 ** 2))
    rgb = np.stack([lum, lum, lum], axis=-1).astype(np.float32)
    # draw a long thin bright streak across the frame
    import cv2
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    cv2.line(bgr, (50, 50), (550, 300), (3000, 3000, 3000), 2, cv2.LINE_AA)
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    trail = cp._detect_trail_mask(rgb)
    assert trail.sum() > 100, "a genuine long thin streak should still be flagged as a trail"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
