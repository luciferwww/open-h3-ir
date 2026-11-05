"""Video references: real frames, or a loud failure.

Nothing produced frames before this. `analyse_video` accepted a frame list and every caller passed
none, so it fell through to `[ref.path]` -- handing a vision model the `.mp4` itself, base64'd into an
`image_url` field. The model received a video blob where an image belongs and returned a card with the
right shape describing nothing in particular. Silent in both directions: no exception, no validator
error, and a compiler perfectly willing to build a brief on it.

These tests need ffmpeg, which is now a hard runtime dependency of video references rather than a
convenience. A skip here means the feature cannot work on this machine, so the skip says so.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from h3ir.analyse import (ANALYZER_VERSION, AssetAnalysisError, VIDEO_FRAME_FRACTIONS,
                          analyse_video, probe_seconds, sample_frames, sha256_file)
from h3ir.backend import Backend
from h3ir.models import AssetKind, AssetRef, Role

ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe missing — video references cannot work on this machine at all")


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> Path:
    """ffmpeg's own animated test pattern: a moving bar and a running counter, so frames sampled at
    different times MUST differ. A solid-colour clip would let a broken sampler pass."""
    out = tmp_path_factory.mktemp("video") / "clip.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-f", "lavfi",
                    "-i", "testsrc=size=320x180:rate=24:duration=6",
                    "-pix_fmt", "yuv420p", str(out)], capture_output=True, timeout=120)
    return out


# ---------------------------------------------------------------- sampling

@ffmpeg
def test_frames_are_sampled_from_across_the_clip_and_are_distinct(clip):
    """The point of sampling is temporal coverage. Identical frames would mean the sampler ran and
    took the same moment three times, which is the shape of a passing test that proves nothing.

    Uses a unique cache key: on the first run of this test the real key already held frames from an
    earlier correct run, so breaking the fractions on purpose changed nothing and the test passed. The
    cache was answering instead of the sampler.
    """
    frames = sample_frames(clip, "distinctness" + "0" * 52)
    assert len(frames) == len(VIDEO_FRAME_FRACTIONS)
    digests = [hashlib.sha256(Path(f).read_bytes()).hexdigest() for f in frames]
    assert len(set(digests)) == len(digests), "the sampler took the same moment more than once"
    assert all(Path(f).stat().st_size > 500 for f in frames), "a frame that small is not an image"


@ffmpeg
def test_the_duration_comes_from_the_file_not_the_caller(clip):
    """A caller's `seconds` is a claim; the file is ground truth."""
    assert probe_seconds(clip) == pytest.approx(6.0, abs=0.2)


@ffmpeg
def test_frames_are_cached_on_content_hash(clip):
    sha = sha256_file(clip)
    first = sample_frames(clip, sha)
    mtimes = [Path(f).stat().st_mtime_ns for f in first]
    again = sample_frames(clip, sha)
    assert again == first
    assert [Path(f).stat().st_mtime_ns for f in again] == mtimes, "re-extracted instead of reusing"


# ---------------------------------------------------------------- the failure is loud

def test_a_video_with_no_path_raises_instead_of_returning_an_empty_card():
    """A card describing nothing is indistinguishable from a card describing something dull, and the
    compiler would build a brief on it without noticing."""
    ref = AssetRef(kind=AssetKind.VIDEO, role=Role.EDIT_SOURCE, sha256="a" * 64, path="")
    with pytest.raises(AssetAnalysisError, match="no frames"):
        analyse_video(None, ref)


def test_a_missing_file_raises():
    with pytest.raises(AssetAnalysisError, match="does not exist"):
        sample_frames("/nonexistent/clip.mp4", "b" * 64)


def test_an_unreadable_file_raises_rather_than_yielding_zero_frames(tmp_path):
    """Not a video. ffprobe finds no duration, and that has to stop the analysis rather than produce
    a card from nothing."""
    fake = tmp_path / "not-a-video.mp4"
    fake.write_bytes(b"this is not a video file")
    with pytest.raises(AssetAnalysisError):
        sample_frames(fake, "c" * 64)


# ---------------------------------------------------------------- the regression guard

@ffmpeg
def test_the_video_file_is_never_sent_as_an_image(clip):
    """The exact bug. `image_data_url` guesses the mime from the extension, so a `.mp4` went out as
    `data:video/mp4;base64,…` inside an `image_url` — a whole video in a field for a picture."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"summary": "s", "subjects": []})}}], "usage": {}})

    ref = AssetRef(kind=AssetKind.VIDEO, role=Role.EDIT_SOURCE, sha256=sha256_file(clip),
                   path=str(clip))
    b = Backend(client=httpx.Client(transport=httpx.MockTransport(handler)))
    card = analyse_video(b, ref)

    parts = captured["messages"][-1]["content"]
    urls = [p["image_url"]["url"] for p in parts if p.get("type") == "image_url"]
    assert urls, "the frames must actually be attached"
    assert all(u.startswith("data:image/") for u in urls), [u[:40] for u in urls]
    assert not any("video/" in u[:40] for u in urls)
    assert card.frames_seen == len(VIDEO_FRAME_FRACTIONS)


def test_the_analyzer_version_moves_when_the_card_contract_does():
    """Every bump so far was mandatory and for the same reason: a cached card whose contract has
    changed is served silently and looks correct.

      1 -> 2  subjects split into `attributes` and `pose`
      2 -> 3  video cards built from sampled FRAMES, not from a base64 video blob
      3 -> 4  `characterisation` split out of an audio card's `summary`

    Asserted as a floor rather than an equality so the next necessary bump does not have to argue
    with this test -- but it must never go BACKWARDS, which is what would silently resurrect a card
    built the broken way."""
    assert int(ANALYZER_VERSION) >= 4


@ffmpeg
def test_the_frames_are_presented_as_one_clip_rather_than_separate_references(clip):
    """Three images of one subject read as three subjects unless the ask says otherwise -- and the
    six-section format has a label namespace that would happily bind three of them."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"summary": "s", "subjects": []})}}], "usage": {}})

    b = Backend(client=httpx.Client(transport=httpx.MockTransport(handler)))
    analyse_video(b, AssetRef(kind=AssetKind.VIDEO, role=Role.EDIT_SOURCE,
                              sha256=sha256_file(clip), path=str(clip)))
    system = captured["messages"][0]["content"]
    assert "SAME clip" in system
    assert "not separate" in system
    assert "describe the subject once" in system


def test_the_video_analyser_gets_the_deep_retry_budget():
    """Multi-frame vision is the endpoint's flakiest structured call (measured: the same clip
    failed the schema-echo check twice, passed third), and a card that cannot be built kills the
    whole brief. The budget is part of the call's contract, so it is pinned."""
    from h3ir.analyse import analyse_video
    from h3ir.models import AssetKind, AssetRef

    from h3ir.config import LLMConfig

    class Recorder:
        cfg = LLMConfig(model="fake-model")

        def json_call(self, messages, schema, **kw):
            self.kw = kw
            return {"summary": "a clip", "subjects": []}

    import base64
    import tempfile

    b = Recorder()
    ref = AssetRef(kind=AssetKind.VIDEO, path="", sha256="a" * 64, role=None)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png)
    analyse_video(b, ref, frames=[f.name])
    assert b.kw.get("retries") == 5

    from h3ir.analyse import analyse_image
    ref_img = AssetRef(kind=AssetKind.IMAGE, path=f.name, sha256="b" * 64, role=None)
    analyse_image(b, ref_img)
    assert b.kw.get("retries") == 5, "the image analyser shares the deep budget"
