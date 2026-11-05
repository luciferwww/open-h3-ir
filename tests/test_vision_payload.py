"""Vision previews are bounded; original conditioning assets are never rewritten."""
import base64
from dataclasses import replace
import hashlib
import io
import json
import random

import httpx
from PIL import Image
import pytest

from h3ir import analyse, backend
from h3ir.config import Config, LLMConfig
from h3ir.models import AssetKind, AssetRef, Role


@pytest.fixture(scope="module")
def large_image(tmp_path_factory):
    path = tmp_path_factory.mktemp("vision") / ("a" * 64)
    pixels = random.Random(42).randbytes(3072 * 2048 * 3)
    Image.frombytes("RGB", (3072, 2048), pixels).save(path, format="PNG")
    return path


def decode(url):
    header, encoded = url.split(",", 1)
    return header, base64.b64decode(encoded, validate=True)


@pytest.mark.parametrize("count", [1, 3])
def test_real_chat_body_fits_a_one_mib_endpoint(large_image, count):
    original = hashlib.sha256(large_image.read_bytes()).hexdigest()
    seen = []

    def endpoint(request):
        seen.append(request)
        if len(request.content) > 1024 * 1024:
            return httpx.Response(413, text="Request body exceeds its size limit.")
        return httpx.Response(200, json={"choices": [{
            "message": {"content": '{"summary":"a scene","subjects":[]}'},
            "finish_reason": "stop"}]})

    cfg = Config(llm=LLMConfig(model="vision"))
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        b = backend.Backend(cfg, client=client)
        result = b.json_call(
            [backend.user_message("Catalogue these frames.", [large_image] * count)],
            analyse.IMAGE_SCHEMA, required=("summary", "subjects"))
    assert result["summary"] == "a scene"
    assert len(seen) == 1
    body = json.loads(seen[0].content)
    urls = [p["image_url"]["url"] for p in body["messages"][0]["content"]
            if p["type"] == "image_url"]
    assert len(urls) == count
    total = 0
    for url in urls:
        header, data = decode(url)
        assert header == "data:image/jpeg;base64"
        total += len(data)
        with Image.open(io.BytesIO(data)) as preview:
            assert preview.format == "JPEG"
            assert max(preview.size) <= 2048
            assert preview.width / preview.height == pytest.approx(1.5, abs=.005)
    assert total <= 512 * 1024
    assert hashlib.sha256(large_image.read_bytes()).hexdigest() == original


def test_small_image_is_sent_byte_for_byte(tmp_path):
    path = tmp_path / "small"
    image = Image.new("RGB", (32, 16), "red")
    image.save(path, format="PNG")
    header, data = decode(backend.image_data_url(path))
    assert header == "data:image/png;base64"
    assert data == path.read_bytes()


def test_impossible_budget_raises(large_image):
    with pytest.raises(backend.BackendError, match="budget"):
        backend.image_data_url(large_image, max_bytes=1)


def test_invalid_image_raises(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    with pytest.raises(backend.BackendError, match="vision image"):
        backend.image_data_url(bad)


def test_analysis_uses_backend_limits_and_cache_tracks_them(large_image, tmp_path, monkeypatch):
    cfg = Config(llm=LLMConfig(model="vision", vision_max_edge=384,
                              vision_image_budget_bytes=32 * 1024))
    monkeypatch.setattr(analyse, "get_config",
                        lambda: replace(cfg, paths=replace(cfg.paths, state_dir=tmp_path)))
    seen = []

    def endpoint(request):
        body = json.loads(request.content)
        urls = [p["image_url"]["url"] for p in body["messages"][-1]["content"]
                if p["type"] == "image_url"]
        for url in urls:
            _, data = decode(url)
            assert len(data) <= b.cfg.vision_image_budget_bytes
            with Image.open(io.BytesIO(data)) as image:
                assert max(image.size) <= b.cfg.vision_max_edge
        seen.append(request)
        return httpx.Response(200, json={"choices": [{
            "message": {"content": '{"summary":"a scene","subjects":[]}'},
            "finish_reason": "stop"}]})

    ref = AssetRef(kind=AssetKind.IMAGE, role=Role.SUBJECT, sha256="a" * 64,
                   path=str(large_image))
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        b = backend.Backend(cfg, client=client)
        analyse.analyse_all(b, [ref])
        analyse.analyse_all(b, [ref])
        assert len(seen) == 1
        b.cfg = replace(cfg.llm, vision_max_edge=256)
        analyse.analyse_all(b, [ref])
        b.cfg = replace(b.cfg, vision_image_budget_bytes=16 * 1024)
        analyse.analyse_all(b, [ref])
    assert len(seen) == 3


def test_explicit_unbounded_mode_preserves_original(large_image):
    _, data = decode(backend.image_data_url(large_image, max_bytes=0, max_edge=0))
    assert data == large_image.read_bytes()


@pytest.mark.parametrize("kind", ["exif", "transparent"])
def test_preview_preserves_orientation_and_transparency(tmp_path, kind):
    path = tmp_path / "image"
    if kind == "exif":
        image = Image.new("RGB", (80, 40), "red")
        image.paste("blue", (40, 0, 80, 40))
        exif = Image.Exif()
        exif[274] = 6
        image.save(path, format="JPEG", exif=exif)
    else:
        Image.new("RGBA", (80, 40), (255, 0, 0, 0)).save(path, format="PNG")
    original = path.read_bytes()
    _, data = decode(backend.image_data_url(path, max_edge=20, max_bytes=0))
    with Image.open(io.BytesIO(data)) as preview:
        if kind == "exif":
            assert preview.size == (10, 20)
            assert preview.getpixel((5, 2))[0] > 200
            assert preview.getpixel((5, 17))[2] > 200
        else:
            assert preview.size == (20, 10)
            assert min(preview.getpixel((10, 5))) > 250
    assert path.read_bytes() == original


def test_server_still_refusing_has_actionable_error_and_no_retry():
    seen = []

    def endpoint(request):
        seen.append(len(request.content))
        return httpx.Response(413, text="Request body exceeds its size limit.")

    cfg = Config(llm=LLMConfig(model="vision"))
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        b = backend.Backend(cfg, client=client)
        with pytest.raises(backend.EndpointRefused) as refused:
            b.chat([backend.user_message("Hello")])
    assert len(seen) == 1
    assert refused.value.status == 413
    assert refused.value.body == "Request body exceeds its size limit."
    assert refused.value.request_bytes == seen[0]
    assert f"outgoing JSON: {seen[0]} bytes" in str(refused.value)
    assert "H3IR_VISION_IMAGE_BUDGET_BYTES" in str(refused.value)
