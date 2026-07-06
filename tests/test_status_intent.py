from seedbox_mcp.telegram_bot import _is_status_request


def test_status_intent_matches():
    for s in ["full status", "run the checks", "status report", "run a check cycle", "how is everything"]:
        assert _is_status_request(s) is True


def test_status_intent_ignores_normal_queries():
    for s in ["add star wars", "is Dune on plex", "what's the queue"]:
        assert _is_status_request(s) is False
