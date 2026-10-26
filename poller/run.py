"""Entry point that runs both pollers on a loop.

    python -m poller.run

Agree on these together before writing it:
  - poll interval (doc says every 5 to 10 seconds)
  - raw file layout, e.g. data/raw/<exchange>/<YYYY-MM-DD>/<timestamp>_<id>.json
  - what happens when one exchange errors (log it and keep going, don't crash)
  - how it stays alive for a full week without anyone restarting it
"""
