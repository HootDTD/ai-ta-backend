"""Import-time coverage for the prewarm CLI shim.

``main()`` itself is a real-model path (pragma-excluded, exercised manually
per campaign/README.md's "NLI model local cache + boot-time pre-warm"
section); this only asserts the module imports cleanly and exposes the
expected entrypoint.
"""

from campaign.infra import prewarm_nli


def test_module_exposes_main_entrypoint():
    assert callable(prewarm_nli.main)
