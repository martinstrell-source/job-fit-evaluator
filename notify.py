"""Alerting for strong-fit postings.

The pipeline (and the Streamlit app's filterable view) is where matches get
reviewed, so this just prints a line to stdout / the poller log. If you ever
want another channel (email, Slack, desktop), add it in this one function.
"""


def notify(title: str, message: str, url: str | None = None) -> None:
    print(f"[ALERT] {title}: {message}" + (f"  {url}" if url else ""))
