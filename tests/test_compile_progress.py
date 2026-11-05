from dataclasses import replace

import pytest

from h3ir import compile as core
from h3ir.backend import Backend
from h3ir.models import Brief


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr(Backend, "require_available", lambda self: None)
    monkeypatch.setattr(Backend, "server_version", lambda self: "progress-test")
    with Backend() as instance:
        yield instance


def test_real_stages_precede_work_and_do_not_change_output(backend, monkeypatch):
    stages = []
    original_analyse = core.analyse_all

    def analyse(*args, **kwargs):
        assert stages[-1] == "analysis"
        return original_analyse(*args, **kwargs)

    baseline = core.compile_brief(Brief("A fox walks."), backend=backend, llm=False, seed=7)
    monkeypatch.setattr(core, "analyse_all", analyse)
    result = core.compile_brief(Brief("A fox walks."), backend=backend, llm=False, seed=7,
                                progress=lambda stage, message: stages.append(stage))
    assert stages == ["request", "connection", "analysis", "mode", "draft", "fallback"]
    assert result.prompt == baseline.prompt
    assert result.source == baseline.source


def test_write_validate_correction_and_fallback_are_real(backend, monkeypatch):
    stages = []
    monkeypatch.setattr(core, "compose_brief", lambda *args, **kwargs: "invalid document")
    from h3ir import prose
    monkeypatch.setattr(prose, "fix_with_findings", lambda *args, **kwargs: "invalid document")
    result = core.compile_brief(Brief("A fox walks."), backend=backend,
                                progress=lambda stage, message: stages.append(stage))
    assert result.source == "draft"
    assert stages[stages.index("writing"):] == [
        "writing", "validation", "correction", "validation", "correction", "validation", "fallback"]
    assert result.provenance["fix_rounds"] == 2


def test_refinement_reports_before_amending_and_propagates_callback(backend, monkeypatch):
    stages = []

    def amend(brief, change, actual_backend):
        assert stages == ["refinement"]
        return replace(brief, seconds=8), ["seconds"]

    monkeypatch.setattr(core, "amend_brief", amend)
    result, brief, changed = core.refine(
        Brief("A fox walks."), "Make it 8 seconds.", backend=backend, llm=False,
        progress=lambda stage, message: stages.append(stage))
    assert stages[:3] == ["refinement", "request", "connection"]
    assert "draft" in stages
    assert brief.seconds == 8 and changed == ["seconds"]
    assert result.ok
