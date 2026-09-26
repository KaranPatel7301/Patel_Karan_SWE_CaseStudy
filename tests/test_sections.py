from app.ingest.filings import (
    chunk_text,
    extract_sections,
    html_to_text,
)


def _body(title: str, words: int) -> str:
    return f"{title}\n\n" + ("This is a real sentence about the business. " * words)


def _document() -> str:
    toc = "\n".join(
        [
            "Item 1A. Risk Factors 12",
            "Item 1B. Unresolved Staff Comments 14",
            "Item 2. Properties 15",
            "Item 7. Management's Discussion and Analysis 40",
            "Item 7A. Quantitative and Qualitative Disclosures 80",
            "Item 8. Financial Statements 82",
        ]
    )
    risk = _body("Item 1A. Risk Factors", 400)
    after_risk = "\n\nItem 1B. Unresolved Staff Comments\n\nNone.\n\nItem 2. Properties\n\nOffices.\n\n"
    mdna = _body("Item 7. Management's Discussion and Analysis of Financial Condition", 400)
    after_mdna = "\n\nItem 7A. Quantitative and Qualitative Disclosures About Market Risk\n\nInterest rates.\n\n"
    return f"{toc}\n\n{risk}{after_risk}{mdna}{after_mdna}Item 8. Financial Statements\n\nStatements.\n"


def test_html_to_text_drops_hidden_xbrl_and_display_none() -> None:
    html = """
    <html><body>
      <ix:header><ix:hidden>SECRET_HEADER</ix:hidden></ix:header>
      <p style="display: none">HIDDEN_NOTE</p>
      <p>Visible paragraph</p>
    </body></html>
    """
    text = html_to_text(html)
    assert "SECRET_HEADER" not in text
    assert "HIDDEN_NOTE" not in text
    assert "Visible paragraph" in text


def test_extractor_keeps_the_longest_span_not_the_table_of_contents() -> None:
    sections = {section.item: section for section in extract_sections(_document())}
    assert sections["risk_factors"].extraction_method == "regex"
    assert sections["mdna"].extraction_method == "regex"
    assert sections["risk_factors"].char_count >= 5_000
    assert "This is a real sentence about the business." in sections["risk_factors"].text
    assert "Item 1A. Risk Factors 12" not in sections["risk_factors"].text
    assert "full_text" not in sections


def test_short_section_falls_back_to_full_text() -> None:
    text = "Item 1A. Risk Factors\n\nShort.\n\nItem 1B. Unresolved\n\n" + (
        "Item 7. Management's Discussion and Analysis\n\n" + ("Revenue grew. " * 400) + "\n\nItem 7A. Market risk\n"
    )
    sections = extract_sections(text)
    by_item = {section.item: section for section in sections}
    assert by_item["mdna"].extraction_method == "regex"
    assert by_item["full_text"].extraction_method == "fallback"
    assert "risk_factors" not in by_item
    assert by_item["full_text"].text == text


def test_short_item_7_uses_the_narrative_heading() -> None:
    toc = "\n".join(
        [
            "Item 7.",
            "",
            "Management's Discussion and Analysis of Financial Condition and Results of Operations",
            "",
            "Item 7A.",
            "",
            "Quantitative and Qualitative Disclosures about Market Risk",
        ]
    )
    risk = _body("Item 1A. Risk Factors", 400) + "\n\nItem 1B. Unresolved Staff Comments\n"
    stub = (
        "Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations.\n\n"
        "Information required by this Item is presented in "
        "“Management's Discussion and Analysis of Financial Condition and Results of Operations” "
        "of this Form 10-K.\n\n"
        "Item 7A. Quantitative and Qualitative Disclosures about Market Risk.\n"
    )
    narrative = (
        "MANAGEMENT\u2019S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION AND RESULTS OF OPERATIONS.\n\n"
        + ("Revenue and margins are discussed in this narrative. " * 300)
        + "\n\nREPORT OF INDEPENDENT REGISTERED PUBLIC ACCOUNTING FIRM\n\nOpinion on the statements.\n"
    )
    text = f"{toc}\n\n{risk}\n\n{stub}\n\n{narrative}"
    sections = {section.item: section for section in extract_sections(text)}
    assert "full_text" not in sections
    assert sections["mdna"].extraction_method == "heading"
    assert sections["mdna"].char_count >= 5_000
    assert sections["mdna"].text.startswith("MANAGEMENT\u2019S DISCUSSION AND ANALYSIS")
    assert "Revenue and margins are discussed in this narrative." in sections["mdna"].text
    assert "Information required by this Item" not in sections["mdna"].text
    assert "REPORT OF INDEPENDENT" not in sections["mdna"].text
    assert "Quantitative and Qualitative Disclosures about Market Risk" not in sections["mdna"].text


def test_mdna_end_falls_back_to_item_8() -> None:
    text = (
        "Item 1A. Risk Factors\n\n" + ("Risk. " * 900) + "\n\nItem 1B. Other\n\n"
        "Item 7. Management's Discussion and Analysis\n\n"
        + ("Demand increased across the company. " * 300)
        + "\n\nItem 8. Financial Statements\n\nBalance sheet.\n"
    )
    sections = {section.item: section for section in extract_sections(text)}
    assert sections["mdna"].extraction_method == "regex"
    assert "Item 8." not in sections["mdna"].text
    assert "Demand increased" in sections["mdna"].text


def test_chunks_are_about_1200_characters_with_overlap() -> None:
    paragraphs = [f"Paragraph {index} " + ("word " * 40) for index in range(20)]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(len(chunk) < 1_600 for chunk in chunks)
    assert chunks[1].startswith(chunks[0][-200:].lstrip()) or chunks[0][-80:] in chunks[1]
