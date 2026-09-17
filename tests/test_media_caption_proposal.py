"""Slice 3: a 1.2.2 finding arrives with a DRAFTED caption file a reviewer can approve.

THE GAP, measured on `origin/main` at ff422436. Slices 1 and 2 built both halves and left them
unconnected: `api/captions.py` can write a WebVTT from a transcript, `formats/av` can find that a
video has none — and nothing joins them, because a media file never reaches the remediation lane
at all. Two gates, both spelling out the same six extensions:

    api/routes/scans.py:508   if not f["file"].lower().endswith(
                                  (".html", ".htm", ".pdf", ".docx", ".pptx", ".xlsx")): continue
    api/handlers.py:610       if ext not in ("html", "htm", "pdf", *_OFFICE_MIME):
                                  log_decision("remediate.deferred", "no server-side remediator")

So no `remediate_file` job is enqueued for a `.mp4`, and if one were, the handler would return
before `_propose_text_findings` ran. A reviewer opening the 1.2.2 card got a finding and a blank
box — the "author it yourself" state the whole proposal lane exists to remove.

WHY A CAPTION DRAFT IS NOT WRITTEN INTO THE FILE. A caption track COULD be muxed into an .mp4,
and doing that re-encodes and re-authors a customer's video — precisely what ADR 0016 refuses for
PDF re-tagging. The approved WebVTT is delivered as a COMPANION file instead, which is also what
WCAG asks for: a caption file beside the media is a conforming alternative, and it is how every
player already expects to find one.

THIS SLICE SHIPPED THAT AS `explain_only=True`, AND IT WAS HALF RIGHT. It bought the property that
matters to the certify gate — `store._row_approved_values` skips such a value, so the file is not
wedged forever owing content no applier will write. It also bought three things nobody wanted: the
card rendered READ-ONLY, approving sent NO value, and no route could hand the file back. ACP told
the reviewer to check a machine transcript for misheard names and gave them no way to change a
word of it.

The follow-up (`tests/test_caption_companion_file.py`) splits the two meanings: `companion_file`
keeps the gate property and makes the value editable, stored and downloadable. The assertion below
moved with it — deliberately, and by rewriting rather than deleting, because what it was really
protecting is the gate, and the gate has not changed.

WHAT IS DELIBERATELY NOT CLAIMED HERE. The draft is machine transcription: it is never
auto-applied, never `validated=True`, and the card says which model wrote it. ASR gets names,
punctuation and homophones wrong, and a wrong caption is worse than an absent one because it looks
served. `fix_mode` for 1.2.1/1.2.2/1.2.3 therefore stays `human-only` — asserted in
tests/test_media_captions_gap.py, and still true after this slice: drafting is not fixing.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "api", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import captions as cap  # noqa: E402
import media  # noqa: E402
import proposals as prop  # noqa: E402
from engines import ASR_OK, MEDIA_OK, NO_ASR, NO_MEDIA  # noqa: E402

needs_media = pytest.mark.skipif(not MEDIA_OK, reason=NO_MEDIA)
needs_asr = pytest.mark.skipif(not ASR_OK, reason=NO_ASR)


def _clip(path: Path, *, seconds: int = 3, audio: bool = True) -> Path:
    ff = media.ffmpeg_path()
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, f"fixture build failed: {r.stderr[-400:]}"
    return path


# ── the gap this slice closes ───────────────────────────────────────────────────────────────
def test_media_now_reaches_the_remediation_lane():
    """Both gates admit media. RED BEFORE THIS SLICE on both counts.

    Read from the source rather than by running a scan: the enqueue gate lives in a route that
    needs a Drive token and a store, and what changed is the ADMISSIBILITY decision, which is a
    property of the predicate. `remediable_extensions()` is that predicate, in one place, so the
    route and the handler can no longer disagree — which they would have, spelled out twice.
    """
    import handlers
    exts = handlers.remediable_extensions()
    for ext in (".html", ".htm", ".pdf", ".docx", ".pptx", ".xlsx"):
        assert ext in exts, f"{ext} lost its server-side remediator"
    assert ".mp4" in exts and ".mp3" in exts, (
        "media is still refused by the remediation lane, so no caption draft can ever be made")


def test_the_two_gates_read_the_same_predicate():
    """The gates were two literals that had to agree and nothing made them.

    A file admitted by the route and refused by the handler burns a job and logs a deferral; one
    admitted by the handler and refused by the route is a code path nothing can reach. Neither
    fails loudly. Asserted structurally — the route must not carry its own extension literal.
    """
    route = (ROOT / "api" / "routes" / "scans.py").read_text()
    assert '(".html", ".htm", ".pdf", ".docx", ".pptx", ".xlsx")' not in route, (
        "routes/scans.py still spells out its own remediable-extension tuple; it must ask "
        "handlers.remediable_extensions() so the two gates cannot drift")


# WHY THE SPEECH IS STUBBED AND NOTHING ELSE IS.
#
# The first draft of the two tests below built a real .mp4 and expected a caption card out of the
# real ASR. Both failed, and the implementation was right: a 440 Hz tone contains NO SPEECH, so
# `transcribe` returns a Transcript with an empty `segments` list and the proposer correctly
# declines — an empty WEBVTT is not a caption file, and offering one invites a reviewer to approve
# captions for a soundtrack that carries none.
#
# Fixing that needs audio with words in it, and there is no speech synthesiser in this environment
# (checked: no espeak, espeak-ng, flite or pico2wave). So the ASR's OUTPUT is stubbed and every
# other stage stays real: ffmpeg builds a genuine H.264+AAC container, `probe` reads its stream
# table, `extract_audio` decodes a real 16 kHz mono WAV, and the segmentation and writers run on
# the stub's segments. What is not exercised is the model's word accuracy — which
# `tests/test_media_engine.py` says outright is not this repo's property to assert, since a model
# upgrade that improved it would fail a pinned transcript.
#
# `test_real_speech_recognition_on_speechless_audio_declines` below keeps the REAL engine in play
# for the thing a tone can honestly settle: the whole path runs, and no speech means no card.
_STUB_SEGMENTS = [
    {"start": 0.0, "end": 2.4, "text": "Welcome to the quarterly all-hands."},
    {"start": 2.4, "end": 5.1, "text": "First, a word on the accessibility programme."},
]


def _stub_asr(monkeypatch, *, model="stub-tiny", language="en"):
    """Replace only the model call. `extract_audio` still runs, so a broken decode still fails."""
    def _fake(wav, **kw):
        assert Path(str(wav)).exists(), "the proposer must transcribe a real extracted WAV"
        return media.Transcript(segments=list(_STUB_SEGMENTS), language=language,
                                duration=5.1, model=model)
    monkeypatch.setattr(media, "transcribe", _fake)
    monkeypatch.setattr(media, "asr_available", lambda: True)


@needs_media
def test_a_captionless_video_produces_an_approvable_caption_file(tmp_path, monkeypatch):
    """THE POINT OF THE SLICE. A real .mp4 in, a valid WebVTT out, on a proposal card."""
    _stub_asr(monkeypatch)
    clip = _clip(tmp_path / "townhall.mp4")
    props = prop.propose_captions(clip, ".mp4")
    assert props, "no caption proposal for a captionless video with a soundtrack"

    p = props[0]
    assert p.get("companion_file"), (
        "the caption draft declares no companion filename, so nothing can deliver it under a name "
        "a player will look for")
    from store import Store
    row = {"proposals": [dict(p, approved_value=p["proposed_value"])], "evidence": [],
           "approved_value": "", "resolution": None}
    assert Store._row_approved_values(row) == {}, (
        "the caption value entered the applier's work list; nothing will ever write it into an "
        "mp4, so the file could never certify — the property explain_only originally bought and "
        "companion_file now carries")
    assert p["sc"] == "1.2.2", "a caption draft filed under any other criterion misroutes the card"
    assert p["proposed_value"].startswith("WEBVTT"), p["proposed_value"][:80]
    assert cap.parse_cues(p["proposed_value"]), "what was written must read back as cues"
    assert "stub-tiny" in p["source"], (
        f"the card must name the model that actually ran, not a hardcoded name: {p['source']}")
    assert p["why_review"], "a machine-written draft must say why a person is being asked"


@needs_media
def test_an_audio_only_file_is_offered_a_transcript_under_1_2_1(tmp_path, monkeypatch):
    """The 1.2.1 fork, carried through to the artefact. A podcast needs a TRANSCRIPT — flowing
    text a person reads — not a cue file synchronised to a picture that does not exist."""
    _stub_asr(monkeypatch)
    pod = tmp_path / "interview.mp3"
    ff = media.ffmpeg_path()
    r = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=3", str(pod)],
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-300:]

    props = prop.propose_captions(pod, ".mp3")
    assert props and props[0]["sc"] == "1.2.1"
    assert not props[0]["proposed_value"].startswith("WEBVTT"), (
        "an audio-only file was offered a cue file; 1.2.1 asks for a text transcript")
    assert "all-hands" in props[0]["proposed_value"], "the transcript must carry the words"


@needs_media
@needs_asr
def test_real_speech_recognition_on_speechless_audio_declines(tmp_path):
    """The real engine, end to end, asserting the one thing a tone can settle.

    Every stage runs for real — container, probe, decode, model load, transcription — and the
    correct outcome is NO CARD, because a 440 Hz tone holds no speech. This is the test that
    would catch the pipeline breaking; the stubbed ones above check the artefact's shape.
    """
    clip = _clip(tmp_path / "tone.mp4")
    assert prop.propose_captions(clip, ".mp4") == [], (
        "a soundtrack with no speech produced a caption draft — an empty cue file invites a "
        "reviewer to approve captions for words nobody said")


# ── the refusals, which are most of the correctness ─────────────────────────────────────────
@needs_media
def test_no_proposal_when_the_transcriber_is_missing(tmp_path, monkeypatch):
    """FAIL CLOSED, one more time. `transcribe` returns None when nothing ran, and a proposer
    that turned that into an empty-but-present caption file would hand a reviewer a blank VTT to
    approve — certifying a video nobody transcribed, with a human's name on it.

    No proposal is the correct outcome: the 1.2.2 FINDING still stands, and the reviewer authors
    captions the way they did before. Silence here removes a draft, never a finding.

    A REAL CLIP, not 64 null bytes. The first version of this test wrote junk to an .mp4 and
    disabled the transcriber — so `probe` returned None and the function declined for THAT reason,
    with the transcriber check never reached. A bite check removing the `asr_available` guard left
    it green: the test asserted the right outcome and proved nothing about the cause. The junk-file
    case has its own test below, where it is the point rather than a confound.
    """
    monkeypatch.setattr(media, "asr_available", lambda: False)
    clip = _clip(tmp_path / "townhall.mp4")
    assert prop.propose_captions(clip, ".mp4") == []


@needs_media
def test_a_transcription_that_fails_mid_run_returns_nothing_rather_than_raising(tmp_path,
                                                                                monkeypatch):
    """The engine is PRESENT and the transcription still returns None — a model that will not
    load, a WAV the decoder rejects. This is the only path that reaches the `result is None`
    guard, and it took a bite check to notice no test did.

    The other "no transcriber" test never gets here: it disables `asr_available`, so the early
    check declines first. Deleting the None guard therefore changed nothing that test could see
    — and would have left `segment_cues(None.segments)` raising AttributeError out of a proposer
    whose whole contract is that a failed draft is silent, not an exception on the remediation
    path.
    """
    monkeypatch.setattr(media, "asr_available", lambda: True)
    monkeypatch.setattr(media, "transcribe", lambda *a, **kw: None)
    clip = _clip(tmp_path / "townhall.mp4")
    assert prop.propose_captions(clip, ".mp4") == []


@needs_media
def test_no_audio_is_decoded_when_there_is_nothing_to_transcribe(tmp_path, monkeypatch):
    """The early engine check is an OPTIMISATION, and this is the only test that can say so.

    A bite check established that deleting it changes no outcome: `media.transcribe` returns None
    without an engine, so the proposer declines anyway through the fail-closed guard. What the
    early check buys is the ffmpeg decode it skips — a full audio extraction per media file, on
    every remediation, on a deployment that can never produce a caption. Asserting the OUTCOME
    here would have been a second test for a property the None contract already owns; asserting
    the work not done is the claim that is actually this line's.
    """
    calls = []
    monkeypatch.setattr(media, "asr_available", lambda: False)
    monkeypatch.setattr(media, "extract_audio",
                        lambda *a, **kw: calls.append("decode"))
    clip = _clip(tmp_path / "townhall.mp4")
    assert prop.propose_captions(clip, ".mp4") == []
    assert calls == [], "audio was decoded for a file no transcriber could read"


@needs_media
def test_no_proposal_for_a_silent_video(tmp_path):
    """Nothing to transcribe. A cue file with no cues is not a caption track, and offering one
    would invite a reviewer to approve captions for a video that has no audio at all."""
    silent = _clip(tmp_path / "screen.mp4", audio=False)
    assert prop.propose_captions(silent, ".mp4") == []


@needs_media
def test_a_long_recording_is_not_transcribed_during_remediation(tmp_path, monkeypatch):
    """The operational bound. Whisper is roughly real-time on CPU, so a 90-minute all-hands would
    hold a remediation worker for tens of minutes — the shape of stall the queue work has been
    fixing all week, arriving from the feature side.

    Over the cap the proposer declines rather than starting: no proposal, and the finding routes
    to a human as it did before. The duration is forced through `probe` so the test needs no long
    fixture.

    THE ASR IS STUBBED HERE ON PURPOSE, and the first version of this test was worthless without
    it. With the real engine on a 440 Hz tone the transcription finds no speech, so the function
    declines for THAT reason whether the cap exists or not — a bite check deleting the cap left
    the test green. Stubbing a transcript that WOULD produce a card makes the cap the only thing
    standing between this file and a proposal, which is what the test claims to be about.
    """
    _stub_asr(monkeypatch)
    clip = _clip(tmp_path / "allhands.mp4")
    assert prop.propose_captions(clip, ".mp4"), (
        "the control failed: with the ASR stubbed this clip must yield a card, or the cap "
        "assertion below proves nothing")

    real_probe = media.probe

    def _long(path):
        info = real_probe(path)
        return None if info is None else media.MediaInfo(
            duration=prop.CAPTION_MAX_SECONDS + 1, has_audio=info.has_audio,
            has_video=info.has_video, has_captions=info.has_captions)

    monkeypatch.setattr(media, "probe", _long)
    assert prop.propose_captions(clip, ".mp4") == []


def test_a_media_file_that_cannot_be_probed_yields_no_proposal(tmp_path):
    assert prop.propose_captions(tmp_path / "absent.mp4", ".mp4") == []
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"PK\x03\x04 not a video")
    assert prop.propose_captions(junk, ".mp4") == []


def test_the_proposer_never_raises_on_a_non_media_extension(tmp_path):
    doc = tmp_path / "report.docx"
    doc.write_bytes(b"PK\x03\x04")
    assert prop.propose_captions(doc, ".docx") == []


# ── the handler branch: the wiring, not just the proposer ───────────────────────────────────
# WITHOUT THESE THE BRANCH IS AN ORPHAN, which is the failure this repo has already paid for
# three times (tests/test_orphaned_detectors.py: three registered detectors nothing invoked, read
# as capability for months). `propose_captions` being correct says nothing about whether
# `_remediate_file` ever calls it, and the tests above only reach the proposer directly.


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Stub the two things a handler cannot have in a unit test — Drive and the store — and
    capture what reaches the queue. Everything between them is the real code path."""
    import handlers

    state = {"proposals": [], "decisions": [], "bytes": b""}

    class _Media:
        def execute(self):
            return state["bytes"]

    class _Files:
        def get_media(self, fileId=None):
            return _Media()

    class _Svc:
        def files(self):
            return _Files()

    monkeypatch.setattr(handlers, "_drive_client", lambda token: _Svc())
    monkeypatch.setattr(handlers, "_remediation_scope", lambda *a, **kw: None)
    monkeypatch.setattr(handlers.core, "store", type("S", (), {
        # The handler now reads assessment scope and source provenance before drafting.
        # No recorded checksum means unknown provenance, not a fabricated source match.
        "get_source_checksum": staticmethod(lambda *a, **kw: None),
        "get_scan_scope": staticmethod(lambda *a, **kw: None),
        "scope_for_file": staticmethod(lambda sid, filename, scope: scope),
        "enqueue_proposals": staticmethod(
            lambda scan_id, file, sc, proposals, **kw: state["proposals"].append(
                {"sc": sc, "file": file, "proposals": proposals, **kw}) or "p1"),
        "log_decision": staticmethod(
            lambda actor, action, **kw: state["decisions"].append({"action": action, **kw})),
    })())
    monkeypatch.setattr(handlers.core, "get_scan_tokens", lambda sid: {"drive": "tok"})
    return state


@needs_media
def test_the_handler_branch_enqueues_the_caption_card(wired, tmp_path, monkeypatch):
    """End to end through `_propose_media_captions`: download → draft → queue."""
    import handlers
    _stub_asr(monkeypatch)
    clip = _clip(tmp_path / "townhall.mp4")
    wired["bytes"] = clip.read_bytes()

    handlers._propose_media_captions("s1", "townhall.mp4", "drive-1", {"drive_token": "tok"})

    assert len(wired["proposals"]) == 1, f"no card reached the queue: {wired}"
    row = wired["proposals"][0]
    assert row["sc"] == "1.2.2"
    assert row["validated"] is False, (
        "a transcription cannot be validated by a re-scan — the caption file is not in the "
        "document, so a cleared re-scan would prove only that the finding stopped firing")
    assert row["proposals"][0]["proposed_value"].startswith("WEBVTT")


@needs_media
def test_a_file_with_no_draft_records_why_and_enqueues_nothing(wired, tmp_path, monkeypatch):
    """A card-less finding must be explicable. Without the recorded reason a reviewer looking at
    a 1.2.2 row with no draft cannot tell "the recording is too long" from "this deployment has
    no transcriber" — and would reasonably read it as the feature being broken."""
    import handlers
    monkeypatch.setattr(media, "asr_available", lambda: False)
    clip = _clip(tmp_path / "townhall.mp4")
    wired["bytes"] = clip.read_bytes()

    handlers._propose_media_captions("s1", "townhall.mp4", "drive-1", {"drive_token": "tok"})

    assert wired["proposals"] == [], "a draft was enqueued with no transcriber available"
    deferred = [d for d in wired["decisions"] if d["action"] == "remediate.deferred"]
    assert len(deferred) == 1 and "media" in deferred[0]["detail"], wired["decisions"]


def test_media_never_reaches_the_document_rewriters(wired, tmp_path, monkeypatch):
    """The early return, asserted by what does NOT happen. Everything below the media branch in
    `_remediate_file` downloads, rewrites bytes and uploads a fixed copy. A .mp4 that fell
    through to it would be handed to an OOXML or PDF remediator."""
    import handlers
    calls = []
    for name in ("_propose_text_findings", "_propose_form_fields"):
        if hasattr(handlers, name):
            monkeypatch.setattr(handlers, name,
                                lambda *a, _n=name, **kw: calls.append(_n))
    monkeypatch.setattr(handlers, "_propose_media_captions",
                        lambda *a, **kw: calls.append("media"))
    monkeypatch.setattr(handlers.core.store, "is_shadowed_output", lambda *a, **kw: False,
                        raising=False)

    handlers._remediate_file({"scan_id": "s1", "file": "townhall.mp4",
                              "drive_file_id": "d1", "drive_token": "tok"}, {})
    assert calls == ["media"], (
        f"a .mp4 reached a document remediation path: {calls}")


# ── what this slice still does not claim ────────────────────────────────────────────────────
def test_captions_are_still_human_only_and_never_auto_applied():
    """Drafting is not fixing. The card is Medium/Low confidence by construction: proposals reach
    the queue with `validated=False`, and nothing in the media path calls an applier."""
    import assessment_policy as ap
    by_id = {c["id"]: c for c in ap.RULE_CATALOG}
    for sc in ("1.2.1", "1.2.2", "1.2.3"):
        assert by_id[sc]["fix_mode"] == "human-only", (
            f"{sc} became {by_id[sc]['fix_mode']}. An ASR draft is a machine guess a person "
            f"confirms — a wrong caption is worse than an absent one, because it looks served")


def test_the_media_file_itself_is_never_rewritten():
    """No applier, no mux, no re-encode. The deliverable is a companion file; touching the
    customer's video would re-author it (ADR 0016) and is not what an approval authorises."""
    import ast
    src = (ROOT / "api" / "proposals.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "propose_captions")
    called = {n.func.attr for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "write_bytes" not in called and "replace" not in called, (
        "the caption proposer writes to disk; a proposal computes a value and returns it")
