"""What ACP actually checks on an HTML file, per WCAG success criterion — read off the current code.

Where the checks live (HTML), for a persisted scan:
  * api/scanner.py `_analyse_html` — every HTML_* rule, one lxml parse, inline-style driven.
  * api/formats/html/detectors/language_of_page.py — the registry's 3.1.1 detector (FULL coverage).
  * api/textchecks.py — sensory wording, language of parts and reading level on the page text.
Fixes: api/remediate.py (`FIXERS`, plus the 2.4.4 link-text proposer).

The browser-side rule modules (frontend/src/rules/*.js) back the live preview and are a separate
engine; this catalog describes what a server-side scan records.

A theme runs through the HTML checks and is worth knowing before reading any one of them: they
read INLINE style attributes (and, for a few, <style> blocks). Rules in external stylesheets are
never fetched, and no page is rendered, so anything that depends on computed style is invisible.

Numbers are derived, never typed — see `threshold()` in pdf.py (shared helpers).
"""
from __future__ import annotations

from .pdf import count, pct, phrases, px, threshold
from .schema import FormatExplanation, RuleRef

_SC = "api/scanner.py"
_TXT = "api/textchecks.py"


def _luma(v) -> str:
    return f"luma above {float(v):g} (0 = black, 1 = white)"


_INLINE_ONLY = ("Colours set in <style> blocks or external stylesheets, rgb()/hsl()/named colours, "
                "and inherited colours — only an inline style=\"color:#hex\" is read.")


def html() -> dict[str, FormatExplanation]:
    """One FormatExplanation per SC in remediation_capability.remediation_table()["html"]."""
    import remediate

    abbr_n = len(remediate.ABBR)

    out: dict[str, FormatExplanation] = {}

    out["1.1.1"] = FormatExplanation(
        status="partial",
        checks=(
            "Every <img> must have an alt attribute, unless it is marked role=\"presentation\" "
            "or role=\"none\". An empty alt=\"\" counts as present (decorative).",
        ),
        does_not_check=(
            "Whether alt text describes the image, or whether alt=\"\" is wrongly used on a "
            "meaningful image.",
            "CSS background images, inline <svg>, <input type=\"image\">, <area>, <canvas>, "
            "<object> and elements with role=\"img\".",
            "The image itself — external image files are never fetched.",
        ),
        evidence="One finding per <img> without alt; no element location is recorded.",
        fix="Review. The image is referenced, not embedded, so no proposer can draft alt text.",
        rules=(RuleRef("HTML_IMG_MISSING_ALT", _SC, "deterministic", "review"),),
    )

    out["1.2.1"] = FormatExplanation(
        status="partial",
        checks=(
            "Every <audio> must point at a transcript: an aria-describedby, or an aria-label "
            "containing the word “transcript”.",
        ),
        does_not_check=(
            "Video-only content (a <video> with no audio track).",
            "A transcript linked or placed next to the player without those attributes.",
            "Whether the transcript is complete or accurate.",
            "Media embedded through <iframe> players (YouTube, Vimeo, …).",
        ),
        evidence="One finding per <audio> element; no location is recorded.",
        fix="Review. Writing a transcript is authoring work.",
        rules=(RuleRef("HTML_AUDIO_NO_TRANSCRIPT", _SC, "deterministic", "review"),),
    )

    out["1.2.2"] = FormatExplanation(
        status="partial",
        checks=("Every <video> must contain a <track kind=\"captions\"> or kind=\"subtitles\".",),
        does_not_check=(
            "Captions burned into the video picture (reported as missing).",
            "Whether the track file exists or is accurate and synchronised.",
            "Media embedded through <iframe> players.",
        ),
        evidence="One finding per <video> element; no location is recorded.",
        fix="Review. Captioning is authoring work.",
        rules=(RuleRef("HTML_VIDEO_NO_CAPTIONS", _SC, "deterministic", "review"),),
    )

    out["1.2.3"] = FormatExplanation(
        status="partial",
        checks=("Every <video> must have a <track kind=\"descriptions\"> or an aria-describedby "
                "pointing at a text alternative.",),
        does_not_check=(
            "Audio description mixed into the main soundtrack.",
            "Whether the referenced alternative is a full media alternative.",
            "Media embedded through <iframe> players.",
        ),
        evidence="One finding per <video> element; no location is recorded.",
        fix="Review. Audio description is authoring work.",
        rules=(RuleRef("HTML_VIDEO_NO_DESCRIPTION", _SC, "deterministic", "review"),),
    )

    out["1.3.1"] = FormatExplanation(
        status="partial",
        checks=(
            "Every <input>, <select> and <textarea> (except hidden inputs and inputs that name "
            "themselves: button, submit, reset, image) has a name from aria-label, "
            "aria-labelledby, a wrapping <label>, or a <label for> that matches its id.",
            "Text styled as a heading but left as body text: a <p> or <div> with no child "
            "elements, whose INLINE style is both large and bold, holding short text that does "
            "not end like a sentence, and that is not page furniture (a wordmark, a headline "
            "figure, a pull quote).",
        ),
        thresholds=(
            threshold("Pseudo-heading: inline font size at least",
                      "scanner:_PSEUDO_HEADING_MIN_PX", px),
            threshold("Pseudo-heading: longest text", "scanner:_PSEUDO_HEADING_MAX_CHARS",
                      count("characters")),
        ),
        does_not_check=(
            "Tables (headers, captions), lists, fieldsets/legends, landmarks and ARIA roles.",
            "Headings styled through classes or stylesheets — only inline style is read.",
            "Whether an existing label is meaningful.",
        ),
        evidence="HTML_FORM_CONTROL_NO_NAME: one per control, no location. HTML_PSEUDO_HEADING: "
                 "one per element, quoting its text.",
        fix="Auto. Unlabelled controls get an aria-label from their placeholder, then name, then "
            "“Field”. Pseudo-headings are retagged as <h2>, keeping their inline style so the "
            "page looks the same. The labelling pass also clears 3.3.2 and 4.1.2.",
        rules=(
            RuleRef("HTML_FORM_CONTROL_NO_NAME", _SC, "deterministic", "auto"),
            RuleRef("HTML_PSEUDO_HEADING", _SC, "heuristic", "auto"),
        ),
    )

    out["1.3.2"] = FormatExplanation(
        status="partial",
        checks=(
            "Flags CSS (inline styles and <style> blocks together) that can make the visual order "
            "differ from source order: a non-zero flex/grid `order`, or a reversed flex direction.",
        ),
        does_not_check=(
            "Absolute/fixed positioning, floats, grid placement, or DOM order itself.",
            "External stylesheets.",
            "Known false positive: the text match also fires inside other property names, so "
            "`border: 1px …` (which contains “order: 1”) is reported. Verified on a one-line page.",
        ),
        evidence="One finding per document; which element or rule triggered it is not recorded.",
        fix="Review. Fixing the order means editing layout or source order.",
        rules=(RuleRef("HTML_VISUAL_REORDER", _SC, "heuristic", "review"),),
    )

    out["1.3.3"] = FormatExplanation(
        status="partial",
        checks=(
            "Searches the page's text for an instruction verb (click, select, see, …) followed "
            "closely in the same sentence by a shape word or a position phrase (on the left, "
            "top-right, below, …).",
        ),
        thresholds=(
            threshold("Maximum findings per document", "textchecks:_SENSORY_MAX",
                      count("sentences")),
            threshold("Longest gap between the verb and the shape/position word",
                      "textchecks:_SENSORY_RE", lambda rx: count("characters")(
                          rx.pattern.split("{0,", 1)[1].split("}", 1)[0])),
        ),
        does_not_check=(
            "Colour, size or sound used as the only cue.",
            "Whether a non-sensory cue is also given; every match goes to a person.",
            "Script and <style> text is not excluded — it is part of the page text searched.",
        ),
        evidence="One finding per matching sentence (deduplicated), quoting the sentence.",
        fix="Assisted. A local text model drafts a rewrite that a person approves.",
        rules=(RuleRef("SENSORY_INSTRUCTION", _TXT, "heuristic", "assisted"),),
    )

    out["1.3.4"] = FormatExplanation(
        status="partial",
        checks=("A <style> block containing an @media (orientation: portrait|landscape) rule "
                "that sets display:none.",),
        does_not_check=(
            "Orientation locks done in JavaScript, with transforms, or in external stylesheets.",
            "Other ways of hiding content in one orientation (visibility, height 0, …).",
        ),
        evidence="One finding per matching <style> block; no location is recorded.",
        fix="Auto. The display:none inside the orientation rule becomes display:revert.",
        rules=(RuleRef("HTML_ORIENTATION_LOCK", _SC, "heuristic", "auto"),),
    )

    out["1.3.5"] = FormatExplanation(
        status="partial",
        checks=(
            "An <input> with no autocomplete attribute is flagged when its type is email or tel, "
            "or its name, id or placeholder uses personal-data wording (email, phone, given/"
            "family name, postal code, country, city, street/address, organisation/company, "
            "birth date).",
        ),
        does_not_check=(
            "<select> and <textarea>.",
            "Whether an existing autocomplete value is a valid, correct token.",
            "Whose data the field collects: organisation/company wording is flagged here, while "
            "the PDF check deliberately excludes it as not about the user.",
        ),
        evidence="One finding per input; no location is recorded.",
        fix="Auto. The matching autocomplete token (email, tel, given-name, postal-code, …) is "
            "written on the input.",
        rules=(RuleRef("HTML_INPUT_NO_AUTOCOMPLETE", _SC, "heuristic", "auto"),),
    )

    out["1.4.1"] = FormatExplanation(
        status="partial",
        checks=("An <a> whose inline style sets a colour and does not set an underline "
                "(text-decoration: underline).",),
        does_not_check=(
            "Whether the link colour actually differs from the surrounding text, or meets the "
            "3:1 difference that would make colour alone acceptable.",
            "Links styled from stylesheets, and colour used for meaning anywhere other than links.",
        ),
        evidence="One finding per link; no location is recorded.",
        fix="Auto. text-decoration:underline is added to the link's inline style.",
        rules=(RuleRef("HTML_LINK_COLOR_ONLY", _SC, "heuristic", "auto"),),
    )

    out["1.4.2"] = FormatExplanation(
        status="partial",
        checks=("An <audio> or <video> with autoplay and without controls.",),
        does_not_check=(
            "Muted media, or sound shorter than 3 seconds (both allowed by 1.4.2) — reported "
            "anyway.",
            "Playback started from JavaScript, or media in <iframe> players.",
        ),
        evidence="One finding per media element; no location is recorded.",
        fix="Auto. The autoplay attribute is removed.",
        rules=(RuleRef("HTML_AUTOPLAY_MEDIA", _SC, "deterministic", "auto"),),
    )

    out["1.4.3"] = FormatExplanation(
        status="partial",
        checks=(
            "An element whose inline style sets a hex text colour that is light — judged by the "
            "colour's brightness alone, assuming a light page behind it. This is not a WCAG "
            "contrast ratio.",
        ),
        thresholds=(
            threshold("Text colour flagged when", "scanner:_analyse_html", _luma, literal="0.62"),
            threshold("Fix: text colours darkened when", "remediate:_fix_contrast", _luma,
                      literal="0.45"),
            threshold("Fix: skipped when the inline background is darker than",
                      "remediate:_fix_contrast", lambda v: f"luma {float(v):g}", literal="0.5"),
        ),
        does_not_check=(
            "The actual background colour — a light colour on a dark background is reported, and "
            "a dark colour on a dark background is not.",
            "Text size (large text's lower bar is not applied).",
            _INLINE_ONLY,
        ),
        evidence="One finding per element; no location or measured ratio is recorded.",
        fix="Auto. The inline colour is replaced with #111111 unless the element's own inline "
            "background is dark; one pass also clears 1.4.6.",
        rules=(RuleRef("HTML_LOW_CONTRAST_AA", _SC, "heuristic", "auto"),),
    )

    out["1.4.4"] = FormatExplanation(
        status="partial",
        checks=("A viewport <meta> that disables zoom: user-scalable=no (or 0), or a "
                "maximum-scale of 0 or 1.",),
        does_not_check=(
            "A maximum-scale above 1 but below 2, which still blocks 200% zoom.",
            "Text sized in fixed units, or content that clips or overlaps at 200%.",
        ),
        evidence="One finding per viewport tag; no location is recorded.",
        fix="Auto. The viewport content is reset to width=device-width, initial-scale=1.",
        rules=(RuleRef("HTML_VIEWPORT_BLOCKS_ZOOM", _SC, "deterministic", "auto"),),
    )

    out["1.4.5"] = FormatExplanation(
        status="partial",
        checks=(
            "An <img> whose file name contains a text-signalling word (heading, banner, title, "
            "quote, slogan, text, …).",
            "A CSS image-replacement trick: a very large negative text-indent.",
        ),
        does_not_check=(
            "The image's pixels — HTML images are not OCR'd (they are referenced, not embedded).",
            "Images of text with ordinary file names, or text in SVG/canvas.",
        ),
        evidence="One finding per document; which image triggered it is not recorded.",
        fix="Review. Replacing an image of text with real text is an authoring edit.",
        rules=(RuleRef("HTML_IMAGE_OF_TEXT", _SC, "heuristic", "review"),),
    )

    out["1.4.6"] = FormatExplanation(
        status="partial",
        checks=("The same brightness test as 1.4.3 with a lower cut-off, so moderately light "
                "inline text colours are also reported.",),
        thresholds=(
            threshold("Text colour flagged when", "scanner:_analyse_html", _luma, literal="0.45"),
        ),
        does_not_check=(
            "The actual background colour, text size, or a real contrast ratio.",
            _INLINE_ONLY,
        ),
        evidence="One finding per element; no location is recorded.",
        fix="Auto, via the 1.4.3 darkening pass.",
        rules=(RuleRef("HTML_LOW_CONTRAST_AAA", _SC, "heuristic", "auto"),),
    )

    out["1.4.10"] = FormatExplanation(
        status="partial",
        checks=("A page with head content (any <meta>, <link> or <style>) but no viewport "
                "<meta>.",),
        does_not_check=(
            "Fixed-width layouts, wide tables or images, or horizontal scrolling at 320 CSS "
            "pixels — nothing is rendered.",
        ),
        evidence="One finding per document.",
        fix="Auto. A width=device-width, initial-scale=1 viewport is added.",
        rules=(RuleRef("HTML_NO_VIEWPORT_REFLOW", _SC, "heuristic", "auto"),),
    )

    out["1.4.11"] = FormatExplanation(
        status="partial",
        checks=("An element whose inline border sets a light hex colour — the same brightness "
                "test as 1.4.3, not a contrast ratio against the adjacent colour.",),
        thresholds=(
            threshold("Border colour flagged when", "scanner:_analyse_html", _luma,
                      literal="0.62"),
        ),
        does_not_check=(
            "Icons, SVG graphics, focus indicators and form-control states.",
            "The colour next to the border.",
            _INLINE_ONLY,
        ),
        evidence="One finding per element; no location is recorded.",
        fix="Review. No automatic recolouring of borders.",
        rules=(RuleRef("HTML_BORDER_LOW_CONTRAST", _SC, "heuristic", "review"),),
    )

    out["1.4.12"] = FormatExplanation(
        status="partial",
        checks=("An inline line-height set in pixels, which cannot grow when a reader "
                "increases text spacing.",),
        does_not_check=(
            "Letter, word or paragraph spacing, fixed-height boxes with overflow hidden, and "
            "stylesheets.",
        ),
        evidence="One finding per element; no location is recorded.",
        fix="Auto. The pixel line-height becomes line-height:1.5.",
        rules=(RuleRef("HTML_FIXED_LINE_HEIGHT", _SC, "heuristic", "auto"),),
    )

    out["2.4.1"] = FormatExplanation(
        status="partial",
        checks=(
            "When the page has repeated chrome (<nav>, <header>, role=navigation or banner), it "
            "must have a <main>/role=main or an in-page link to an existing id.",
        ),
        does_not_check=(
            "Whether the in-page link actually skips the chrome — any same-page link to an "
            "existing id counts.",
            "Headings as a bypass mechanism.",
        ),
        evidence="One finding per document.",
        fix="Auto. The content after the chrome is wrapped in <main id=\"main-content\"> and a "
            "“Skip to main content” link is added at the top.",
        rules=(RuleRef("HTML_NO_SKIP_LINK", _SC, "deterministic", "auto"),),
    )

    out["2.4.2"] = FormatExplanation(
        status="partial",
        checks=("The page has a <title> with non-blank text.",),
        thresholds=(
            threshold("Fix: longest generated title", "remediate:_fix_title",
                      count("characters"), literal="80"),
        ),
        does_not_check=("Whether the title describes the page.",),
        evidence="One finding per document.",
        fix="Auto. The title is taken from the first <h1>, or “Document” when there is none.",
        rules=(RuleRef("HTML_MISSING_TITLE", _SC, "deterministic", "auto"),),
    )

    out["2.4.3"] = FormatExplanation(
        status="partial",
        checks=("Any element with a positive tabindex, which overrides the natural focus order.",),
        does_not_check=(
            "Focus order created by DOM order, CSS positioning or scripts; dialogs and focus "
            "management.",
        ),
        evidence="One finding per element; no location is recorded.",
        fix="Auto. Positive tabindex values are reset to 0.",
        rules=(RuleRef("HTML_POSITIVE_TABINDEX", _SC, "deterministic", "auto"),),
    )

    out["2.4.4"] = FormatExplanation(
        status="partial",
        checks=(
            "An <a> with no text and no aria-label/title.",
            "An <a> whose whole text is one of a short list of generic phrases, with no "
            "aria-label.",
        ),
        thresholds=(
            threshold("Generic link phrases", "scanner:_VAGUE_LINK_TEXT", phrases),
        ),
        does_not_check=(
            "Image links: an <a> whose only content is an <img alt=\"…\"> is reported as empty, "
            "because the image's alt is not read as the link's text.",
            "Whether descriptive text names the right destination, or surrounding context.",
            "The PDF/Office checks use a longer phrase list than this one.",
        ),
        evidence="One finding per link; no location is recorded.",
        fix="Assisted. When the link target yields a descriptive name (e.g. a file name) ACP "
            "applies it and records it for one-click confirmation; otherwise, with AI on, a text "
            "model drafts link text for a person to approve.",
        rules=(
            RuleRef("HTML_EMPTY_LINK", _SC, "deterministic", "assisted"),
            RuleRef("HTML_VAGUE_LINK", _SC, "deterministic", "assisted"),
        ),
    )

    out["2.4.6"] = FormatExplanation(
        status="partial",
        checks=("Heading levels step down by at most one (h1 → h3 is a skip). Every gap is "
                "reported.",),
        does_not_check=(
            "Whether headings describe their sections — the core of 2.4.6.",
            "The first heading's level (a page may start at h3 unreported).",
            "Form labels, the other half of 2.4.6.",
        ),
        evidence="One finding per skip, naming the heading's position and the levels involved.",
        fix="Auto. Each skipped level is renumbered to one below the previous heading.",
        rules=(RuleRef("HTML_HEADING_SKIP", _SC, "deterministic", "auto"),),
    )

    out["2.4.7"] = FormatExplanation(
        status="partial",
        checks=("A page with interactive elements where any <style> block or inline style "
                "sets outline:none or outline:0.",),
        does_not_check=(
            "Whether a replacement focus style (e.g. :focus-visible, box-shadow) is provided — "
            "the page is reported anyway.",
            "Focus indicators that are present but too faint.",
        ),
        evidence="One finding per document.",
        fix="Auto. Each outline suppression becomes outline:revert, restoring the browser's "
            "focus ring.",
        rules=(RuleRef("HTML_FOCUS_OUTLINE_SUPPRESSED", _SC, "heuristic", "auto"),),
    )

    out["2.4.9"] = FormatExplanation(
        status="partial",
        checks=(
            "Links with identical text (or aria-label) that go to different destinations.",
            "Links whose text is a generic phrase (same list as 2.4.4).",
        ),
        does_not_check=("Whether unique link text is meaningful on its own.",),
        evidence="One finding per affected link; no location is recorded.",
        fix="Review.",
        rules=(RuleRef("HTML_LINK_PURPOSE_AMBIGUOUS", _SC, "deterministic", "review"),),
    )

    out["2.5.3"] = FormatExplanation(
        status="partial",
        checks=("An <a> or <button> with an aria-label that does not contain its visible text "
                "(compared ignoring case and extra spaces).",),
        thresholds=(
            threshold("Visible text compared", "scanner:_analyse_html", count("characters"),
                      literal="80"),
        ),
        does_not_check=(
            "Names from aria-labelledby, inputs with <label>, and custom widgets.",
            "Visible text that is an image of text.",
        ),
        evidence="One finding per element; no location is recorded.",
        fix="Auto. The aria-label becomes “<visible text> — <old label>”.",
        rules=(RuleRef("HTML_LABEL_NOT_IN_NAME", _SC, "deterministic", "auto"),),
    )

    out["2.5.8"] = FormatExplanation(
        status="partial",
        checks=("An interactive element (link with href, button, form control, role=button, "
                "onclick) whose inline width or height is set in pixels below the minimum.",),
        thresholds=(
            threshold("Minimum target size", "scanner:_analyse_html", px, literal="24",
                      standard=True),
        ),
        does_not_check=(
            "The rendered size — targets sized by padding, fonts or stylesheets are never measured.",
            "WCAG's exceptions: spacing around small targets, inline links in text, essential "
            "sizes.",
        ),
        evidence="One finding per element; no location is recorded.",
        fix="Review. Target size is a layout decision.",
        rules=(RuleRef("HTML_TARGET_TOO_SMALL", _SC, "deterministic", "review"),),
    )

    out["3.1.1"] = FormatExplanation(
        status="implemented",
        checks=("The root <html> element has a non-blank lang (or xml:lang) attribute.",),
        does_not_check=("Whether the value is a valid language tag or the right language.",),
        evidence="One finding per document.",
        fix="Auto. lang=\"en\" is added — always English; the page's language is not detected.",
        rules=(
            RuleRef("HTML_MISSING_LANG", _SC, "deterministic", "auto"),
            RuleRef("HTML_MISSING_LANG", "api/formats/html/detectors/language_of_page.py",
                    "deterministic", "auto"),
        ),
    )

    out["3.1.2"] = FormatExplanation(
        status="partial",
        checks=(
            "Detects the language of each sentence-sized passage of the page text and, when a "
            "confident second language appears, reports passages in it.",
        ),
        thresholds=(
            threshold("Shortest passage judged", "textchecks:_MIN_SEG_WORDS", count("words")),
            threshold("Detector confidence needed", "textchecks:_MIN_CONF", pct),
            threshold("Passages examined", "textchecks:_MAX_SEGS", count("passages")),
        ),
        does_not_check=(
            "lang attributes on elements: they are not read for HTML, so a passage correctly "
            "marked with lang=\"fr\" is still reported (verified: no marks are collected for "
            ".html).",
            "Anything when the langdetect library is not installed.",
            "Short passages, names and single foreign words.",
        ),
        evidence="One finding per document listing each language and its passage count.",
        fix="Assisted: a language-of-parts proposal is raised for a person to approve; nothing "
            "is marked automatically.",
        rules=(RuleRef("LANG_PARTS_UNMARKED", _TXT, "heuristic", "assisted"),),
    )

    out["3.1.4"] = FormatExplanation(
        status="partial",
        checks=(f"Text containing one of a fixed glossary of {abbr_n} abbreviations "
                "(WCAG, ADA, PDF, FAQ, HR, …) outside an <abbr>.",),
        does_not_check=(
            "Any abbreviation not in the glossary.",
            "Whether an abbreviation is expanded on first use in the text itself.",
        ),
        evidence="One finding per text node containing a glossary term; no location is recorded.",
        fix="Auto. Each occurrence is wrapped in <abbr title=\"…\"> with the glossary expansion.",
        rules=(RuleRef("HTML_UNEXPANDED_ABBR", _SC, "deterministic", "auto"),),
    )

    out["3.1.5"] = FormatExplanation(
        status="partial",
        checks=("Computes the Flesch-Kincaid grade of the page text and flags genuinely dense "
                "prose.",),
        thresholds=(
            threshold("Grade at or above which the page is flagged", "textchecks:_MIN_GRADE",
                      count("(Flesch-Kincaid grade)")),
            threshold("Fewest words scored", "textchecks:_MIN_WORDS_FOR_READING",
                      count("words")),
            threshold("Longer average sentences are treated as unpunctuated and not scored",
                      "textchecks:_MAX_WORDS_PER_SENTENCE", count("words")),
        ),
        does_not_check=(
            "Text between WCAG's lower-secondary level (about grade 9) and the flag grade.",
            "Whether a plain-language version exists.",
        ),
        evidence="One finding per document, with the grade.",
        fix="Assisted. A plain-language rewrite is proposed for a person to approve.",
        rules=(RuleRef("READING_LEVEL_ADVANCED", _TXT, "heuristic", "assisted"),),
    )

    out["3.3.2"] = FormatExplanation(
        status="partial",
        checks=("A required <input>, <select> or <textarea> with no label or instruction at "
                "all: no aria-label/labelledby/describedby, title, placeholder, wrapping "
                "<label> or <label for>.",),
        does_not_check=(
            "Whether instructions explain the expected format.",
            "Fields that are required without the required attribute (e.g. aria-required).",
        ),
        evidence="One finding per field; no location is recorded.",
        fix="Auto, via the 1.3.1 labelling pass (an aria-label from placeholder, name or "
            "“Field”).",
        rules=(RuleRef("HTML_REQUIRED_NO_GUIDANCE", _SC, "deterministic", "auto"),),
    )

    out["4.1.2"] = FormatExplanation(
        status="partial",
        checks=("Every <input> (except hidden, submit, button, image, reset) has aria-label, "
                "aria-labelledby, title, or a <label for> matching its id.",),
        does_not_check=(
            "<select>, <textarea>, buttons, and custom widgets' roles, states and values.",
            "Known false positive: an input wrapped in its <label> (an implicit label) is "
            "reported even though it has a name — verified. The 1.3.1 check credits it.",
        ),
        evidence="One finding per input; no location is recorded.",
        fix="Auto, via the 1.3.1 labelling pass. That pass also does not recognise a wrapping "
            "<label>, so when it runs it can give an implicitly labelled input an aria-label "
            "(e.g. its name attribute) that overrides the real label.",
        rules=(RuleRef("HTML_INPUT_NO_LABEL", _SC, "deterministic", "auto"),),
    )

    return out
