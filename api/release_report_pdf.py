"""Printable, branded follow-up reports from the same audited report evidence."""
from pathlib import Path

from lxml import html, etree

_FONTS = Path(__file__).resolve().parent / 'assets' / 'fonts'
_FONT_REGULAR = (_FONTS / 'DejaVuSans.ttf').as_uri()
_FONT_BOLD = (_FONTS / 'DejaVuSans-Bold.ttf').as_uri()
_FONT_URLS = frozenset({_FONT_REGULAR, _FONT_BOLD})

# DejaVu is bundled and declared here so the ☐ follow-up boxes and arrows have a glyph on every
# host, and so the page furniture (margin boxes do not inherit from body) is not set in a serif.
PRINT_CSS = '''
@font-face { font-family: "DejaVu Sans"; src: url("@@FONT_REGULAR@@"); font-weight: 400; }
@font-face { font-family: "DejaVu Sans"; src: url("@@FONT_BOLD@@"); font-weight: 700; }
@page { size: A4; margin: 18mm 16mm 20mm;
  font-family: "DejaVu Sans", sans-serif;
  @bottom-left { content: "Mova iO | Accessibility follow-up"; font-size: 8pt; color: #625469; }
  @bottom-right { content: "Page " counter(page) " of " counter(pages); font-size: 8pt; color: #625469; }
}
body { font: 10pt/1.5 "DejaVu Sans", sans-serif; margin: 0; padding: 0; background: white; color: #302535; }
main { border: 0; padding: 0; }
.brand { border-bottom: 3px solid #62435d; padding-bottom: 12px; display: flex; align-items: center; gap: 20px; }
.brand img { width: 155px; max-width: 155px; order: 2; margin-left: auto; }
.brand > div { order: 1; }
.print-checkbox { display: inline-block; width: 11px; height: 11px; border: 1px solid #302535; margin-right: 7px; }
.check-off { margin: 6px 0 0; font-size: 9pt; border-top: 1px solid #e4dcea; padding-top: 4px; break-inside: avoid; }
.check-off p { margin: 0 0 3px; }
.notes { border-bottom: 1px solid #bbb; min-height: 20px; margin-top: 4px; font-size: 9pt; }
.print-box { font-size: 11pt; }
.print-notice { font-size: 7.5pt; color: #625469; }
.finding-card h3 { break-after: avoid; }
.finding-card p { margin: 2px 0; }
.finding-card ol { margin: 2px 0 4px; padding-left: 20px; }
.finding-card .facts { font-size: 8.5pt; }
.document-appendix { break-before: page; }
h1 { font-size: 22px; line-height: 1.25; overflow-wrap: anywhere; }
h2 { font-size: 16px; margin-top: 20px; break-after: avoid; }
h3 { font-size: 12px; margin: 0 0 8px; overflow-wrap: anywhere; }
p { overflow-wrap: anywhere; }
.notice { border-left: 4px solid #62435d; background: #f5eff7; padding: 10px; }
table { width: 100%; border-collapse: collapse; font-size: 9pt; }
th, td { border: 1px solid #d9cfdf; padding: 7px; overflow-wrap: anywhere; }
th { background: #f5eff7; }
tr { break-inside: avoid; }
.report-card { border: 1px solid #d9cfdf; border-left: 4px solid #86618b; border-radius: 7px; padding: 9px 11px; margin: 8px 0; break-inside: avoid; }
/* A finding card can be long (steps, recommended values): let it flow rather than leave a
   mostly-empty page ahead of it. Its heading and follow-up box still stay whole. */
.report-card.finding-card { break-inside: auto; padding: 7px 10px; }
.report-field { margin: 3px 0; overflow-wrap: anywhere; }
.report-field b { color: #62435d; }
.report-field.evidence { font-family: "DejaVu Sans Mono", monospace; font-size: 9pt; white-space: pre-wrap; }
details { display: block; border: 1px solid #ded5e4; padding: 8px; margin: 7px 0; break-inside: auto; }
summary { display: block; font-weight: bold; break-after: avoid; }
a { color: #573352; }
.pdf-evidence-pair { break-inside: avoid; margin-top: 16px; }
.pdf-evidence-images { display: flex; gap: 12px; }
.pdf-evidence-images figure { width: 48%; margin: 0; }
.pdf-evidence-images img { width: 100%; height: 95mm; object-fit: contain; border: 1px solid #d9cfdf; }
.pdf-evidence-images figcaption { font-weight: bold; margin-bottom: 6px; }
.pdf-evidence-crops img { height: 32mm; }
'''


def render_report_pdf(source):
    from weasyprint import HTML, CSS, default_url_fetcher
    document = html.fromstring(source)
    for style in document.xpath('//style'):
        style.getparent().remove(style)
    # Wide tables become readable, labeled cards. No evidence column is dropped.
    for table in document.xpath('//table'):
        headers = [h.text_content() for h in table.xpath('./thead/tr/th')]
        if (len(headers) <= 3 and 'Before' not in headers) or 'Success criterion' in headers:
            continue
        cards = etree.Element('section')
        for row in table.xpath('./tbody/tr'):
            card = etree.SubElement(cards, 'section', {'class': 'report-card'})
            if 'Issue' in headers:
                check = etree.SubElement(card, 'div', {'class': 'check-off'})
                box = etree.SubElement(check, 'span', {'class': 'print-checkbox', 'aria-hidden': 'true'})
                box.tail = 'Remediated and rechecked'
            for index, cell in enumerate(row.xpath('./td')):
                field = etree.SubElement(card, 'h3' if index == 0 else 'div', {'class': 'report-field' + (' evidence' if headers[index] in ('Before', 'After', 'Issue', 'Recommended action') else '')})
                label = etree.SubElement(field, 'b')
                label.text = headers[index] + ': '
                label.tail = cell.text
                if headers[index] == 'Criterion' and cell.text:
                    from wcag_codeset import _name_for
                    title = _name_for(cell.text)
                    if title != cell.text:
                        label.tail += ' - ' + title
                for child in list(cell):
                    field.append(child)
            if 'Issue' in headers:
                note = etree.SubElement(card, 'div', {'class': 'notes'})
                note.text = 'Reviewer / date / notes: '
        table.getparent().replace(table, cards)
    for detail in document.xpath('//details'):
        detail.set('open', 'open')
    # Relative HTML report links do not resolve inside standalone PDFs.
    for anchor in document.xpath('//a'):
        href = anchor.get('href', '')
        internal = href.startswith('#') and bool(document.xpath('//*[@id=$target]', target=href[1:]))
        if not href.startswith('https://') and not internal:
            anchor.text = 'Checklist included in this report'
            anchor.drop_tag()
    def fetch(url, *args, **kwargs):
        if not (url.startswith('data:image/png;base64,') or url in _FONT_URLS):
            raise ValueError('Only embedded PNG images and bundled fonts may be loaded')
        return default_url_fetcher(url, *args, **kwargs)
    markup = html.tostring(document, encoding='unicode').replace('Expand a category to see SCs by file.', 'Detailed SC checklists follow for each file.')
    return HTML(string=markup, url_fetcher=fetch).write_pdf(
        stylesheets=[CSS(string=PRINT_CSS.replace('@@FONT_REGULAR@@', _FONT_REGULAR)
                         .replace('@@FONT_BOLD@@', _FONT_BOLD), url_fetcher=fetch)],
        pdf_variant='pdf/ua-1')
