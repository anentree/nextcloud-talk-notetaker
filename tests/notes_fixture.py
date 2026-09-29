"""Stand-in notes long enough to pass content verification (verify.py).

Pipeline tests used to return "# Notes" from a mocked summarizer. Real notes
are never that short, and verify.py now holds a recording whose notes are,
so the stubs need a realistic body.
"""

NOTES_BODY = (
    "\n\n## Topics\n\n- Reviewed the quarterly budget and the open invoices.\n"
    "- Agreed on the timeline for the next filing and who prepares it.\n"
    "\n## Action items\n\n| Owner | Task |\n|---|---|\n| Neo | Send the draft |\n"
)
