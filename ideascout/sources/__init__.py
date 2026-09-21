"""Website-source collection adapters (first: Yellowbrick).

The intelligence/screening layer (ideascout.idea_extraction,
ideascout.shadow) must never contain source-specific logic -- every
adapter in this package is responsible for producing a standard
base.SourceItem from whatever a given website's markup looks like, and
nothing downstream of that needs to know or care which site it came from.
"""
