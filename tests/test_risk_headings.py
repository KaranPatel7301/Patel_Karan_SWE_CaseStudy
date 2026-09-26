from app.ingest.filings import extract_risk_headings
from app.services.risk_diff import diff_headings


def test_extract_risk_headings_keeps_emphasized_paragraphs_in_the_section() -> None:
    html = """
    <html><body>
      <div style="display:none"><span style="font-weight:700">Hidden heading that is long enough to qualify as a risk heading.</span></div>
      <p style="font-weight:700">Item 1. Business overview that is long enough to pass the length filter.</p>
      <div style="font-weight:700">If Eaton is unable to protect its information technology infrastructure.</div>
      <div>
        <span style="font-weight:700;font-style:italic">We may be unable to adequately protect our intellectual property rights,</span>
        <span style="font-weight:700;font-style:italic">which could affect our ability to compete.</span>
      </div>
      <div><span style="font-weight:400">If Eaton is unable to protect its information technology infrastructure.</span></div>
      <div style="font-weight:700">Too short.</div>
      <p><b>Our operations depend on production facilities throughout the world, a real heading.</b></p>
    </body></html>
    """
    risk = (
        "If Eaton is unable to protect its information technology infrastructure. "
        "We may be unable to adequately protect our intellectual property rights, "
        "which could affect our ability to compete. "
        "Our operations depend on production facilities throughout the world, a real heading."
    )
    assert extract_risk_headings(html, risk) == [
        "If Eaton is unable to protect its information technology infrastructure.",
        "We may be unable to adequately protect our intellectual property rights, which could affect our ability to compete.",
        "Our operations depend on production facilities throughout the world, a real heading.",
    ]


def test_wrapped_italic_intro_over_400_characters_is_not_a_heading() -> None:
    html = """
    <html><body>
      <div style="font-style:italic">The following risk factors should be considered in addition to the other information in this Annual Report on Form 10-K. The following risks could harm our business, financial condition, results of operations or reputation, which could cause our stock</div>
      <div style="font-style:italic">price to decline. Additional risks and uncertainties not presently known to us may also harm our business, financial condition, results of operations or reputation.</div>
      <div style="font-weight:700">Commercial arrangements expose us to counterparty risks.</div>
    </body></html>
    """
    risk = (
        "The following risk factors should be considered in addition to the other information in this Annual Report on Form 10-K. "
        "The following risks could harm our business, financial condition, results of operations or reputation, which could cause our stock "
        "price to decline. Additional risks and uncertainties not presently known to us may also harm our business, financial condition, results of operations or reputation. "
        "Commercial arrangements expose us to counterparty risks."
    )
    assert extract_risk_headings(html, risk) == [
        "Commercial arrangements expose us to counterparty risks.",
    ]


def test_diff_reports_added_and_removed_headings() -> None:
    latest = [
        "Risks related to demand for our products and services in key markets.",
        "A brand new supply-chain concentration risk that did not exist in the prior filing.",
    ]
    prior = [
        "Risks related to demand for our products and services.",
        "An old litigation heading that was removed from the latest filing entirely.",
    ]
    added, removed, reworded = diff_headings(latest, prior)
    assert added == ["A brand new supply-chain concentration risk that did not exist in the prior filing."]
    assert removed == ["An old litigation heading that was removed from the latest filing entirely."]
    assert reworded == []


def test_climate_weather_pair_is_reworded() -> None:
    weather = (
        "Weather disruptions and regulatory, market and social reactions to them "
        "create uncertainties that could negatively impact our business."
    )
    climate = (
        "The effects of climate change, including weather disruptions and regulatory/market reactions, "
        "create uncertainties that could negatively impact our business."
    )
    added, removed, reworded = diff_headings(
        [weather, "A brand new artificial intelligence risk that Eaton did not disclose last year."],
        [climate, "An unrelated pension-plan heading that disappeared from the latest filing."],
    )
    assert added == ["A brand new artificial intelligence risk that Eaton did not disclose last year."]
    assert removed == ["An unrelated pension-plan heading that disappeared from the latest filing."]
    assert len(reworded) == 1
    assert reworded[0].latest == weather
    assert reworded[0].prior == climate
    assert 60 <= reworded[0].score < 85
