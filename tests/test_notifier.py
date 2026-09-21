"""Unit tests for ideascout/notifier.py -- proving there is no active
outbound-delivery code path anywhere yet.
"""

from __future__ import annotations

import pytest

from ideascout import notifier


def test_disabled_notifier_always_raises_regardless_of_input():
    n = notifier.DisabledNotifier()
    with pytest.raises(notifier.NotifierDisabledError):
        n.send_digest([])

    item = notifier.DigestItem(
        company="XYZ Corp", ticker="XYZ", source_name="yellowbrick", overall_prediction="INVESTIGATE_NOW",
        why_mispriced="x", upside_case="x", main_concern="x", canonical_url="https://x/pitch/abc123",
    )
    with pytest.raises(notifier.NotifierDisabledError):
        n.send_digest([item])
