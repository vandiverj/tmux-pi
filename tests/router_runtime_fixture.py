"""Pinned Laya answers through the real router dispatch(), without an MCP/model."""
import json
import sys
from test_router import Response, RouterTests, dispatch

text = sys.argv[1]
fixture = RouterTests()
fixture.setUp()
change = "Fix typo" in text or "password" in text
sensitive = "password" in text
answer = fixture.laya_reply(
    intent="implementation" if change else "communication_or_documentation",
    changes=0.9 if change else 0.1,
    tools=0.9 if change else 0.1,
    sensitive=0.95 if sensitive else 0.1,
)
print(json.dumps(dispatch(text, fixture.config, opener=lambda request, timeout: Response(answer))))
