"""Facts the file itself carries, read instead of guessed. No model and no GPU.

`AssetRef.px` was populated in exactly one place in the whole codebase -- `evalloop/suite.py`, the
eval suite, from literals. The service, the CLI and the node all left it None, so everything
downstream took its fallback branch:

  * `plan.rows_for_image` returned the canvas figure for every reference regardless of its real
    size, so `sizing: "max"` and `sizing: "match"` published the same row cost for the same plate.
    A caller could not see the cost of the one knob they had turned, and README's cost claim rests
    on that number.
  * `mode.infer_mode`'s aspect-mismatch downgrade reads `a.px`, so it could never fire -- which was
    the only deterministic route to `status: needs_input`.

And the declared kind was equally unchecked: an mp4 attached as `kind: image` travelled to the
vision endpoint and came back as `HTTP 400: Failed to load image: cannot identify image file
<_io.BytesIO object>`, forwarded to the caller as a 502 with the inference server's internals in it.

The header reader remains stdlib only. Pillow prepares bounded vision previews elsewhere;
reading one number from the original file does not need to decode its pixels.
"""
from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from h3ir.analyse import AssetAnalysisError, image_size, measure_assets, sniff_container
from h3ir.grid import Target
from h3ir.models import AssetKind, AssetRef, Brief, Role
from h3ir.plan import build_manifest

REPO = Path(__file__).resolve().parents[1]
JPG = REPO / "docs/media/plate-car.jpg"
PNG = REPO / "docs/media/social-card.png"
WEBP = REPO / "docs/media/dial-restrained-vs-extreme.webp"
MP4 = REPO / "docs/media/off-vs-on.mp4"


# ---------------------------------------------------------------- the reader, against something else

@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="needs ffprobe to cross-check")
@pytest.mark.parametrize("path", [JPG, PNG, WEBP], ids=lambda p: p.suffix)
def test_the_dimensions_match_what_an_independent_tool_reports(path):
    """Checked against ffprobe rather than against a literal I wrote down. A test that asserts my
    own parser's output is a test that cannot fail when the parser is wrong.
    """
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
                         capture_output=True, text=True)
    expected = tuple(int(x) for x in out.stdout.strip().split("x")[:2])
    if expected == (0, 0):
        # ffprobe reports 0x0 for this repo's animated WebP; it is the cross-check that fails there,
        # not the reader, and pretending otherwise would be inventing agreement. The VP8X case gets
        # its own test below, against the field layout rather than against another tool.
        pytest.skip(f"ffprobe cannot size {path.name}")
    assert image_size(path) == expected


def test_the_extended_webp_size_comes_from_the_canvas_fields():
    """VP8X stores canvas width-1 and height-1 as three little-endian bytes each at offset 24, and
    this repo's only WebP is one. Asserted from the bytes on disk, so the test states the format
    rather than echoing the function."""
    head = WEBP.read_bytes()[:30]
    assert head[:4] == b"RIFF" and head[8:12] == b"WEBP" and head[12:16] == b"VP8X"
    expected = (int.from_bytes(head[24:27], "little") + 1,
                int.from_bytes(head[27:30], "little") + 1)
    assert image_size(WEBP) == expected == (760, 212)


def test_the_png_reader_agrees_with_the_ihdr_header_directly():
    """A second independent check for PNG, using the suite's existing IHDR read."""
    head = PNG.read_bytes()[:33]
    assert head[12:16] == b"IHDR"
    assert image_size(PNG) == struct.unpack(">II", head[16:24])


def test_an_unrecognised_file_reads_as_no_size_rather_than_a_wrong_one(tmp_path):
    """None is the honest answer and the safe one: everything downstream already handles it, and a
    guessed size would put a wrong row cost in the manifest with nothing to reveal it."""
    f = tmp_path / "mystery.tiff"
    f.write_bytes(b"II*\x00" + b"\x00" * 64)
    assert image_size(f) is None
    assert sniff_container(f) is None


@pytest.mark.parametrize("path,kind", [(JPG, "image"), (PNG, "image"), (WEBP, "image"),
                                       (MP4, "video")])
def test_the_container_sniff_places_the_files_this_repo_ships(path, kind):
    assert sniff_container(path) == kind


def test_a_wav_is_audio_and_not_confused_with_a_webp(tmp_path):
    """WEBP, WAVE and AVI all open with RIFF, so the four bytes after the size are what separate
    them. Getting that wrong would refuse a legitimate attachment."""
    f = tmp_path / "tone.wav"
    f.write_bytes(b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 32)
    assert sniff_container(f) == "audio"


# ---------------------------------------------------------------- what a data URL declares

def test_the_image_type_is_read_out_of_the_bytes(tmp_path):
    """The sniffer the data URL is built on. Asserted on files whose names say nothing at all, since
    a name that happens to match proves nothing about whether the bytes were read: a first version
    of this used the repo's own correctly-named samples and passed with the sniffing removed.
    """
    from h3ir.analyse import image_mime

    for source, expected in ((JPG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp")):
        blind = tmp_path / f"{expected.split('/')[1]}-with-no-name"
        blind.write_bytes(source.read_bytes())
        assert image_mime(blind) == expected
    (tmp_path / "clip").write_bytes(MP4.read_bytes())
    assert image_mime(tmp_path / "clip") is None, "only an image gets an image type"
    (tmp_path / "junk").write_bytes(b"II*\x00" + b"\x00" * 40)
    assert image_mime(tmp_path / "junk") is None, "an unrecognised file is not guessed at"


def test_a_stored_upload_with_no_extension_is_still_declared_correctly(tmp_path):
    """The case this exists for: the file is named by its sha256 and nothing else."""
    from h3ir.backend import image_data_url

    f = tmp_path / ("a" * 64)
    f.write_bytes(JPG.read_bytes())
    assert image_data_url(f).startswith("data:image/jpeg;base64,"), \
        "an extensionless file was announced as something it is not"


@pytest.mark.parametrize("name", ["plate.png", "plate.txt", "plate"])
def test_a_jpeg_is_a_jpeg_whatever_it_is_called(tmp_path, name):
    from h3ir.backend import image_data_url

    f = tmp_path / name
    f.write_bytes(JPG.read_bytes())
    assert "image/jpeg" in image_data_url(f)


# ---------------------------------------------------------------- px reaches the manifest

def test_measuring_fills_in_the_dimensions_the_caller_did_not_supply():
    ref = AssetRef(kind=AssetKind.IMAGE, role=Role.SUBJECT, sha256="p", path=str(JPG))
    assert ref.px is None
    measure_assets([ref])
    assert ref.px == (1400, 933)


def test_a_caller_supplied_size_is_never_overwritten():
    """The node knows its tensor's shape exactly and passes it. Re-deriving it from a re-encoded
    temp file would be this layer second-guessing a fact it was given."""
    ref = AssetRef(kind=AssetKind.IMAGE, role=Role.SUBJECT, sha256="p", path=str(JPG),
                   px=(640, 360))
    measure_assets([ref])
    assert ref.px == (640, 360)


def test_max_sizing_now_costs_more_rows_than_match_for_the_same_plate():
    """The defect, stated as arithmetic. Both used to report 1008 -- the canvas figure, which is
    what `rows_for_image` short-circuits to when px is None -- so the knob had no visible effect,
    and the `match` figure was wrong as well as the `max` one.
    """
    target = Target.build(5.0)
    rows = {}
    for sizing in ("match", "max"):
        ref = AssetRef(kind=AssetKind.IMAGE, role=Role.SUBJECT, sha256="p", path=str(JPG),
                       sizing=sizing)
        measure_assets([ref])
        rows[sizing] = build_manifest(Brief(intent="x", assets=[ref]), target)[0].rows

    from h3ir.grid import rows_per_latent_frame

    unmeasured = AssetRef(kind=AssetKind.IMAGE, role=Role.SUBJECT, sha256="p", path=str(JPG))
    old = build_manifest(Brief(intent="x", assets=[unmeasured]), target)[0].rows
    assert old == rows_per_latent_frame(*target.canvas) == 1008, "the old figure was the canvas"
    assert rows["match"] != rows["max"], rows
    assert rows["max"] > rows["match"], rows
    # This 1400x933 plate, through the same arithmetic the runtime's own sizing follows: `match`
    # scales to the generation's pixel area, `max` to a 2048 short edge. Neither is 1008.
    assert (rows["match"], rows["max"]) == (1014, 1276), rows


def test_the_row_figure_tracks_the_actual_image(tmp_path):
    """A bigger plate has to cost more. With px unpopulated every image in the world reported the
    same 1008, which is the shape of the bug rather than a rounding detail."""
    small = tmp_path / "small.png"
    big = tmp_path / "big.png"
    for path, (w, h) in ((small, (640, 360)), (big, (4000, 3000))):
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
                         + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
                         + b"\x00" * 4)
    target = Target.build(5.0)
    got = []
    for path in (small, big):
        ref = AssetRef(kind=AssetKind.IMAGE, role=Role.SUBJECT, sha256=path.name, path=str(path),
                       sizing="max")
        measure_assets([ref])
        got.append(build_manifest(Brief(intent="x", assets=[ref]), target)[0].rows)
    assert got[1] > got[0], got


# ---------------------------------------------------------------- what a real size unlocks

def test_a_square_plate_on_a_wide_target_lowers_the_confidence(tmp_path):
    """The one deterministic route to `status: needs_input`, which could not fire at all while px
    was None: `mode.infer_mode` reads `a.px` to notice that an anchor's aspect does not match the
    target it will be stretched to. The classifier returned 0.95 on all 29 samples the audit took,
    so this route was the only one left and it was unreachable.
    """
    from h3ir.mode import infer_mode, needs_clarification

    square = tmp_path / "square.png"
    square.write_bytes(b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
                       + (768).to_bytes(4, "big") + (768).to_bytes(4, "big")
                       + b"\x08\x02\x00\x00\x00" + b"\x00" * 4)
    ref = AssetRef(kind=AssetKind.IMAGE, role=Role.SUBJECT, sha256="sq", path=str(square))
    measure_assets([ref])
    assert ref.px == (768, 768)

    brief = Brief(intent="Animate this photo so the car pulls forward.", aspect="16:9",
                  assets=[ref])
    d = infer_mode(brief, {}, backend=None)
    assert d.confidence == 0.6, d
    assert any("aspect mismatch" in s for s in d.signals), d.signals
    q = needs_clarification(d)
    assert q and q["id"] == "anchor_or_reference", q


def test_a_matching_aspect_asks_nothing():
    """The question is only worth asking where the wrong default is visible in the output. A plate
    that already matches the target gets no ceremony."""
    from h3ir.mode import infer_mode, needs_clarification

    ref = AssetRef(kind=AssetKind.IMAGE, role=Role.SUBJECT, sha256="p", path=str(PNG))
    measure_assets([ref])                      # 1280x640, close enough to 16:9
    d = infer_mode(Brief(intent="Animate this photo so the car pulls forward.", aspect="16:9",
                         assets=[ref]), {}, backend=None)
    assert d.confidence == 0.85
    assert needs_clarification(d) is None


# ---------------------------------------------------------------- the wrong kind, said plainly

def test_a_video_attached_as_an_image_is_refused_before_the_model_sees_it():
    """The 502 that leaked the inference server's internals. The refusal names both kinds and what
    to do, and it happens at intake rather than after a round trip."""
    ref = AssetRef(kind=AssetKind.IMAGE, role=Role.SUBJECT, sha256="v", path=str(MP4))
    with pytest.raises(AssetAnalysisError) as e:
        measure_assets([ref])
    msg = str(e.value)
    assert "off-vs-on.mp4 was attached as kind: image" in msg
    assert "its bytes are a video file" in msg
    assert "Attach it with kind: video" in msg


def test_an_image_attached_as_a_video_is_refused_the_same_way():
    ref = AssetRef(kind=AssetKind.VIDEO, role=Role.EDIT_SOURCE, sha256="i", path=str(JPG))
    with pytest.raises(AssetAnalysisError) as e:
        measure_assets([ref])
    assert "attached as kind: video" in str(e.value)
    assert "Attach it with kind: image" in str(e.value)


def test_an_audio_declaration_is_left_alone(tmp_path):
    """m4a is audio inside an ISO container that sniffs as video, so audio is deliberately outside
    this check. Refusing a legitimate m4a to improve one error message would be a bad trade."""
    f = tmp_path / "voice.m4a"
    f.write_bytes(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 32)
    ref = AssetRef(kind=AssetKind.AUDIO, role=Role.VOICE_TIMBRE, sha256="a", path=str(f))
    measure_assets([ref])          # no raise


def test_the_refusal_reaches_the_caller_as_a_422(monkeypatch):
    """Through the compiler, where measuring runs, and before the backend is probed."""
    from starlette.testclient import TestClient

    from h3ir import service

    client = TestClient(service.app, raise_server_exceptions=False)
    r = client.post("/v1/briefs", json={"intent": "Animate this photo.",
                                        "assets": [{"path": str(MP4), "kind": "image"}]})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "asset-unreadable"
    assert "Attach it with kind: video" in r.json()["detail"]["message"]
