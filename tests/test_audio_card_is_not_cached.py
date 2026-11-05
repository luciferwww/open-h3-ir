"""An audio card is 100% caller-supplied metadata, so caching it by content hash loses the caller.

Found by re-firing row 31 against the live service after the transcript was plumbed through to the
composer: the words still did not reach `<d>` in any of 7 runs, and the reason was upstream of every
change. `analyse_all` consults the card cache first, the key is
`sha256 | ANALYZER_VERSION | model | kind`, and for audio the card contains nothing derived from the
bytes at all -- `analyse_audio` makes no model call by design, because nothing here can hear. Every
field in it comes from the request:

    role      -> the summary sentence ("a spoken vocal reference supplying voice timbre")
    note      -> `characterisation`, the ONLY channel the encoder has for what the audio sounds like
    transcript-> the words, supplied by the caller's own recogniser
    seconds   -> the duration

None of those is in the key. So the first request that attached a wav saved a card, and every later
request attaching the SAME wav with a different note, a different role, or a transcript got the first
request's card back. The live cache entry for the probe's `aud_voice.wav` held `transcript: ''` and a
note from a different request entirely ("a low, slow, slightly hoarse female voice" against a request
that said "a flat male voice, close mic"), and that is what reached the writer.

Caching it bought nothing either: there is no model call and no file read to save. So audio is not
cached, in both directions.
"""
from __future__ import annotations

from h3ir.analyse import analyse_all, save_cached
from h3ir.config import LLMConfig
from h3ir.models import AssetCard, AssetKind, AssetRef, Role

WORDS = "We close at six, not half past."


class _Backend:
    cfg = LLMConfig(model="test-model")


def _wav(note: str, role: Role = Role.VOICE_TIMBRE) -> AssetRef:
    return AssetRef(kind=AssetKind.AUDIO, role=role, sha256="a" * 64, seconds=3.0, note=note)


def _poison_the_cache() -> None:
    """A card for the same bytes from an earlier request: no transcript, someone else's note."""
    stale = AssetCard(sha256="a" * 64, kind=AssetKind.AUDIO,
                      summary="a spoken vocal reference supplying voice timbre and delivery, "
                              "described by the caller as: a low, slow, slightly hoarse female "
                              "voice (3.00s).",
                      characterisation="a low, slow, slightly hoarse female voice",
                      transcript="", analyzer_version="4",
                      model_id="none (typed metadata only)")
    save_cached(_wav("a low, slow, slightly hoarse female voice"), stale, "test-model")


def test_the_transcript_survives_a_cache_hit():
    _poison_the_cache()
    cards = analyse_all(_Backend(), [_wav("a flat male voice, close mic")],
                        transcripts={"a" * 64: WORDS})
    assert cards["a" * 64].transcript == WORDS


def test_the_callers_note_survives_a_cache_hit():
    """`characterisation` is the only channel for timbre, and X15 exists to demand it, so serving
    another request's note is worse than serving none: it is an assertion about a sound."""
    _poison_the_cache()
    cards = analyse_all(_Backend(), [_wav("a flat male voice, close mic")], transcripts={})
    card = cards["a" * 64]
    assert card.characterisation == "a flat male voice, close mic"
    assert "flat male voice" in card.summary
    assert "hoarse female" not in card.summary


def test_the_role_survives_a_cache_hit():
    """The summary sentence is chosen from the role, so a card cached as a voice reference described
    a background music track as a voice."""
    _poison_the_cache()
    cards = analyse_all(_Backend(), [_wav("a slow piano loop", role=Role.BGM)], transcripts={})
    assert "background music track" in cards["a" * 64].summary


def test_nothing_is_written_to_the_cache_for_audio_either():
    """A write is as wrong as a read: it is what poisons the next request."""
    from h3ir.analyse import _cache_path, _cache_key

    ref = _wav("a flat male voice, close mic")
    p = _cache_path(_cache_key(ref, "test-model"))
    if p.exists():
        p.unlink()
    analyse_all(_Backend(), [ref], transcripts={"a" * 64: WORDS})
    assert not p.exists(), "an audio card was cached; the next request's note would be overwritten"


def test_an_image_card_is_still_cached():
    """The distinction is not "caching is bad": an image card IS derived from the bytes by a model
    call, so content-addressing it is right and the saving is real."""
    from h3ir.analyse import _cache_key, _cache_path, load_cached

    ref = AssetRef(kind=AssetKind.IMAGE, role=Role.SUBJECT, sha256="b" * 64, px=(512, 512))
    card = AssetCard(sha256="b" * 64, kind=AssetKind.IMAGE, summary="a black car",
                     analyzer_version="4", model_id="test-model")
    save_cached(ref, card, "test-model")
    assert _cache_path(_cache_key(ref, "test-model")).exists()
    assert load_cached(ref, "test-model") is not None
    cards = analyse_all(_Backend(), [ref])
    assert cards["b" * 64].summary == "a black car", "the image cache hit is still used"
